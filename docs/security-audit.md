# Data-leak audit (2026-11-21)

Goal: make sure nothing in this app can send camera video, thumbnails, credentials or camera metadata off the machine.

## Findings
- **hls.js 1.5.17** (`static/hls.min.js`, 413,952 bytes, sha512 equals cdnjs's published hash). Static scan: no `sendBeacon`, `WebSocket`,
  `new Image`, `importScripts`, `eval`, `new Function`, `localStorage` or `document.cookie`. Only `XMLHttpRequest`/`fetch` (its segment loaders,
  which request only the URLs the page gives it). Its only two URLs are namespace identifiers (`aomedia.org/emsg/ID3`, `w3.org/ns/ttml`).
  Now served locally instead of from a CDN, so nothing is loaded from a third party at runtime.
- **pytapo 3.4.34**: every connection target is the camera host (`self.host`, control port 443, stream port 8800). External URLs in the code are
  comments / issue links only. No telemetry or TP-Link cloud hostnames. The cloud password is used locally to derive the login for the camera.
- **python-kasa 0.12.0, rtp, other pytapo dependencies**: only used for transports/helpers; external URLs found are licence and documentation links.
  No outbound connection is initiated at import time by anything we use.
- **Page**: no third-party scripts, fonts or images. `static/app.js` holds all page code (no inline scripts/handlers).

## Enforced by the server (server.py `local_only` middleware)
- Bound to 127.0.0.1; `Host` must be `localhost:8080` or `127.0.0.1:8080` (anti DNS-rebinding); cross-site `Sec-Fetch-Site` refused.
- `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:;
  connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`
  so even a compromised script cannot send data to another origin from the page.
- `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `Cross-Origin-Resource-Policy: same-origin`.

## Not covered / how to verify at runtime
- Static review is not proof. While the app is in use, check connections: `lsof -nP -i -a -p $(pgrep -f server.py)` and
  `lsof -nP -i -a -c ffmpeg`. Expected: 127.0.0.1 listeners and 192.168.1.x peers only.
- Python packages have not been scanned for known CVEs (e.g. `pip-audit`); ffmpeg is the Homebrew build.
- Resolved: the RTSP URL is no longer in the process list. The live relay passes it in a private 0600 ffconcat temp file (deleted ~6 s after start, stale ones swept at startup); `tests/test_live_argv.py` checks argv. Camera password lookup: `python -m kinora.creds status` prints which sources exist (never values); `set` stores it in the OS keyring via getpass.
- CSP does not stop page navigation to another site (no `navigate-to`), only fetch/XHR/img/script/etc.
