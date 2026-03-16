# OBS Record Watchdog (Bark Alert)

Monitor OBS recording/stream status and send Bark alerts when:

- OBS disconnects (websocket unavailable)
- recording or streaming unexpectedly stops
- audio appears silent for too long

## 1) Prerequisites

- Python 3.10+
- OBS Studio with `obs-websocket` enabled (OBS 28+ has it built in)
- Bark app + Bark device key

In OBS:

1. `Tools -> WebSocket Server Settings`
2. Enable server
3. Remember host/port/password

## 2) Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3) Configure

Copy config template:

```bash
cp config.example.json config.json
```

Windows (PowerShell):

```powershell
Copy-Item config.example.json config.json
```

Then edit `config.json`.

Important fields:

- `obs.host`, `obs.port`, `obs.password`: OBS websocket info
- `bark.targets`: Bark target list, each item is `{ device_key, code }` and is one-to-one
- `monitor.expect_recording`: alert if recording is not active
- `monitor.expect_streaming`: alert if streaming is not active
- `monitor.detect_audio_silence`: enable silence detection
- `monitor.audio_input_name`: OBS audio input name to monitor
- `monitor.silence_threshold_db`: dB threshold (lower means quieter), e.g. `-50`
- `monitor.silence_seconds`: how long below threshold is considered abnormal

## 4) Run

```bash
python monitor_obs.py --config config.json
```

Windows (PowerShell):

```powershell
py monitor_obs.py --config config.json
```

Keep this process running in background (tmux/systemd/launchd are recommended).

## 5) Notes

- Silence detection is based on OBS input volume (`GetInputVolume`).
- Adjust `silence_threshold_db` and `silence_seconds` to reduce false positives.
- Alerts use cooldown (`monitor.alert_cooldown_seconds`) to avoid spam.