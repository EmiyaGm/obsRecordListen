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

## 4) 命令行脚本用法（`monitor_obs.py`）

```bash
python monitor_obs.py --config config.json
```

Windows（PowerShell）：

```powershell
py monitor_obs.py --config config.json
```

仅列出 OBS 输入源（用于确认 `audio_input_name`）：

```bash
python monitor_obs.py --config config.json --list-sources
```

Windows（PowerShell）：

```powershell
py monitor_obs.py --config config.json --list-sources
```

建议将该进程后台常驻运行（如 Windows 任务计划、tmux、systemd、launchd）。

## 5) 图形界面用法（`app_gui.py`）

启动 GUI：

```bash
python app_gui.py
```

Windows（PowerShell）：

```powershell
py app_gui.py
```

界面功能：

- 编辑并保存 `obs` / `bark` / `monitor` 配置
- 在 Bark 目标表格中新增/删除 `device_key + code`
- 点击“开始监听”后后台线程启动监控
- 点击“停止监听”可优雅停止
- 底部日志窗口实时显示运行输出

建议流程：

1. 先“选择配置文件”并“加载配置”（或新建后保存）
2. 检查 `audio_input_name` 是否与 OBS 输入名一致
3. 点击“开始监听”，观察底部日志
4. 修改参数后先“保存配置”，再重启监听

## 6) 打包为 Windows EXE（PyInstaller）

先安装依赖：

```powershell
pip install pyinstaller pyside6 requests websocket-client
```

在项目根目录执行：

```powershell
pyinstaller --noconfirm --clean --windowed --onefile --name OBSWatchdogGUI --add-data "config.json;." app_gui.py
```

说明：

- 若没有 `config.json`，可先去掉 `--add-data "config.json;."`
- 打包产物位置：`dist\OBSWatchdogGUI.exe`
- `--windowed` 会隐藏控制台窗口，适合 GUI 程序

## 7) 说明

- 静音检测基于 OBS 实时音量事件（`InputVolumeMeters`）。
- 可通过调整 `silence_threshold_db` 与 `silence_seconds` 减少误报。
- 告警有冷却时间（`monitor.alert_cooldown_seconds`），用于避免频繁推送。