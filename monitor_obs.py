#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
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
    detect_stall: bool
    stall_source_name: str
    stall_image_width: int
    stall_image_height: int
    stall_image_quality: int
    stall_seconds: int
    stall_only_when_output_active: bool


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
        self.prev_screenshot_hash: Optional[str] = None
        self.last_change_ts: Optional[float] = None
        self.last_alert_ts_by_key: dict[str, float] = {}
        self.stall_alert_active = False

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

            identify = {"rpcVersion": 1}
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

    def list_sources(self) -> list[tuple[str, str]]:
        if not self._connect():
            return []

        all_items: list[tuple[str, str]] = []
        try:
            scenes_resp = self._obs_request("GetSceneList")
            scenes = scenes_resp.get("scenes", [])
            for scene in scenes:
                scene_name = scene.get("sceneName", "")
                if not scene_name:
                    continue
                items_resp = self._obs_request(
                    "GetSceneItemList", {"sceneName": scene_name}
                )
                for item in items_resp.get("sceneItems", []):
                    source_name = item.get("sourceName", "")
                    if source_name:
                        all_items.append((scene_name, source_name))
        except Exception as exc:
            print(f"[WARN] 获取来源列表失败: {exc}")
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

    def _get_screenshot_hash(self) -> Optional[str]:
        if self.client is None:
            return None
        try:
            shot = self._obs_request(
                "GetSourceScreenshot",
                {
                    "sourceName": self.monitor_cfg.stall_source_name,
                    "imageFormat": "jpg",
                    "imageWidth": self.monitor_cfg.stall_image_width,
                    "imageHeight": self.monitor_cfg.stall_image_height,
                    "imageCompressionQuality": self.monitor_cfg.stall_image_quality,
                },
            )
            # Returns data URL like: data:image/jpg;base64,XXXX
            image_data_url = shot.get("imageData", "")
            b64_data = image_data_url.split(",", 1)[1] if "," in image_data_url else image_data_url
            raw = base64.b64decode(b64_data)
            return hashlib.sha256(raw).hexdigest()
        except ObsRequestError as exc:
            # Source name is invalid: this is config issue, not websocket disconnection.
            if exc.request_type == "GetSourceScreenshot" and "No source was found" in exc.comment:
                print(
                    f"[WARN] 截图源不存在: {self.monitor_cfg.stall_source_name}，已停用卡画面检测。"
                )
                self.monitor_cfg.detect_stall = False
                self._alert(
                    "screenshot_source_missing",
                    "截图源不存在",
                    (
                        f"OBS 中未找到来源 '{self.monitor_cfg.stall_source_name}'，"
                        "已自动停用卡画面检测。请修改 config.json 的 stall_source_name。"
                    ),
                )
                return None
            print(f"[WARN] OBS 截图请求失败: {exc}")
            return None
        except Exception as exc:
            print(f"[WARN] OBS 截图失败: {exc}")
            try:
                if self.client is not None:
                    self.client.close()
            except Exception:
                pass
            self.client = None
            self._alert(
                "screenshot_failed",
                "OBS 截图失败",
                "无法截图，无法进行卡画面检测。",
            )
            return None

    def _check_stall(self, recording_active: bool, streaming_active: bool) -> None:
        if not self.monitor_cfg.detect_stall:
            return

        should_check = True
        if self.monitor_cfg.stall_only_when_output_active:
            should_check = recording_active or streaming_active
        if not should_check:
            self.prev_screenshot_hash = None
            self.last_change_ts = None
            self.stall_alert_active = False
            return

        now = time.time()
        current_hash = self._get_screenshot_hash()
        if not current_hash:
            return

        if self.prev_screenshot_hash != current_hash:
            self.prev_screenshot_hash = current_hash
            self.last_change_ts = now
            if self.stall_alert_active:
                print("[INFO] 画面已恢复变化。")
                self.stall_alert_active = False
            return

        if self.last_change_ts is None:
            self.last_change_ts = now
            return

        stalled_for = now - self.last_change_ts
        if stalled_for >= self.monitor_cfg.stall_seconds and not self.stall_alert_active:
            self._alert(
                "video_stalled",
                "OBS 画面可能卡住",
                f"来源 '{self.monitor_cfg.stall_source_name}' 已连续 {int(stalled_for)} 秒无变化。",
            )
            self.stall_alert_active = True

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

                self._check_stall(recording_active, streaming_active)

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
    monitor_cfg = MonitorConfig(**raw["monitor"])
    return obs_cfg, bark_cfg, monitor_cfg


def main() -> None:
    parser = argparse.ArgumentParser(description="OBS 监控并通过 Bark 告警")
    parser.add_argument("--config", default="config.json", help="配置文件路径（JSON）")
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="仅列出 OBS 场景中的所有来源名，不启动监控",
    )
    args = parser.parse_args()

    obs_cfg, bark_cfg, monitor_cfg = load_config(args.config)
    notifier = BarkNotifier(bark_cfg)
    watcher = ObsWatchdog(obs_cfg, monitor_cfg, notifier)
    if args.list_sources:
        pairs = watcher.list_sources()
        if not pairs:
            print("[WARN] 没有读取到任何来源。请检查 OBS 是否开启、WebSocket 配置是否正确。")
            return

        print("=== OBS 场景/来源列表 ===")
        for scene_name, source_name in pairs:
            print(f"- 场景: {scene_name} | 来源: {source_name}")

        unique_sources = sorted({src for _, src in pairs})
        print("\n=== 可用于 stall_source_name 的来源名（去重） ===")
        for source_name in unique_sources:
            print(f"- {source_name}")
        return

    watcher.run_forever()


if __name__ == "__main__":
    main()
