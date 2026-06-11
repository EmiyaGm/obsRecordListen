# OBS 录制监控（Bark / ntfy 告警）

用于监控 OBS 的录制/推流状态，并在以下情况通过 Bark 和/或 ntfy 发送告警：

- OBS 断连（WebSocket 不可用）
- 录制或推流意外停止
- 音频输入长时间接近静音

Bark 适合 iOS；[ntfy](https://ntfy.sh/) 适合 Android（也可跨平台）。两者可同时配置，告警会并行推送到所有已配置的目标。

## 1) 前置条件

- Python 3.10+
- 已开启 `obs-websocket` 的 OBS Studio（OBS 28+ 内置）
- 推送方式（至少配置一种）：
  - **Bark**：Bark App 与设备 key（iOS 常用）
  - **ntfy**：ntfy App 与订阅 topic（Android 常用）

在 OBS 中：

1. 打开 `工具 -> WebSocket 服务器设置`
2. 启用服务器
3. 记住 host / port / password

### ntfy（Android）快速配置

1. 在 Google Play 或 F-Droid 安装 [ntfy](https://ntfy.sh/) App
2. 在 App 中点击右上角 `+`，**Subscribe to topic**（订阅主题）
3. 输入一个不易被猜到的主题名，例如 `obs-watchdog-你的随机字符串`
4. 将同一主题名填入本项目的 `ntfy.targets`（见下文配置说明）
5. 保存配置并启动监控；在 App 对应主题下应能收到测试告警

> 主题名相当于「频道密码」：使用公共服务器 `https://ntfy.sh` 时，请选用足够随机、他人难以猜到的 topic，避免陌生人向你的频道发消息。若需更高安全性，可使用 ntfy 的 Access tokens 或自建服务器。

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

### OBS 与监控

- `obs.host`、`obs.port`、`obs.password`：OBS WebSocket 连接信息
- `monitor.expect_recording`：期望录制中；若未录制则告警
- `monitor.expect_streaming`：期望推流中；若未推流则告警
- `monitor.detect_audio_silence`：是否启用静音检测
- `monitor.audio_input_name`：要监控的 OBS 音频输入名称
- `monitor.silence_threshold_db`：静音阈值（dB，越小越安静），例如 `-50`
- `monitor.silence_seconds`：低于阈值持续多久视为异常
- `monitor.log_audio_volume`：是否在控制台打印当前音量（用于调试）
- `monitor.log_audio_volume_interval_seconds`：音量打印间隔（秒）

### Bark（iOS 等）

- `bark.server`：Bark 服务地址，默认 `https://api.day.app`
- `bark.targets`：目标列表，每项为 `{ "device_key", "code" }`，一一对应
- `bark.group`：通知分组名
- `bark.sound`：通知声音，例如 `bell`

不需要 Bark 时，将 `bark.targets` 留空 `[]` 即可。

### ntfy（Android 等）

- `ntfy.server`：ntfy 服务地址，默认 `https://ntfy.sh`；自建服务器填你的实例 URL
- `ntfy.targets`：主题列表，每项为 `{ "topic", "token" }`
  - `topic`：与手机 App 中订阅的主题名一致（必填）
  - `token`：私有主题的访问令牌（可选，公共随机 topic 通常留空）
- `ntfy.priority`：优先级 `1`（最低）～`5`（紧急），默认 `4`（高）。也支持字符串：`min` / `low` / `default` / `high` / `max`
- `ntfy.tags`：标签数组，会显示在通知上，例如 `["warning", "obs"]`

不需要 ntfy 时，将 `ntfy.targets` 留空 `[]` 即可。

配置示例（仅 ntfy、不用 Bark）：

```json
{
  "bark": {
    "server": "https://api.day.app",
    "targets": [],
    "group": "OBS Watchdog",
    "sound": "bell"
  },
  "ntfy": {
    "server": "https://ntfy.sh",
    "targets": [
      { "topic": "obs-watchdog-xK9mP2qR", "token": "" }
    ],
    "priority": 4,
    "tags": ["warning", "obs"]
  }
}
```

手动测试 ntfy 是否通畅（将 topic 换成你的）：

```bash
curl -H "Title: OBS 测试" -d "这是一条测试消息" https://ntfy.sh/你的topic
```

手机 ntfy App 订阅同一 topic 后，应立刻收到通知。

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

- 编辑并保存 `obs` / `bark` / `ntfy` / `monitor` 配置
- 在 Bark 目标表格中新增/删除 `device_key + code`
- 在 ntfy 主题表格中新增/删除 `topic`（及可选 `token`）
- 点击「开始监听」后后台线程启动监控
- 点击「停止监听」可优雅停止
- 底部日志窗口实时显示运行输出

建议流程：

1. 先「选择配置文件」并「加载配置」（或新建后保存）
2. 检查 `audio_input_name` 是否与 OBS 输入名一致
3. 在 ntfy 区域填写与手机 App 相同的 topic
4. 点击「开始监听」，观察底部日志
5. 修改参数后先「保存配置」，再重启监听

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
- Bark 与 ntfy 可同时启用；至少配置其中一种的推送目标，否则告警只会打印到控制台。
