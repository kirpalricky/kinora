# Patterns and recipes

## Tests
`tests/run_all.sh` runs all offline tests (unittest modules, then `test_prefetch.js` if node exists). It uses `$PY`, else `.venv/bin/python`, else the main checkout's venv (so it works from a worktree). None of them contact a camera. `tools/probe.py` is deliberately not in the suite.
| Test | Covers |
|---|---|
| `test_gate.py` | login gate in a temp dir |
| `test_listings_export.py` | listing cache, export planning, merge (real ffmpeg) |
| `test_slots.py`, `test_slots_register.py` | slot ownership; play and live register into the shared singleton |
| `test_session_lock.py` | control-session serialisation |
| `test_errors.py` | domain error to HTTP mapping |
| `test_live_argv.py` | the RTSP password is not in ffmpeg's argv; the temp list file is 0600 and deleted |
| `test_udt.py` | UDT loopback with 5% loss |
| `test_p2p_setup.py` | P2P day-listing order |
| `test_play.py` | HLS playback with a fake camera and real ffmpeg |
| `test_prefetch.js` | prefetch and fast-forward policy |

## Recipes
- **Add a camera method** (a new pytapo call): call it through `await session.api(cam, lambda t: t.method(...))` from a service. Never build a `Tapo(...)` yourself and never retry a login: the gate counts every login.
- **Add a service**: new module in `kinora/services/`; take its dependencies as arguments (no imports of `server`); raise `CameraError` subclasses (`BadInput`, `Unavailable`...), never `web.HTTP*`; if it uses the video slot, go through `slots`. Add a handler and a route in `kinora/server.py`.
- **Add an HTTP route**: handler in `kinora/server.py`, entry in the `app.add_routes` block; errors that are `CameraError` are mapped by the `camera_errors` middleware; `slots.Busy` must be mapped explicitly (503).
- **Add a camera flag**: add the key in `cameras.json`, expose it in the `cameras` handler (`kinora/server.py`), read it in `app.js`, document it in `docs/codemap/frontend.md`.
- **Change the page**: edit `kinora/web/static/app.js` or `kinora/web/index.html`, bump the `?v=` on the script tag, hard-reload the browser. Keep the policy blocks pure and run `node tests/test_prefetch.js`.

## Rules that bite
- Camera credentials never in files or chat; read from the environment and the Keychain (`creds.py`).
- Never view camera video or thumbnails (see `docs/OPERATIONS.md`, Privacy).
- A camera error code of `-40105` (unknown method) drops the session; do not experiment with unknown methods.
- Anything that logs in needs the first-use checklist in `docs/OPERATIONS.md` and a look at `/api/status` first.
