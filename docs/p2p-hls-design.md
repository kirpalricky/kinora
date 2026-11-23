# Design: HLS streaming playback for P2P-only cameras (Baby Room, C100 v2)

Status: DESIGN FOR REVIEW (2026-11-22). Nothing in this document is implemented yet.

## Why
Baby Room serves SD-card playback only through the Tapo app's P2P channel (UDT). We can already fetch one recording window
(`p2p_media.fetch_window`) but the camera streams at **strict 1x real time** (30 s of media = 30-48 s; first video after ~1.3 s).
With the existing "download a window, then play the mp4" design, a scrub waits ~20 s and each next piece only just keeps up.
Goal: playback starts in ~3-5 s, continues across recordings by itself (the camera rolls on past `end_time` into the next recordings),
a scrub/click restarts at the new time, no per-piece P2P setup, no extra logins.

## Facts the design relies on (all verified live on Baby Room unless marked)
- One P2P session = local `request_p2p` (needs a day listing for the date first, same login/user id) + UDP probe + UDT (`udt.py`) + loopback `p2p.Bridge`
  + pytapo `HttpMediaSession` pointed at 127.0.0.1:<port>. Setup takes ~0.6 s and costs NO login when the server's pytapo session is already alive.
- Playback request (`playback` get, then `play` do with `X-Session-Id`) streams MPEG-TS parts: `resp.plaintext` = TS bytes with video; `resp.audioPayload` = raw G.711 A-law extracted by pytapo's TSReader (8 kHz).
  pytapo's `Convert` merges them offline with ffmpeg: video as `-f mpegts`, audio as `-f alaw -ar 8000`, `-c:v copy -c:a aac`.
- The stream does not stop at end_time; it rolls into following recordings (PTS continuous). 1x only (`scale` 4/1, 16/1 = key frames only; `download` unsupported on C100).
- The camera serves ~2 simultaneous video streams, shared with the phone app and our Live RTSP relay.
- pytapo's media-session handler acks the camera's data window (`X-Data-Received`) only while we consume the response queue: a slow consumer stalls the camera.
- Login gate (server.py): >=180 s between logins per camera, 4/h; `client(cam)` is the single long-lived gated session; `api(cam, fn)` serialises blocking pytapo calls per camera.
- Existing live relay (`live.py`): ffmpeg RTSP->HLS (copy video, AAC), `hls_list_size 5` + `delete_segments`, pidfile + orphan kill, idle reaper 45 s, `/live/<cam>/<file>` route, the page uses hls.js (`liveSyncDurationCount:2`).

## Proposed architecture
### Server: new module `p2p_play.py`
One `PlaySession` per camera at most.

`start(cam, ts)` (POST `/api/play/start?cam=c199&ts=<epoch>`):
1. Stop any existing play session for the camera, and the RTSP live relay of that camera (stream slots).
2. `t = client(cam)` (gated; same behaviour as other routes). Day listing for the day of `ts` and the next day (cached per login in `t._p2p_listed`).
3. Create `cache/play/<cid>/<sid>/`, spawn ffmpeg, spawn the feeder task, return `{"sid":..., "playlist": "/play/<cid>/<sid>/index.m3u8", "start": ts}` immediately
   (the page then polls `/api/play/status` while the playlist is not there yet).

ffmpeg process (stdin = video TS, fd 3 = raw A-law audio):
```
ffmpeg -v error -f mpegts -i pipe:0 -f alaw -ar 8000 -ac 1 -i pipe:3 -map 0:v -map 1:a -c:v copy -c:a aac -b:a 64k
       -f hls -hls_time 2 -hls_playlist_type event -hls_flags independent_segments -hls_segment_filename seg_%05d.ts index.m3u8
```
(`event` playlist = growing, nothing deleted until the session ends, so the viewer can pause / step back within the session. Alternative: live-style sliding window.)

Feeder task (`_run`): `request_session` -> `punch` -> `UdtStream.connect` -> Bridge -> `HttpMediaSession` -> step 1 `playback get` -> on first response with session id send step 2 `play do` ->
loop over `gen`: `video/mp2t` -> `proc.stdin.write(plaintext)`; `resp.audioPayload` -> audio pipe; drain with a bounded wait; JSON error codes / `stream_status finished` end the loop.
Ends on: ffmpeg death, camera stream end, stall (no data for 20 s), hard cap (e.g. 4 h), `stop()`, server shutdown. Cleanup always: close stdin + audio fd, terminate ffmpeg, `await bridge.close()` (UDT shutdown), delete the session directory.

