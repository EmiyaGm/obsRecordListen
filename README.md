# OBS 录制监控（Bark 告警）

用于监控 OBS 的录制/推流状态，并在以下情况通过 Bark 发送告警：

- OBS 断连（WebSocket 不可用）
- 录制或推流意外停止
- 音频输入长时间接近静音

## 1) 前置条件

- Python 3.10+
- 已开启 `obs-websocket` 的 OBS Studio（OBS 28+ 内置）
- Bark App 与 Bark 设备 key

在 OBS 中：

1. 打开 `工具 -> WebSocket 服务器设置`
2. 启用服务器
3. 记住 host / port / password

## 2) 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows（PowerShell）：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3) 配置

先复制配置模板：

```bash
cp config.example.json config.json
```

Windows（PowerShell）：

```powershell
Copy-Item config.example.json config.json
```

然后编辑 `config.json`。

关键字段说明：

- `obs.host`、`obs.port`、`obs.password`：OBS WebSocket 连接信息
- `bark.targets`：Bark 目标列表，每项为 `{ device_key, code }`，一一对应
- `monitor.expect_recording`：期望录制中；若未录制则告警
- `monitor.expect_streaming`：期望推流中；若未推流则告警
- `monitor.detect_audio_silence`：是否启用静音检测
- `monitor.audio_input_name`：要监控的 OBS 音频输入名称
- `monitor.silence_threshold_db`：静音阈值（dB，越小越安静），例如 `-50`
- `monitor.silence_seconds`：低于阈值持续多久视为异常
- `monitor.log_audio_volume`：是否在控制台打印当前音量（用于调试）
- `monitor.log_audio_volume_interval_seconds`：音量打印间隔（秒）

## 4) 运行

```bash
python monitor_obs.py --config config.json
```

Windows（PowerShell）：

```powershell
py monitor_obs.py --config config.json
```

建议将该进程后台常驻运行（如 tmux / systemd / launchd）。

## 5) 说明

- 静音检测基于 OBS 输入音量接口（`GetInputVolume`）。
- 可通过调整 `silence_threshold_db` 与 `silence_seconds` 减少误报。
- 告警有冷却时间（`monitor.alert_cooldown_seconds`），用于避免频繁推送。