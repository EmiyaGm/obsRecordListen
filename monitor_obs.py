#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Optional

import requests
from websocket import WebSocket, create_connection


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


class BarkNotifier:
    def __init__(self, cfg: BarkConfig):
        self.cfg = cfg

    def send(self, title: str, body: str) -> None:
        if not self.cfg.targets:
            print("[WARN] 未配置 Bark targets，已跳过推送。")
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


class ObsWatchdog:
    def __init__(self, obs_cfg: ObsConfig, monitor_cfg: MonitorConfig, notifier: BarkNotifier):
        self.obs_cfg = obs_cfg
        self.monitor_cfg = monitor_cfg
        self.notifier = notifier

        self.client: Optional[WebSocket] = None
        self.request_id = 0
        self.last_sound_active_ts: Optional[float] = None
        self.last_alert_ts_by_key: dict[str, float] = {}
        self.silence_alert_active = False
        self.last_audio_log_ts: float = 0.0
        self.last_meter_db: Optional[float] = None
        self.last_meter_ts: float = 0.0

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
                # Subscribe to all events, including high-volume meter events.
                "eventSubscriptions": 0xFFFFFFFF,
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
            print("[INFO] 已连接 OBS WebSocket。")
            return True
        except Exception as exc:
            try:
                if self.client is not None:
                    self.client.close()
            except Exception:
                pass
            self.client = None
            print(f"[WARN] OBS 连接失败: {exc}")
            self._alert(
                "obs_disconnected",
                "OBS 已断开",
                "无法连接到 OBS WebSocket。",
            )
            return False

    def _flatten_numbers(self, value: object) -> list[float]:
        values: list[float] = []
        if isinstance(value, (int, float)):
            values.append(float(value))
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
        for item in event_data.get("inputs", []):
            input_name = item.get("inputName")
            if input_name != self.monitor_cfg.audio_input_name:
                continue
            db_values = self._flatten_numbers(item.get("inputLevelsDb"))
            if not db_values:
                continue
            # OBS meter may include -inf when silent; choose the highest finite value.
            finite_values = [v for v in db_values if math.isfinite(v)]
            current_db = max(finite_values) if finite_values else -120.0
            self.last_meter_db = current_db
            self.last_meter_ts = time.time()
            break

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

        # Ignore events/other messages until request response arrives.
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
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
            self._alert(
                "obs_disconnected",
                "OBS 状态不可用",
                "读取 OBS 状态时 WebSocket 连接中断。",
            )
            return False, False

    def _get_input_volume_db(self) -> Optional[float]:
        if self.client is None:
            return None
        try:
            now = time.time()
            # Prefer realtime meter data when available.
            freshness_window = max(1, self.monitor_cfg.check_interval_seconds * 2)
            if self.last_meter_db is not None and now - self.last_meter_ts <= freshness_window:
                return self.last_meter_db

            resp = self._obs_request(
                "GetInputVolume",
                {
                    "inputName": self.monitor_cfg.audio_input_name,
                }
            )
            volume_db = resp.get("inputVolumeDb")
            if volume_db is None:
                print("[WARN] OBS 未返回 inputVolumeDb，跳过本次声音检测。")
                return None
            print(
                "[WARN] 当前使用输入增益值而非实时电平，"
                "请确认 OBS 版本支持 InputVolumeMeters 事件。"
            )
            return float(volume_db)
        except ObsRequestError as exc:
            # Input name is invalid: this is config issue, not websocket disconnection.
            if exc.request_type == "GetInputVolume" and "No input was found" in exc.comment:
                print(
                    f"[WARN] 音频输入不存在: {self.monitor_cfg.audio_input_name}，已停用静音检测。"
                )
                self.monitor_cfg.detect_audio_silence = False
                self._alert(
                    "audio_input_missing",
                    "音频输入不存在",
                    (
                        f"OBS 中未找到音频输入 '{self.monitor_cfg.audio_input_name}'，"
                        "已自动停用静音检测。请修改 config.json 的 audio_input_name。"
                    ),
                )
                return None
            print(f"[WARN] OBS 音量请求失败: {exc}")
            return None
        except Exception as exc:
            print(f"[WARN] OBS 读取音量失败: {exc}")
            try:
                if self.client is not None:
                    self.client.close()
            except Exception:
                pass
            self.client = None
            self._alert(
                "audio_volume_failed",
                "OBS 音量读取失败",
                "无法读取音频输入音量，无法进行静音检测。",
            )
            return None

    def _check_audio_silence(self, recording_active: bool, streaming_active: bool) -> None:
        if not self.monitor_cfg.detect_audio_silence:
            return

        should_check = True
        if self.monitor_cfg.silence_only_when_output_active:
            should_check = recording_active or streaming_active
        if not should_check:
            self.last_sound_active_ts = None
            self.silence_alert_active = False
            return

        now = time.time()
        volume_db = self._get_input_volume_db()
        if volume_db is None:
            return
        self._maybe_log_audio_volume(volume_db, now)

        if volume_db > self.monitor_cfg.silence_threshold_db:
            self.last_sound_active_ts = now
            if self.silence_alert_active:
                print("[INFO] 声音已恢复。")
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

    def _maybe_log_audio_volume(self, volume_db: float, now: float) -> None:
        if not self.monitor_cfg.log_audio_volume:
            return
        interval = max(0, self.monitor_cfg.log_audio_volume_interval_seconds)
        if interval > 0 and now - self.last_audio_log_ts < interval:
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

    def run_forever(self) -> None:
        print("[INFO] OBS 监控已启动。")
        while True:
            if self._connect():
                recording_active, streaming_active = self._check_output_status()

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

            time.sleep(self.monitor_cfg.check_interval_seconds)


def load_config(path: str) -> tuple[ObsConfig, BarkConfig, MonitorConfig]:
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

    # Backward compatibility: old config may provide separate arrays/fields.
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
        silence_seconds=int(monitor_raw.get("silence_seconds", monitor_raw.get("stall_seconds", 20))),
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
    )
    return obs_cfg, bark_cfg, monitor_cfg


def main() -> None:
    parser = argparse.ArgumentParser(description="OBS 监控并通过 Bark 告警")
    parser.add_argument("--config", default="config.json", help="配置文件路径（JSON）")
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="仅列出 OBS 的输入名（可用于 audio_input_name），不启动监控",
    )
    args = parser.parse_args()

    obs_cfg, bark_cfg, monitor_cfg = load_config(args.config)
    notifier = BarkNotifier(bark_cfg)
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
