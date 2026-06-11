#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import sys
import time
import threading
from dataclasses import dataclass
from typing import Optional

import requests
from websocket import WebSocket, WebSocketTimeoutException, create_connection


class ObsRequestError(Exception):
    def __init__(self, request_type: str, comment: str):
        self.request_type = request_type
        self.comment = comment
        super().__init__(f"{request_type} failed: {comment}")


@dataclass
class BarkTarget:
    device_key: str
    code: str


@dataclass
class BarkConfig:
    server: str
    targets: list[BarkTarget]
    group: str
    sound: str


@dataclass
class NtfyTarget:
    topic: str
    token: str = ""


@dataclass
class NtfyConfig:
    server: str
    targets: list[NtfyTarget]
    priority: int
    tags: list[str]


@dataclass
class ObsConfig:
    host: str
    port: int
    password: str
    timeout_seconds: int


@dataclass
class MonitorConfig:
    check_interval_seconds: int
    alert_cooldown_seconds: int
    expect_recording: bool
    expect_streaming: bool
    detect_audio_silence: bool
    audio_input_name: str
    silence_threshold_db: float
    silence_seconds: int
    silence_only_when_output_active: bool
    log_audio_volume: bool
    log_audio_volume_interval_seconds: int
    meter_timeout_seconds: int
    startup_grace_seconds: int
    # 录制/推流均停止且仅在输出活跃时检测静音时：停止后过 N 秒再采一次电平并 Bark 告知（0=关闭）
    audio_recheck_after_output_stop_seconds: int
    # 曾在录制中变为未录制后，满 N 秒则退出进程（0=不退出）
    exit_after_record_stop_seconds: int


class BarkNotifier:
    def __init__(self, cfg: BarkConfig):
        self.cfg = cfg

    def send(self, title: str, body: str) -> None:
        if not self.cfg.targets:
            return

        for target in self.cfg.targets:
            url = f"{self.cfg.server.rstrip('/')}/{target.code}"
            payload = {
                "device_key": target.device_key,
                "title": title,
                "body": body,
                "group": self.cfg.group,
                "sound": self.cfg.sound,
            }
            try:
                resp = requests.post(url, json=payload, timeout=5)
                resp.raise_for_status()
            except Exception as exc:
                print(
                    "[WARN] Bark 推送失败"
                    f"（code={target.code[:6]}..., device_key={target.device_key[:6]}...）: {exc}"
                )


class NtfyNotifier:
    def __init__(self, cfg: NtfyConfig):
        self.cfg = cfg

    def send(self, title: str, body: str) -> None:
        if not self.cfg.targets:
            return

        for target in self.cfg.targets:
            url = f"{self.cfg.server.rstrip('/')}/{target.topic}"
            headers: dict[str, str] = {"Title": title}
            if self.cfg.tags:
                headers["Tags"] = ",".join(self.cfg.tags)
            if self.cfg.priority:
                headers["Priority"] = str(self.cfg.priority)
            if target.token:
                headers["Authorization"] = f"Bearer {target.token}"

            try:
                resp = requests.post(
                    url,
                    data=body.encode("utf-8"),
                    headers=headers,
                    timeout=5,
                )
                resp.raise_for_status()
            except Exception as exc:
                topic_preview = target.topic[:12] + "..." if len(target.topic) > 12 else target.topic
                print(f"[WARN] ntfy 推送失败（topic={topic_preview}）: {exc}")


class AlertNotifier:
    def __init__(self, bark_cfg: BarkConfig, ntfy_cfg: NtfyConfig):
        self.bark = BarkNotifier(bark_cfg)
        self.ntfy = NtfyNotifier(ntfy_cfg)

    def send(self, title: str, body: str) -> None:
        if not self.bark.cfg.targets and not self.ntfy.cfg.targets:
            print("[WARN] 未配置任何推送目标（Bark / ntfy），已跳过推送。")
            return
        self.bark.send(title, body)
        self.ntfy.send(title, body)


