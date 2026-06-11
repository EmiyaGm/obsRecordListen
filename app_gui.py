#!/usr/bin/env python3
from __future__ import annotations

import json
import queue
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from monitor_obs import (
    AlertNotifier,
    BarkConfig,
    BarkTarget,
    MonitorConfig,
    NtfyConfig,
    NtfyTarget,
    ObsConfig,
    ObsWatchdog,
    load_config,
)


class QueueWriter:
    def __init__(self, out_queue: queue.Queue[str]):
        self.out_queue = out_queue

    def write(self, text: str) -> int:
        if text:
            self.out_queue.put(text)
        return len(text)

    def flush(self) -> None:
        return


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OBS Watchdog GUI")
        self.resize(980, 760)

        self.config_path: Path = Path("config.json")
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None

        central = QWidget(self)
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        config_container = QWidget()
        config_layout = QVBoxLayout(config_container)
        config_layout.setContentsMargins(0, 0, 0, 0)
        config_layout.setSpacing(8)

        path_row = QHBoxLayout()
        self.config_path_input = QLineEdit(str(self.config_path))
        btn_pick = QPushButton("选择配置文件")
        btn_pick.clicked.connect(self.pick_config_path)
        path_row.addWidget(QLabel("配置文件"))
        path_row.addWidget(self.config_path_input)
        path_row.addWidget(btn_pick)
        config_layout.addLayout(path_row)

        config_layout.addWidget(self._build_obs_group())
        config_layout.addWidget(self._build_bark_group())
        config_layout.addWidget(self._build_ntfy_group())
        config_layout.addWidget(self._build_monitor_group())
        config_layout.addLayout(self._build_action_row())
        config_layout.addStretch(1)

        self.config_scroll = QScrollArea()
        self.config_scroll.setWidgetResizable(True)
        self.config_scroll.setWidget(config_container)
        main_layout.addWidget(self.config_scroll, 3)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("运行日志会显示在这里...")
        self.log_output.setMinimumHeight(180)
        main_layout.addWidget(QLabel("日志"))
        main_layout.addWidget(self.log_output, 2)

        self.log_timer = QTimer(self)
        self.log_timer.setInterval(200)
        self.log_timer.timeout.connect(self.flush_log_queue)
        self.log_timer.start()

        self.load_config_from_file()

    def _label_with_desc(self, title: str, desc: str) -> str:
        return f"{title}（{desc}）"

    def _build_obs_group(self) -> QGroupBox:
        group = QGroupBox("OBS 连接设置")
        form = QFormLayout(group)

        self.obs_host = QLineEdit()
        self.obs_port = QSpinBox()
        self.obs_port.setRange(1, 65535)
        self.obs_password = QLineEdit()
        self.obs_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.obs_timeout = QSpinBox()
        self.obs_timeout.setRange(1, 120)

        self.obs_host.setToolTip("OBS WebSocket 地址，常见为 localhost 或 127.0.0.1")
        self.obs_port.setToolTip("OBS WebSocket 端口，默认常见为 4455")
        self.obs_password.setToolTip("OBS WebSocket 密码，若未设置可留空")
        self.obs_timeout.setToolTip("连接或请求 OBS 的超时时间（秒）")

        form.addRow(self._label_with_desc("主机地址", "OBS WebSocket 所在地址"), self.obs_host)
        form.addRow(self._label_with_desc("端口", "OBS WebSocket 端口"), self.obs_port)
        form.addRow(self._label_with_desc("密码", "OBS WebSocket 认证密码"), self.obs_password)
        form.addRow(self._label_with_desc("超时秒数", "连接/请求超时"), self.obs_timeout)
        return group

    def _build_bark_group(self) -> QGroupBox:
        group = QGroupBox("Bark 推送设置")
        layout = QVBoxLayout(group)
        form = QFormLayout()

        self.bark_server = QLineEdit()
        self.bark_group = QLineEdit()
        self.bark_sound = QLineEdit()

        self.bark_server.setToolTip("Bark 服务地址，例如 https://api.day.app")
        self.bark_group.setToolTip("通知分组名称，便于在 Bark 中归类")
        self.bark_sound.setToolTip("通知声音标识，例如 bell")

        form.addRow(self._label_with_desc("服务地址", "Bark 服务端 URL"), self.bark_server)
        form.addRow(self._label_with_desc("通知分组", "Bark 分组名"), self.bark_group)
        form.addRow(self._label_with_desc("通知声音", "Bark sound 参数"), self.bark_sound)
        layout.addLayout(form)

        layout.addWidget(QLabel("推送目标列表（每行一个设备）"))
        self.targets_table = QTableWidget(0, 2)
        self.targets_table.setHorizontalHeaderLabels(["设备 Key（device_key）", "推送码（code）"])
        self.targets_table.setToolTip("每行填写一个 Bark 目标，device_key 和 code 必须同时填写")
        self.targets_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.targets_table)

        row = QHBoxLayout()
        btn_add = QPushButton("新增目标")
        btn_del = QPushButton("删除选中行")
        btn_add.clicked.connect(self.add_target_row)
        btn_del.clicked.connect(self.delete_selected_target_rows)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        row.addStretch(1)
        layout.addLayout(row)

        return group

    def _build_ntfy_group(self) -> QGroupBox:
        group = QGroupBox("ntfy 推送设置（适合 Android）")
        layout = QVBoxLayout(group)
        form = QFormLayout()

        self.ntfy_server = QLineEdit()
        self.ntfy_priority = QSpinBox()
        self.ntfy_priority.setRange(1, 5)
        self.ntfy_tags = QLineEdit()

        self.ntfy_server.setToolTip("ntfy 服务地址，默认 https://ntfy.sh，也可填自建服务器")
        self.ntfy_priority.setToolTip("通知优先级：1 最低，3 默认，4 高，5 紧急")
        self.ntfy_tags.setToolTip("通知标签，逗号分隔，例如 warning,obs")

        form.addRow(self._label_with_desc("服务地址", "ntfy 服务端 URL"), self.ntfy_server)
        form.addRow(self._label_with_desc("优先级", "1-5，数值越大越醒目"), self.ntfy_priority)
        form.addRow(self._label_with_desc("标签", "逗号分隔的标签或 emoji"), self.ntfy_tags)
        layout.addLayout(form)

        layout.addWidget(QLabel("订阅主题列表（每行一个 topic，可选填访问令牌）"))
        self.ntfy_targets_table = QTableWidget(0, 2)
        self.ntfy_targets_table.setHorizontalHeaderLabels(["主题（topic）", "访问令牌（token，可选）"])
        self.ntfy_targets_table.setToolTip(
            "在 ntfy App 中订阅相同 topic 即可收到推送；私有主题需填写 token"
        )
        self.ntfy_targets_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.ntfy_targets_table)

        row = QHBoxLayout()
        btn_add = QPushButton("新增主题")
        btn_del = QPushButton("删除选中行")
        btn_add.clicked.connect(self.add_ntfy_target_row)
        btn_del.clicked.connect(self.delete_selected_ntfy_target_rows)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        row.addStretch(1)
        layout.addLayout(row)

        return group

    def _build_monitor_group(self) -> QGroupBox:
        group = QGroupBox("监听规则设置")
        grid = QGridLayout(group)

        self.check_interval_seconds = QSpinBox()
        self.check_interval_seconds.setRange(0, 3600)
        self.alert_cooldown_seconds = QSpinBox()
        self.alert_cooldown_seconds.setRange(0, 86400)
        self.expect_recording = QCheckBox("期望录制中（若停止则告警）")
        self.expect_streaming = QCheckBox("期望推流中（若停止则告警）")
        self.detect_audio_silence = QCheckBox("启用音频静音检测")
        self.audio_input_name = QLineEdit()
        self.silence_threshold_db = QDoubleSpinBox()
        self.silence_threshold_db.setRange(-120.0, 20.0)
        self.silence_threshold_db.setSingleStep(0.5)
        self.silence_seconds = QSpinBox()
        self.silence_seconds.setRange(1, 86400)
        self.silence_only_when_output_active = QCheckBox("仅在录制/推流活跃时检测静音")
        self.log_audio_volume = QCheckBox("输出音频音量日志")
        self.log_audio_volume_interval_seconds = QSpinBox()
        self.log_audio_volume_interval_seconds.setRange(0, 3600)
        self.meter_timeout_seconds = QSpinBox()
        self.meter_timeout_seconds.setRange(1, 600)
        self.startup_grace_seconds = QSpinBox()
        self.startup_grace_seconds.setRange(0, 600)
        self.audio_recheck_after_output_stop_seconds = QSpinBox()
        self.audio_recheck_after_output_stop_seconds.setRange(0, 86400)
        self.exit_after_record_stop_seconds = QSpinBox()
        self.exit_after_record_stop_seconds.setRange(0, 86400)

        widgets: list[tuple[str, str, QWidget]] = [
            ("检查间隔（秒）", "每隔多久检查一次 OBS 状态", self.check_interval_seconds),
            ("告警冷却（秒）", "同类告警最短发送间隔", self.alert_cooldown_seconds),
            ("音频输入名", "OBS 中要检测的输入名称", self.audio_input_name),
            ("静音阈值（dB）", "低于此音量会判定为静音区间", self.silence_threshold_db),
            ("静音持续（秒）", "连续低于阈值多少秒触发告警", self.silence_seconds),
            ("音量日志间隔（秒）", "输出音量日志的时间间隔", self.log_audio_volume_interval_seconds),
            ("电平超时（秒）", "超过该时间未收到电平则视为异常", self.meter_timeout_seconds),
            ("启动宽限（秒）", "连接后在此时间内忽略电平缺失", self.startup_grace_seconds),
            ("停止后复查延时（秒）", "录制/推流停止后延时复查音频，0 表示关闭", self.audio_recheck_after_output_stop_seconds),
            ("录制停止后退出（秒）", "曾在录制中，停止超过该秒数自动退出，0 表示不退出", self.exit_after_record_stop_seconds),
        ]

        checks = [
            self.expect_recording,
            self.expect_streaming,
            self.detect_audio_silence,
            self.silence_only_when_output_active,
            self.log_audio_volume,
        ]

        self.expect_recording.setToolTip("开启后：检测到未录制时会发送告警")
        self.expect_streaming.setToolTip("开启后：检测到未推流时会发送告警")
        self.detect_audio_silence.setToolTip("开启后：根据输入电平判断是否静音并告警")
        self.silence_only_when_output_active.setToolTip("开启后：只有录制或推流中才做静音检测")
        self.log_audio_volume.setToolTip("开启后：按设定间隔打印输入音量")

        row = 0
        for title, desc, widget in widgets:
            widget.setToolTip(desc)
            grid.addWidget(QLabel(self._label_with_desc(title, desc)), row, 0)
            grid.addWidget(widget, row, 1)
            row += 1

        for check in checks:
            grid.addWidget(check, row, 0, 1, 2)
            row += 1

        return group

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        btn_load = QPushButton("加载配置")
        btn_save = QPushButton("保存配置")
        self.btn_start = QPushButton("开始监听")
        self.btn_stop = QPushButton("停止监听")
        self.btn_stop.setEnabled(False)

        btn_load.clicked.connect(self.load_config_from_file)
        btn_save.clicked.connect(self.save_config_to_file)
        self.btn_start.clicked.connect(self.start_watchdog)
        self.btn_stop.clicked.connect(self.stop_watchdog)

        row.addWidget(btn_load)
        row.addWidget(btn_save)
        row.addStretch(1)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        return row

    def pick_config_path(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "选择配置文件",
            self.config_path_input.text().strip() or "config.json",
            "JSON Files (*.json);;All Files (*)",
        )
        if file_path:
            self.config_path_input.setText(file_path)
            self.config_path = Path(file_path)

    def add_target_row(self, device_key: str = "", code: str = "") -> None:
        row = self.targets_table.rowCount()
        self.targets_table.insertRow(row)
        self.targets_table.setItem(row, 0, QTableWidgetItem(device_key))
        self.targets_table.setItem(row, 1, QTableWidgetItem(code))

    def delete_selected_target_rows(self) -> None:
        selected_rows = sorted({item.row() for item in self.targets_table.selectedItems()}, reverse=True)
        for row in selected_rows:
            self.targets_table.removeRow(row)

    def add_ntfy_target_row(self, topic: str = "", token: str = "") -> None:
        row = self.ntfy_targets_table.rowCount()
        self.ntfy_targets_table.insertRow(row)
        self.ntfy_targets_table.setItem(row, 0, QTableWidgetItem(topic))
        self.ntfy_targets_table.setItem(row, 1, QTableWidgetItem(token))

    def delete_selected_ntfy_target_rows(self) -> None:
        selected_rows = sorted(
            {item.row() for item in self.ntfy_targets_table.selectedItems()},
            reverse=True,
        )
        for row in selected_rows:
            self.ntfy_targets_table.removeRow(row)

    def load_config_from_file(self) -> None:
        path_text = self.config_path_input.text().strip() or "config.json"
        self.config_path = Path(path_text)
        if not self.config_path.exists():
            self.append_log(f"[INFO] 配置文件不存在，将使用当前界面值：{self.config_path}")
            return
        try:
            obs_cfg, bark_cfg, ntfy_cfg, monitor_cfg = load_config(str(self.config_path))
        except Exception as exc:
            QMessageBox.critical(self, "加载失败", f"读取配置失败：{exc}")
            return

        self.set_obs_form(obs_cfg)
        self.set_bark_form(bark_cfg)
        self.set_ntfy_form(ntfy_cfg)
        self.set_monitor_form(monitor_cfg)
        self.append_log(f"[INFO] 已加载配置：{self.config_path}")

    def save_config_to_file(self) -> None:
        path_text = self.config_path_input.text().strip() or "config.json"
        self.config_path = Path(path_text)
        try:
            payload = self.build_raw_config()
            if self.config_path.parent and not self.config_path.parent.exists():
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.append_log(f"[INFO] 已保存配置：{self.config_path}")
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", f"保存配置失败：{exc}")

    def set_obs_form(self, cfg: ObsConfig) -> None:
        self.obs_host.setText(cfg.host)
        self.obs_port.setValue(cfg.port)
        self.obs_password.setText(cfg.password)
        self.obs_timeout.setValue(cfg.timeout_seconds)

    def set_bark_form(self, cfg: BarkConfig) -> None:
        self.bark_server.setText(cfg.server)
        self.bark_group.setText(cfg.group)
        self.bark_sound.setText(cfg.sound)
        self.targets_table.setRowCount(0)
        for target in cfg.targets:
            self.add_target_row(target.device_key, target.code)

    def set_ntfy_form(self, cfg: NtfyConfig) -> None:
        self.ntfy_server.setText(cfg.server)
        self.ntfy_priority.setValue(cfg.priority)
        self.ntfy_tags.setText(",".join(cfg.tags))
        self.ntfy_targets_table.setRowCount(0)
        for target in cfg.targets:
            self.add_ntfy_target_row(target.topic, target.token)

    def set_monitor_form(self, cfg: MonitorConfig) -> None:
        self.check_interval_seconds.setValue(cfg.check_interval_seconds)
        self.alert_cooldown_seconds.setValue(cfg.alert_cooldown_seconds)
        self.expect_recording.setChecked(cfg.expect_recording)
        self.expect_streaming.setChecked(cfg.expect_streaming)
        self.detect_audio_silence.setChecked(cfg.detect_audio_silence)
        self.audio_input_name.setText(cfg.audio_input_name)
        self.silence_threshold_db.setValue(cfg.silence_threshold_db)
        self.silence_seconds.setValue(cfg.silence_seconds)
        self.silence_only_when_output_active.setChecked(cfg.silence_only_when_output_active)
        self.log_audio_volume.setChecked(cfg.log_audio_volume)
        self.log_audio_volume_interval_seconds.setValue(cfg.log_audio_volume_interval_seconds)
        self.meter_timeout_seconds.setValue(cfg.meter_timeout_seconds)
        self.startup_grace_seconds.setValue(cfg.startup_grace_seconds)
        self.audio_recheck_after_output_stop_seconds.setValue(cfg.audio_recheck_after_output_stop_seconds)
        self.exit_after_record_stop_seconds.setValue(cfg.exit_after_record_stop_seconds)

    def build_config_objects(self) -> tuple[ObsConfig, BarkConfig, NtfyConfig, MonitorConfig]:
        obs_cfg = ObsConfig(
            host=self.obs_host.text().strip(),
            port=int(self.obs_port.value()),
            password=self.obs_password.text(),
            timeout_seconds=int(self.obs_timeout.value()),
        )
        targets: list[BarkTarget] = []
        for row in range(self.targets_table.rowCount()):
            device_key_item = self.targets_table.item(row, 0)
            code_item = self.targets_table.item(row, 1)
            device_key = (device_key_item.text() if device_key_item else "").strip()
            code = (code_item.text() if code_item else "").strip()
            if device_key and code:
                targets.append(BarkTarget(device_key=device_key, code=code))

        bark_cfg = BarkConfig(
            server=self.bark_server.text().strip(),
            targets=targets,
            group=self.bark_group.text().strip() or "OBS Watchdog",
            sound=self.bark_sound.text().strip() or "bell",
        )

        ntfy_targets: list[NtfyTarget] = []
        for row in range(self.ntfy_targets_table.rowCount()):
            topic_item = self.ntfy_targets_table.item(row, 0)
            token_item = self.ntfy_targets_table.item(row, 1)
            topic = (topic_item.text() if topic_item else "").strip()
            token = (token_item.text() if token_item else "").strip()
            if topic:
                ntfy_targets.append(NtfyTarget(topic=topic, token=token))

        tags = [t.strip() for t in self.ntfy_tags.text().split(",") if t.strip()]
        ntfy_cfg = NtfyConfig(
            server=self.ntfy_server.text().strip() or "https://ntfy.sh",
            targets=ntfy_targets,
            priority=int(self.ntfy_priority.value()),
            tags=tags or ["warning", "obs"],
        )

        monitor_cfg = MonitorConfig(
            check_interval_seconds=int(self.check_interval_seconds.value()),
            alert_cooldown_seconds=int(self.alert_cooldown_seconds.value()),
            expect_recording=bool(self.expect_recording.isChecked()),
            expect_streaming=bool(self.expect_streaming.isChecked()),
            detect_audio_silence=bool(self.detect_audio_silence.isChecked()),
            audio_input_name=self.audio_input_name.text().strip(),
            silence_threshold_db=float(self.silence_threshold_db.value()),
            silence_seconds=int(self.silence_seconds.value()),
            silence_only_when_output_active=bool(self.silence_only_when_output_active.isChecked()),
            log_audio_volume=bool(self.log_audio_volume.isChecked()),
            log_audio_volume_interval_seconds=int(self.log_audio_volume_interval_seconds.value()),
            meter_timeout_seconds=int(self.meter_timeout_seconds.value()),
            startup_grace_seconds=int(self.startup_grace_seconds.value()),
            audio_recheck_after_output_stop_seconds=int(
                self.audio_recheck_after_output_stop_seconds.value()
            ),
            exit_after_record_stop_seconds=int(self.exit_after_record_stop_seconds.value()),
        )
        return obs_cfg, bark_cfg, ntfy_cfg, monitor_cfg

    def build_raw_config(self) -> dict[str, Any]:
        obs_cfg, bark_cfg, ntfy_cfg, monitor_cfg = self.build_config_objects()
        return {
            "obs": asdict(obs_cfg),
            "bark": {
                "server": bark_cfg.server,
                "targets": [asdict(t) for t in bark_cfg.targets],
                "group": bark_cfg.group,
                "sound": bark_cfg.sound,
            },
            "ntfy": {
                "server": ntfy_cfg.server,
                "targets": [asdict(t) for t in ntfy_cfg.targets],
                "priority": ntfy_cfg.priority,
                "tags": ntfy_cfg.tags,
            },
            "monitor": asdict(monitor_cfg),
        }

    def start_watchdog(self) -> None:
        if self.worker_thread is not None and self.worker_thread.is_alive():
            QMessageBox.information(self, "提示", "监听已在运行中。")
            return

        self.save_config_to_file()
        obs_cfg, bark_cfg, ntfy_cfg, monitor_cfg = self.build_config_objects()

        self.stop_event = threading.Event()

        def worker() -> None:
            writer = QueueWriter(self.log_queue)
            notifier = AlertNotifier(bark_cfg, ntfy_cfg)
            watcher = ObsWatchdog(obs_cfg, monitor_cfg, notifier)
            with redirect_stdout(writer), redirect_stderr(writer):
                try:
                    watcher.run_forever(stop_event=self.stop_event)
                except Exception as exc:
                    print(f"[ERROR] 监听线程异常退出: {exc}")
                finally:
                    print("[INFO] 监听线程已结束。")

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.append_log("[INFO] 已启动监听线程。")

    def stop_watchdog(self) -> None:
        if self.stop_event is None:
            return
        self.stop_event.set()
        self.append_log("[INFO] 正在请求停止监听...")
        self.btn_stop.setEnabled(False)

    def flush_log_queue(self) -> None:
        has_new_text = False
        while True:
            try:
                chunk = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_output.moveCursor(self.log_output.textCursor().MoveOperation.End)
            self.log_output.insertPlainText(chunk)
            has_new_text = True
        if has_new_text:
            self.log_output.moveCursor(self.log_output.textCursor().MoveOperation.End)

        if self.worker_thread is not None and not self.worker_thread.is_alive():
            self.worker_thread = None
            self.stop_event = None
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)

    def append_log(self, text: str) -> None:
        self.log_output.appendPlainText(text)


def main() -> None:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