`stop(cam)`, `stop_all()`, `reaper()` (no playlist/segment request for 45 s -> stop, like `live.py`), `pidfile` + `kill_orphans()` at startup (like `live.py`).

### Server routes (server.py)
- `POST /api/play/start`, `POST /api/play/stop`, `GET /api/play/status?cam=` (state starting|playing|ended|error, message, last codes, started_at, wall-clock of the newest chunk if known),
  `GET /play/{cam}/{sid}/{file}` (file regex `^(index\.m3u8|seg_\d{5}\.ts)$`, sid regex, no traversal, `Cache-Control: no-store`).
- Camera flag in `cameras.json`: `"stream_playback": true` (c199). `/api/cameras` exposes it. The mp4 `/api/clip` route stays for export and as fallback.
- Concurrency: while a play session runs, `fetch_media` / export / thumbs for that camera return 503 "playback is open" (they share the media lock `_locks[cid]`); starting Live stops playback and vice versa.
- Rate limit: max 3 starts per minute per camera (a rapid scrubbing user must not hammer the camera).

### Page (static/app.js)
- For a camera with `stream_playback`: `play(ts)` (click on timeline, event, auto-continue) calls `/api/play/start`, polls status until the playlist exists (or error), then attaches hls.js like `startLive` (muted start + "Press play" fallback).
  No piece/prefetch logic for that camera. Speed buttons disabled like Live (1x only). A scrub restarts the session (debounced 350 ms already exists).
- Timeline position: `viewT = start + (v.currentTime - baseline)` where baseline = currentTime of the first played frame. Known weakness: if the camera skips a gap between recordings, wall clock jumps while PTS stays continuous.
  Option: server extracts `X-Data-PTS` (wall-clock ms) from the response headers and writes `pos.json` (stream_pts_seconds -> wall clock) every ~2 s; the page maps currentTime through it.
- Leaving the camera / pressing Live / closing the page: `stop` (best effort `navigator.sendBeacon`) + idle reaper as the safety net.
- Errors (gate refusal "waiting N s", busy, no video) shown in the existing message area; the gate refusal retries itself (already implemented for pieces).

## Failure modes to handle
- Camera stream slots full (-52407 etc.) -> status error "camera busy", never retried in a loop.
- UDT/camera stalls or the phone app opens a stream -> stall timeout, clean teardown, UI shows "playback ended".
- Server crash/kill -> orphan ffmpeg (pidfile cleanup at startup); camera session times out by itself (UDT keepalive silence).
- Session crossing midnight (camera local day changes) -> pre-list next day; if the camera ends the stream there, status `ended`, page offers "continue" = start at the new time.
- ffmpeg stdin/audio pipe backpressure: a full audio pipe must not deadlock the video writer (write both with timeouts; drop audio rather than block if ffmpeg stops reading).

## Open questions for review
1. Two-pipe ffmpeg input (stdin TS + fd 3 raw A-law): deadlock/backpressure risks, A/V sync when the raw audio has no timestamps, alternatives (mux audio into the TS ourselves, write audio into a second TS PES, use a named pipe/UDP, or AAC-encode in-process).
2. HLS `event` playlist vs live sliding window for hls.js and for seeking back; segment/timestamp offsets after `-f hls` with copied camera PTS (start at 0? discontinuities at recording boundaries?).
3. Mapping stream time to wall-clock when recordings have gaps (use `X-Data-PTS`?); is the proposed `pos.json` worth it?
4. Lifecycle/locking against the existing per-camera locks, the API lock, the live relay, `fetch_media`/export, and the login gate. Any deadlock or starvation risk? Should play sessions hold `_locks[cid]` for their whole lifetime?
5. Resource leaks and teardown order (ffmpeg, pipes, UDT, Bridge TCP, UDP socket, pytapo media session tasks); behaviour when the browser disappears.
6. Camera-side risks: leaving a playback stream open for hours, concurrent app/phone use, the 2-stream limit, and what a restart of the stream (scrub) does to the camera's slot accounting.
7. Alternatives worth considering (e.g. direct fMP4/MSE push over a WebSocket instead of HLS; a smaller first step such as playing the pieces from the existing route with progressive HLS only for the first piece).
8. Anything missing, simpler, or dangerous.