class ObsWatchdog:
    def __init__(self, obs_cfg: ObsConfig, monitor_cfg: MonitorConfig, notifier: AlertNotifier):
        self.obs_cfg = obs_cfg
        self.monitor_cfg = monitor_cfg
        self.notifier = notifier

        self.client: Optional[WebSocket] = None
        self.request_id = 0

        self.last_sound_active_ts: Optional[float] = None
        self.last_alert_ts_by_key: dict[str, float] = {}
        self.silence_alert_active = False
        self.meter_missing_alert_active = False

        self.last_audio_log_ts: float = 0.0
        self.last_meter_db: Optional[float] = None
        self.last_meter_ts: float = 0.0
        self.connected_ts: float = 0.0
        self.meter_parse_warned = False
        self.meter_name_hint_warned = False

        self._last_outputs_active: bool = False
        self._had_output_active_session: bool = False
        self._output_inactive_since: Optional[float] = None
        self._output_stop_recheck_sent: bool = False

        self._prev_recording_active: bool = False
        self._record_inactive_since: Optional[float] = None

    def _reset_runtime_audio_state(self) -> None:
        self.last_sound_active_ts = None
        self.silence_alert_active = False
        self.meter_missing_alert_active = False
        self.last_meter_db = None
        self.last_meter_ts = 0.0
        self.last_audio_log_ts = 0.0

    def _can_alert(self, key: str, now: float) -> bool:
        last = self.last_alert_ts_by_key.get(key, 0.0)
        if now - last >= self.monitor_cfg.alert_cooldown_seconds:
            self.last_alert_ts_by_key[key] = now
            return True
        return False

    def _alert(self, key: str, title: str, body: str) -> None:
        now = time.time()
        if self._can_alert(key, now):
            print(f"[ALERT] {title} - {body}")
            self.notifier.send(title, body)

    def _notify_no_cooldown(self, title: str, body: str) -> None:
        """立即推送（不受 alert_cooldown_seconds 限制）。"""
        print(f"[ALERT] {title} - {body}")
        self.notifier.send(title, body)

    def _disconnect_client(self) -> None:
        try:
            if self.client is not None:
                self.client.close()
        except Exception:
            pass
        self.client = None
        self.connected_ts = 0.0
        self._reset_runtime_audio_state()

    def _connect(self) -> bool:
        if self.client is not None:
            return True
        try:
            url = f"ws://{self.obs_cfg.host}:{self.obs_cfg.port}"
            ws = create_connection(url, timeout=self.obs_cfg.timeout_seconds)

            hello_raw = ws.recv()
            hello = json.loads(hello_raw)
            if hello.get("op") != 0:
                raise RuntimeError(f"Unexpected OBS hello payload: {hello}")

            identify = {
                "rpcVersion": 1,
                "eventSubscriptions": 2047 | 65536,
            }
            auth_info = (hello.get("d") or {}).get("authentication")
            if auth_info:
                salt = auth_info["salt"]
                challenge = auth_info["challenge"]
                secret = base64.b64encode(
                    hashlib.sha256((self.obs_cfg.password + salt).encode("utf-8")).digest()
                ).decode("utf-8")
                auth = base64.b64encode(
                    hashlib.sha256((secret + challenge).encode("utf-8")).digest()
                ).decode("utf-8")
                identify["authentication"] = auth

            ws.send(json.dumps({"op": 1, "d": identify}))
            identified_raw = ws.recv()
            identified = json.loads(identified_raw)
            if identified.get("op") != 2:
                raise RuntimeError(f"OBS 身份认证失败: {identified}")

            self.client = ws
            self.connected_ts = time.time()
            self._reset_runtime_audio_state()
            self._last_outputs_active = False
            self._had_output_active_session = False
            self._output_inactive_since = None
            self._output_stop_recheck_sent = False
            self._prev_recording_active = False
            self._record_inactive_since = None
            print("[INFO] 已连接 OBS WebSocket。")
            return True
        except Exception as exc:
            self._disconnect_client()
            print(f"[WARN] OBS 连接失败: {exc}")
            self._alert(
                "obs_disconnected",
                "OBS 已断开",
                "无法连接到 OBS WebSocket。",
            )
            return False

    def _pump_events(self, max_wait_ms: int = 120) -> None:
        if self.client is None:
            return
        end_at = time.time() + max_wait_ms / 1000.0
        old_timeout = self.client.gettimeout()
        try:
            self.client.settimeout(0.02)
            while time.time() < end_at:
                try:
                    raw = self.client.recv()
                except WebSocketTimeoutException:
                    continue
                msg = json.loads(raw)
                if msg.get("op") == 5:
                    self._handle_event(msg)
        finally:
            self.client.settimeout(old_timeout)

    def _flatten_numbers(self, value: object) -> list[float]:
        values: list[float] = []
        if isinstance(value, (int, float)):
            values.append(float(value))
            return values
        if isinstance(value, dict):
            for item in value.values():
                values.extend(self._flatten_numbers(item))
            return values
        if isinstance(value, list):
            for item in value:
                values.extend(self._flatten_numbers(item))
        return values

    def _handle_event(self, msg: dict) -> None:
        data = msg.get("d") or {}
        if data.get("eventType") != "InputVolumeMeters":
            return
        event_data = data.get("eventData") or {}
        seen_names: list[str] = []
        for item in event_data.get("inputs", []):
            input_name = item.get("inputName")
            if isinstance(input_name, str) and input_name:
                seen_names.append(input_name)
            if input_name != self.monitor_cfg.audio_input_name:
                continue

            db_values = self._flatten_numbers(item.get("inputLevelsDb"))
            if not db_values:
                mul_values = self._flatten_numbers(item.get("inputLevelsMul"))
                db_values = [20.0 * math.log10(max(v, 1e-6)) if v > 0 else -120.0 for v in mul_values]
            if not db_values:
                if not self.meter_parse_warned:
                    print(
                        "[WARN] 已收到目标输入的实时电平事件，但字段解析为空。"
                        f"可见字段: {list(item.keys())}"
                    )
                    self.meter_parse_warned = True
                continue

            finite_values = [v for v in db_values if math.isfinite(v)]
            current_db = max(finite_values) if finite_values else -120.0

            self.last_meter_db = current_db
            self.last_meter_ts = time.time()
            break
        else:
            if seen_names and not self.meter_name_hint_warned:
                preview = ", ".join(seen_names[:8])
                print(
                    "[WARN] 已收到实时电平事件，但未匹配到配置的 audio_input_name。"
                    f"事件中的输入名示例: {preview}"
                )
                self.meter_name_hint_warned = True

    def _obs_request(self, request_type: str, request_data: Optional[dict] = None) -> dict:
        if self.client is None:
            raise RuntimeError("OBS WebSocket 未连接")

        self.request_id += 1
        req_id = f"req-{self.request_id}"
        payload = {
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": req_id,
                "requestData": request_data or {},
            },
        }
        self.client.send(json.dumps(payload))

        while True:
            raw = self.client.recv()
            msg = json.loads(raw)
            if msg.get("op") == 5:
                self._handle_event(msg)
                continue
            if msg.get("op") != 7:
                continue
            data = msg.get("d") or {}
            if data.get("requestId") != req_id:
                continue
            status = data.get("requestStatus") or {}
            if not status.get("result"):
                comment = status.get("comment", "unknown error")
                raise ObsRequestError(request_type=request_type, comment=comment)
            return data.get("responseData") or {}

    def list_inputs(self) -> list[tuple[str, str]]:
        if not self._connect():
            return []

        all_items: list[tuple[str, str]] = []
        try:
            inputs_resp = self._obs_request("GetInputList")
            for item in inputs_resp.get("inputs", []):
                input_name = item.get("inputName", "")
                input_kind = item.get("inputKind", "")
                if input_name:
                    all_items.append((input_kind, input_name))
        except Exception as exc:
            print(f"[WARN] 获取输入列表失败: {exc}")
            return []

        return all_items

    def _check_output_status(self) -> tuple[bool, bool]:
        if self.client is None:
            return False, False
        try:
            rec = self._obs_request("GetRecordStatus")
            stream = self._obs_request("GetStreamStatus")
            return bool(rec.get("outputActive")), bool(stream.get("outputActive"))
        except Exception as exc:
            print(f"[WARN] 读取 OBS 状态失败: {exc}")
            self._disconnect_client()
            self._alert(
                "obs_disconnected",
                "OBS 状态不可用",
                "读取 OBS 状态时 WebSocket 连接中断。",
            )
            return False, False

    def _get_input_volume_db(self) -> Optional[float]:
        if self.client is None:
            return None

        now = time.time()
        self._pump_events()

        if now - self.connected_ts < self.monitor_cfg.startup_grace_seconds:
            return None

        if self.last_meter_db is not None and now - self.last_meter_ts <= self.monitor_cfg.meter_timeout_seconds:
            if self.meter_missing_alert_active:
                print("[INFO] 已重新收到音频电平事件。")
                self.meter_missing_alert_active = False
            return self.last_meter_db

        if not self.meter_missing_alert_active:
            self._alert(
                "audio_meter_missing",
                "OBS 音频电平不可用",
                (
                    f"音频输入 '{self.monitor_cfg.audio_input_name}' 的实时电平事件"
                    f"已超过 {self.monitor_cfg.meter_timeout_seconds} 秒未更新。"
                ),
            )
            self.meter_missing_alert_active = True

        return None

    def _sample_input_volume_db_quiet(self) -> tuple[Optional[float], str]:
        """拉取当前电平用于汇报，不触发「电平不可用」类告警。"""
        now = time.time()
        self._pump_events()
        if now - self.connected_ts < self.monitor_cfg.startup_grace_seconds:
            return None, "连接后仍在启动宽限期内，未采样"
        if (
            self.last_meter_db is not None
            and now - self.last_meter_ts <= self.monitor_cfg.meter_timeout_seconds
        ):
            return self.last_meter_db, "已收到实时电平"
        return (
            None,
            f"已超过 {self.monitor_cfg.meter_timeout_seconds} 秒未收到该输入的电平更新",
        )

    def _check_audio_silence(self, recording_active: bool, streaming_active: bool) -> None:
        if not self.monitor_cfg.detect_audio_silence:
            return

        outputs_active = recording_active or streaming_active
        prev_outputs_active = self._last_outputs_active
        now = time.time()

        recheck_sec = self.monitor_cfg.audio_recheck_after_output_stop_seconds
        if (
            recheck_sec > 0
            and self.monitor_cfg.silence_only_when_output_active
            and self.client is not None
        ):
            if outputs_active:
                self._had_output_active_session = True
                self._output_inactive_since = None
                self._output_stop_recheck_sent = False
            else:
                if prev_outputs_active:
                    self._output_inactive_since = now
                    self._output_stop_recheck_sent = False
                if (
                    self._had_output_active_session
                    and self._output_inactive_since is not None
                    and not self._output_stop_recheck_sent
                    and now - self._output_inactive_since >= recheck_sec
                ):
                    db, detail = self._sample_input_volume_db_quiet()
                    if db is not None:
                        loud = db > self.monitor_cfg.silence_threshold_db
                        body = (
                            f"输入「{self.monitor_cfg.audio_input_name}」停止输出约 "
                            f"{int(now - self._output_inactive_since)} 秒后复查："
                            f"{db:.1f} dB（阈值 {self.monitor_cfg.silence_threshold_db:.1f} dB），"
                            f"判定为{'有声' if loud else '低于阈值/静音区间'}。{detail}"
                        )
                    else:
                        body = (
                            f"输入「{self.monitor_cfg.audio_input_name}」停止输出约 "
                            f"{int(now - self._output_inactive_since)} 秒后复查：{detail}。"
                        )
                    self._alert(
                        "audio_stop_recheck",
                        "录制/推流停止后音频复查",
                        body,
                    )
                    self._output_stop_recheck_sent = True

        self._last_outputs_active = outputs_active

        should_check = True
        if self.monitor_cfg.silence_only_when_output_active:
            should_check = outputs_active

        if not should_check:
            self.last_sound_active_ts = None
            self.silence_alert_active = False
            self.meter_missing_alert_active = False
            return

        now = time.time()
        volume_db = self._get_input_volume_db()

        if volume_db is None:
            self._maybe_log_audio_volume(None, now)
            return

        self._maybe_log_audio_volume(volume_db, now)

        if volume_db > self.monitor_cfg.silence_threshold_db:
            self.last_sound_active_ts = now
            if self.silence_alert_active:
                print("[INFO] 声音已恢复。")
                self._notify_no_cooldown(
                    "OBS 声音已恢复",
                    (
                        f"音频输入 '{self.monitor_cfg.audio_input_name}' "
                        f"音量已恢复至 {volume_db:.1f} dB（阈值 "
                        f"{self.monitor_cfg.silence_threshold_db:.1f} dB）。"
                    ),
                )
                self.silence_alert_active = False
            return

        if self.last_sound_active_ts is None:
            self.last_sound_active_ts = now
            return

        silent_for = now - self.last_sound_active_ts
        if silent_for >= self.monitor_cfg.silence_seconds and not self.silence_alert_active:
            self._alert(
                "audio_silent",
                "OBS 声音可能中断",
                (
                    f"音频输入 '{self.monitor_cfg.audio_input_name}' "
                    f"音量连续 {int(silent_for)} 秒低于 "
                    f"{self.monitor_cfg.silence_threshold_db:.1f} dB。"
                ),
            )
            self.silence_alert_active = True

    def _maybe_log_audio_volume(self, volume_db: Optional[float], now: float) -> None:
        if not self.monitor_cfg.log_audio_volume:
            return
        interval = max(0, self.monitor_cfg.log_audio_volume_interval_seconds)
        if interval > 0 and now - self.last_audio_log_ts < interval:
            return

        if volume_db is None:
            # Skip "unknown" log during startup grace period to avoid one-time false alarm.
            if now - self.connected_ts < self.monitor_cfg.startup_grace_seconds:
                return
            self.last_audio_log_ts = now
            print(
                "[AUDIO] "
                f"输入='{self.monitor_cfg.audio_input_name}' "
                "音量=无实时数据 "
                f"阈值={self.monitor_cfg.silence_threshold_db:.1f} dB "
                "状态=未知"
            )
            return

        self.last_audio_log_ts = now
        status = "有声" if volume_db > self.monitor_cfg.silence_threshold_db else "静音区间"
        print(
            "[AUDIO] "
            f"输入='{self.monitor_cfg.audio_input_name}' "
            f"音量={volume_db:.1f} dB "
            f"阈值={self.monitor_cfg.silence_threshold_db:.1f} dB "
            f"状态={status}"
        )

    def run_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        print("[INFO] OBS 监控已启动。")
        while True:
            if stop_event is not None and stop_event.is_set():
                print("[INFO] 收到停止信号，监控即将退出。")
                self._disconnect_client()
                return
            if self._connect():
                recording_active, streaming_active = self._check_output_status()

                exit_sec = self.monitor_cfg.exit_after_record_stop_seconds
                if exit_sec > 0:
                    now = time.time()
                    if recording_active:
                        self._record_inactive_since = None
                    elif self._prev_recording_active:
                        self._record_inactive_since = now
                    if self._record_inactive_since is not None:
                        if now - self._record_inactive_since >= exit_sec:
                            print(
                                f"[INFO] 录制已停止超过 {exit_sec} 秒，退出程序。"
                            )
                            self._disconnect_client()
                            sys.exit(0)

                if self.monitor_cfg.expect_recording and not recording_active:
                    self._alert(
                        "record_stopped",
                        "OBS 录制已停止",
                        "当前未检测到录制进行中。",
                    )

                if self.monitor_cfg.expect_streaming and not streaming_active:
                    self._alert(
                        "stream_stopped",
                        "OBS 推流已停止",
                        "当前未检测到推流进行中。",
                    )

                self._check_audio_silence(recording_active, streaming_active)

                self._prev_recording_active = recording_active

            interval = max(0, int(self.monitor_cfg.check_interval_seconds))
            if stop_event is None or interval == 0:
                time.sleep(interval)
                continue
            # Use interruptible sleep so GUI stop requests can be handled quickly.
            if stop_event.wait(timeout=interval):
                print("[INFO] 收到停止信号，监控即将退出。")
                self._disconnect_client()
                return


