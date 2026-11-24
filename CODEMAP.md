# CODEMAP
> This is the index. Open the file you need; don't grep for it.

## Project shape
A local aiohttp server plus a single-page UI that plays back the SD cards of three Tapo cameras (Baby Room C100, Living Room C210, Kitchen C200) over the Tapo local API, with a P2P/UDT path for the C100. No database: state is files under `cache/` and `exports/`. Login safety (the gate) is the most important invariant.

## Where things live
| Concern | Path | Drill-down doc |
|---------|------|----------------|
| HTTP routes, middleware, clip/thumb handlers | `kinora/server.py` (root `server.py` is a dev shim) | `docs/codemap/backend.md` |
| Other devices (allowed list, sharing, network socket, waiting list) | `kinora/devices.py`, `kinora/services/devices_api.py`, Settings pane in `kinora/web/static/setup.js` | `docs/codemap/backend.md`, `docs/codemap/frontend.md` |
| Camera table, paths, tunables | `kinora/config.py`, `cameras.json` | `docs/codemap/backend.md`, `docs/codemap/frontend.md` (flags) |
| Login gate, pytapo patch, sessions | `kinora/camera/{gate,patch,session}.py` | `docs/codemap/backend.md` |
| Video slot arbitration (clip, play, live) | `kinora/camera/slots.py` | `docs/codemap/backend.md` |
| Domain errors and HTTP mapping | `kinora/errors.py` | `docs/codemap/backend.md` |
| Day listings and events cache | `kinora/services/listings.py` | `docs/codemap/backend.md` |
| Streamed playback (HLS), fast-forward | `kinora/services/play.py`, `kinora/services/hls.py` | `docs/codemap/backend.md` |
| Live view (RTSP -> HLS) | `kinora/services/live.py` | `docs/codemap/backend.md` |
| Export, pan/tilt | `kinora/services/{export,ptz}.py` | `docs/codemap/backend.md` |
| P2P/UDT transport for the C100 | `kinora/{p2p,p2p_media,udt}.py` | `docs/codemap/backend.md` |
| Health checks, credentials | `kinora/{health,creds}.py` | `docs/codemap/backend.md` |
| `kinora` command, `doctor`, browser open | `kinora/cli.py` | `docs/codemap/backend.md` |
| Packaging (wheel, entry point, package data) | `pyproject.toml`, `tests/package_smoke.sh` | `docs/OPERATIONS.md` |
| Web UI (package data) | `kinora/web/index.html`, `kinora/web/static/app.js` | `docs/codemap/frontend.md` |
| Tests | `tests/` | `docs/codemap/patterns.md` |
| Running safely, first-use checklist | | `docs/OPERATIONS.md` |
| Why things are the way they are (dated) | | `docs/pytapo-notes.md`, `docs/p2p-hls-design.md` |

## Common tasks -> docs to read
| Task | Read these |
|------|-----------|
| Fix or change a route | `backend.md` (Routes, Invariants) |
| Anything that logs in to a camera | `OPERATIONS.md`, `backend.md` (login gate) |
| Playback does not start | `backend.md` (Play), `cache/play/<cam>/<sid>/play.log` |
| Baby Room (C100) problems | `backend.md` (P2P) |
| Fast-forward or speed change | `frontend.md` (ff-policy), `backend.md` (Play) |
| Page change | `frontend.md`, `patterns.md` (Change the page) |
| Add a flag or camera | `frontend.md` (cameras.json keys), `patterns.md` |
| Add a service | `patterns.md` (Recipes) |
| Run or write tests | `patterns.md` (Tests) |
| Lockouts, status, restarts | `OPERATIONS.md` |

## Keeping this index honest
Run `/audit-codemap` to detect drift. Update the matching `docs/codemap/*.md` when you add, move or delete a module, route or camera flag.
