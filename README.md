# Kinora

Kinora is a local web app for scrubbable playback of the SD cards in Tapo (TP-Link) cameras, instead of only the live RTSP feed.
Nothing leaves the LAN and no TP-Link cloud session is used for video.

## What it does
- Day timeline per camera with continuous recordings and event clips; click to play, scrub, change speed.
- Playback by downloading short windows over the Tapo local API, or by HLS streaming for cameras that cannot download (Baby Room, over a local P2P/UDT channel).
- Key-frame fast-forward (4x, 8x, 16x) streamed straight from the camera.
- Live view (RTSP relayed to HLS), pan/tilt, and export of video or ASR-ready audio with a wall-clock mapping.
- A login gate that stops the cameras from locking out after failed logins.

## Install
```bash
pipx install kinora-cams   # placeholder distribution name: the final PyPI name is not checked yet
kinora                     # starts the server and opens http://localhost:8080 on the setup wizard
```
Kinora needs FFmpeg on your PATH (macOS: `brew install ffmpeg`; Debian/Ubuntu: `sudo apt install ffmpeg`). Run `kinora doctor` for a plain checklist (Python, data folder, ffmpeg, keyring, port); it never contacts a camera. Options: `--port N`, `--no-browser`, `--home PATH`, `--allow IP`, `--host ADDR`, `--version`.

**Use Kinora from other laptops:** open Settings (the gear), choose **Other devices**, and turn sharing **On**. Kinora then listens on this computer's home-network address (not the whole network). Open the address it shows on the other laptop, and allow that laptop here: when it tries to connect, it appears under *Waiting for approval* and you press **Allow**. Under the address, **Show QR code** draws a code that a phone camera can scan. Other laptops can watch recordings, go live and export, but cannot change settings or passwords, which stay on the server's own computer. Devices that are not allowed get a 403 with a short message. Only allow a device you recognise: anyone on your network can ask. Devices are allowed by IP address, so a device that takes over an allowed address gets that device's access. There is no login and traffic is plain HTTP, so only turn sharing on for a network you trust.

*Advanced:* `kinora --allow 192.168.1.20 --allow 192.168.1.21` (IPs or ranges like `192.168.1.0/24`, repeatable; or `KINORA_ALLOW=ip,ip`), or `--host ADDR`, fixes the list and the listening address when Kinora starts. Those entries are shown read-only in Settings, and the sharing switch then stays as the command line set it.

**Windows:** run Kinora under [WSL](https://learn.microsoft.com/windows/wsl/install) (`wsl --install` in an admin PowerShell, then open the Ubuntu shell) and follow the Linux steps above (`sudo apt install ffmpeg`, `pipx install ...`, `kinora`). Open http://localhost:8080 in your Windows browser. Your cameras must be reachable from WSL: if they are not, enable WSL's mirrored networking (`networkingMode=mirrored` in `%UserProfile%\.wslconfig`, then `wsl --shutdown`).

## Run from a checkout
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python server.py  # http://localhost:8080
```
On first start the page opens a setup wizard: it asks for your TP-Link login and each camera's IP and camera account, then verifies each camera with a single login. Passwords are stored in the OS keyring (or an opt-in private file), never in `cameras.json`. Cameras can be edited afterwards under Settings (for example a changed IP).
Your data (cameras, settings, cache, exports) lives in the per-user data folder (`KINORA_HOME` overrides it); `cameras.example.json` shows the file format. Read [docs/OPERATIONS.md](docs/OPERATIONS.md) before the first run: the cameras lock out for 30 minutes after repeated failed logins, and the wizard's first-run notes are there.

## Layout
```
server.py              dev shim for kinora/server.py (routes, middleware, startup)
kinora/web/           single-page UI (index.html, static/app.js), shipped in the wheel
kinora/cli.py         the `kinora` command
pyproject.toml        packaging (version from kinora.config.VERSION; requirements.txt is the pinned lock)
kinora/               camera access, P2P/UDT, services (see CODEMAP.md)
tests/run_all.sh       the offline test suite (no camera needed)
tools/probe.py         one-off camera probe (do not use while debugging a lockout)
docs/                  OPERATIONS.md, design notes
```
Start with [CODEMAP.md](CODEMAP.md) to find code.

## Tests
```bash
tests/run_all.sh
```

## Disclaimer

Kinora is unofficial software. It is not affiliated with, endorsed by or supported by TP-Link. "Tapo" and "TP-Link" are trademarks of their owner and are used here only to describe which cameras Kinora is compatible with. TP-Link may change or break third-party access at any time. Use at your own risk.

## Licence

MIT, see [LICENSE](LICENSE). Third-party components and their licences are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