def _load_ntfy_config(raw: dict) -> NtfyConfig:
    ntfy_raw = dict(raw.get("ntfy") or {})
    targets: list[NtfyTarget] = []

    for item in ntfy_raw.get("targets", []) or []:
        topic = str(item.get("topic", "")).strip()
        token = str(item.get("token", "")).strip()
        if topic:
            targets.append(NtfyTarget(topic=topic, token=token))

    if not targets:
        for topic in ntfy_raw.get("topics", []) or []:
            t = str(topic).strip()
            if t:
                targets.append(NtfyTarget(topic=t))

    legacy_topic = str(ntfy_raw.get("topic", "")).strip()
    if not targets and legacy_topic:
        targets.append(NtfyTarget(topic=legacy_topic, token=str(ntfy_raw.get("token", "")).strip()))

    priority_raw = ntfy_raw.get("priority", 4)
    priority_map = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}
    if isinstance(priority_raw, str):
        priority = priority_map.get(priority_raw.strip().lower(), 4)
    else:
        priority = int(priority_raw)

    tags_raw = ntfy_raw.get("tags", ["warning", "obs"]) or []
    tags = [str(t).strip() for t in tags_raw if str(t).strip()]

    return NtfyConfig(
        server=str(ntfy_raw.get("server", "https://ntfy.sh")).strip() or "https://ntfy.sh",
        targets=targets,
        priority=max(1, min(5, priority)),
        tags=tags,
    )


