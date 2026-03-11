# OBS Record Watchdog (Bark Alert)

Monitor OBS recording/stream status and send Bark alerts when:

- OBS disconnects (websocket unavailable)
- recording or streaming unexpectedly stops
- video appears stalled (same screenshot hash for too long)

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

## 3) Configure

Copy config template:

```bash
cp config.example.json config.json
```

Then edit `config.json`.

Important fields:

- `obs.host`, `obs.port`, `obs.password`: OBS websocket info
- `bark.targets`: Bark target list, each item is `{ device_key, code }` and is one-to-one
- `monitor.expect_recording`: alert if recording is not active
- `monitor.expect_streaming`: alert if streaming is not active
- `monitor.detect_stall`: enable frozen-frame detection
- `monitor.stall_source_name`: source/scene name to screenshot for stall detection
- `monitor.stall_seconds`: how long unchanged screenshot is considered stalled

## 4) Run

```bash
python monitor_obs.py --config config.json
```

Keep this process running in background (tmux/systemd/launchd are recommended).

## 5) Notes

- Stall detection is hash-based screenshot comparison; fully static scenes can trigger false positives.
- If your content is naturally static, increase `stall_seconds` or disable `detect_stall`.
- Alerts use cooldown (`monitor.alert_cooldown_seconds`) to avoid spam.