def load_config(path: str) -> tuple[ObsConfig, BarkConfig, NtfyConfig, MonitorConfig]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    obs_cfg = ObsConfig(**raw["obs"])
    bark_raw = dict(raw["bark"])
    targets: list[BarkTarget] = []

    raw_targets = bark_raw.get("targets", [])
    for item in raw_targets:
        device_key = str(item.get("device_key", "")).strip()
        code = str(item.get("code", "")).strip()
        if device_key and code:
            targets.append(BarkTarget(device_key=device_key, code=code))

    if not targets:
        codes = bark_raw.get("codes", []) or []
        device_keys = bark_raw.get("device_keys", []) or []
        for device_key, code in zip(device_keys, codes):
            dk = str(device_key).strip()
            c = str(code).strip()
            if dk and c:
                targets.append(BarkTarget(device_key=dk, code=c))

    if not targets:
        legacy_device_key = str(bark_raw.get("device_key", "")).strip()
        legacy_code = str(bark_raw.get("code", "")).strip()
        if legacy_device_key and legacy_code:
            targets.append(BarkTarget(device_key=legacy_device_key, code=legacy_code))

    bark_cfg = BarkConfig(
        server=bark_raw["server"],
        targets=targets,
        group=bark_raw.get("group", "OBS Watchdog"),
        sound=bark_raw.get("sound", "bell"),
    )

    monitor_raw = raw["monitor"]
    monitor_cfg = MonitorConfig(
        check_interval_seconds=int(monitor_raw["check_interval_seconds"]),
        alert_cooldown_seconds=int(monitor_raw["alert_cooldown_seconds"]),
        expect_recording=bool(monitor_raw.get("expect_recording", True)),
        expect_streaming=bool(monitor_raw.get("expect_streaming", False)),
        detect_audio_silence=bool(
            monitor_raw.get("detect_audio_silence", monitor_raw.get("detect_stall", False))
        ),
        audio_input_name=str(
            monitor_raw.get("audio_input_name", monitor_raw.get("stall_source_name", ""))
        ).strip(),
        silence_threshold_db=float(monitor_raw.get("silence_threshold_db", -50.0)),
        silence_seconds=int(
            monitor_raw.get("silence_seconds", monitor_raw.get("stall_seconds", 20))
        ),
        silence_only_when_output_active=bool(
            monitor_raw.get(
                "silence_only_when_output_active",
                monitor_raw.get("stall_only_when_output_active", True),
            )
        ),
        log_audio_volume=bool(monitor_raw.get("log_audio_volume", False)),
        log_audio_volume_interval_seconds=int(
            monitor_raw.get(
                "log_audio_volume_interval_seconds",
                monitor_raw.get("check_interval_seconds", 5),
            )
        ),
        meter_timeout_seconds=int(monitor_raw.get("meter_timeout_seconds", 8)),
        startup_grace_seconds=int(monitor_raw.get("startup_grace_seconds", 5)),
        audio_recheck_after_output_stop_seconds=int(
            monitor_raw.get("audio_recheck_after_output_stop_seconds", 0)
        ),
        exit_after_record_stop_seconds=int(
            monitor_raw.get("exit_after_record_stop_seconds", 0)
        ),
    )
    ntfy_cfg = _load_ntfy_config(raw)
    return obs_cfg, bark_cfg, ntfy_cfg, monitor_cfg


def main() -> None:
    parser = argparse.ArgumentParser(description="OBS 监控并通过 Bark / ntfy 告警")
    parser.add_argument("--config", default="config.json", help="配置文件路径（JSON）")
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="仅列出 OBS 的输入名（可用于 audio_input_name），不启动监控",
    )
    args = parser.parse_args()

    obs_cfg, bark_cfg, ntfy_cfg, monitor_cfg = load_config(args.config)
    notifier = AlertNotifier(bark_cfg, ntfy_cfg)
    watcher = ObsWatchdog(obs_cfg, monitor_cfg, notifier)

    if args.list_sources:
        pairs = watcher.list_inputs()
        if not pairs:
            print("[WARN] 没有读取到任何输入。请检查 OBS 是否开启、WebSocket 配置是否正确。")
            return

        print("=== OBS 输入列表 ===")
        for input_kind, input_name in pairs:
            print(f"- 类型: {input_kind} | 输入: {input_name}")

        unique_sources = sorted({src for _, src in pairs})
        print("\n=== 可用于 audio_input_name 的输入名（去重） ===")
        for source_name in unique_sources:
            print(f"- {source_name}")
        return

    watcher.run_forever()


if __name__ == "__main__":
    main()