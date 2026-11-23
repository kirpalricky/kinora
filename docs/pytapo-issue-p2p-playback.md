**Title:** C100 v2 (fw 1.3.17): SD-card playback works only over the app's P2P/UDT channel - documented flow, working from the LAN with no cloud

## Summary
On a Tapo C100 (hardware 2.0, firmware 1.3.17) pytapo can list recordings and events and can stream *live* over port 8800, but **`playback` and `download` requests on port 8800 get no reply at all** (not even a session id). The official app plays the same recordings fine. This issue documents why, and a flow I verified that makes SD-card playback work from the LAN without the TP-Link cloud or the app. Happy to contribute code if useful (see the end).

Tested: C100 hw 2.0 / fw 1.3.17 (`encrypt_type` "3"), pytapo 3.4.34, local API login as `admin` with the cloud password. Only this one unit; other models may differ.

## What I saw
- `getRecordings` / `getEvents` work. The media session on 8800 logs in (digest + AES key exchange) and a `preview` request streams video.
- `playback` (any variant, with or without `end_time`, channels `[0]`/`[0,1]`, with `player_id`, hub-style query params) and `download`: the camera accepts the request and then sends nothing.
- `getAppComponentList` reports `playback:4` and has no `localCtrl`, no `recordDownload`, no `streamCapability` (a C200 fw 1.9.2 has `playback:6`, `recordDownload:2`, `localCtrl:1`).
- While the app plays a recording on the same Wi-Fi, router counters show about 7 MB of UDP going directly camera -> phone, and only tens of KB to the cloud. So the app does not use 8800 for this model; it uses a UDP peer-to-peer channel.

## The flow (all verified on this unit)
1. **Ask the camera for a P2P session over the normal local API** (same session as `getRecordings`):
   `{"method":"do","p2p":{"request_p2p":{"version":"1.1","app_imp_version":4,"pub_ip":<our LAN ip>,"pub_port":<a UDP port we bound>,"pri_ip":<same>,"pri_port":<same>,"nat_type":0,"app_nonce":<16 random chars>,"punch_version":0,"punch_timeout":30,"punch_maxfds":128,"cone2symm_interval":20,"symm2cone_interval":80,"stun_url":"stun.tplinkcloud.com"}}}`
   Reply: `result` with `sid`, `is_ready:1`, `nat_type`, `pub_ip/pub_port`, `pri_ip/pri_port` (the camera's LAN address and a per-session UDP port).
   - `stun_url` must be non-empty, otherwise `-52002`. Unknown sid in `is_p2p_ready`: `-52005`; missing params: `-50001`. An unknown method name (`-40105`) drops the session.
2. **UDP probe.** From the bound UDP socket send the `sid` string (looks like `1<MAC>-<epoch>-<16 chars>`) to the camera's `pri_ip:pri_port`. It answers with the same string whose first character is incremented, plus a NUL. Echo it back.
3. **UDT v4 in rendezvous mode on the same socket.** Handshake: version 4, socket type 1 (stream), MSS 1420, flow window 1024, `reqType` 0 (destination socket id 0) -> -1 -> -2. Data payload max 1376 bytes. ACK body is 24 bytes (next seq, rtt, rttvar, buffer, rate, capacity), plus ACK2/NAK/keepalive/shutdown as in the UDT4 spec. Header timestamps may be 0.
4. **Inside UDT the same `POST /stream` multipart protocol as port 8800 runs** (401 digest challenge, 200 with key exchange, plaintext JSON requests, encrypted `video/mp2t` parts). So pytapo's `HttpMediaSession` works unchanged if it is pointed at a local TCP port that relays to the UDT stream.
5. **Playback request prerequisites.** The camera answers `-52402` (VOD_INVALID_REQUEST) until a day listing (`searchVideoOfDay`, i.e. `getRecordings(YYYYMMDD)`) has been done for the date **in the same login**, and `client_id` must be that login's `getUserID` (other ids give `-52407` TOO_MANY_CLIENT). The app's two steps work: `get` `playback` -> `session_id`, then `do` `play` with `X-Session-Id`:
   `{"playback":{"audio_config":{"encode_type":"G711alaw","sample_rate":"8"},"auto_switch_date":"0","channels":[0],"client_id":<getUserID>,"event_type":[1],"filter":true,"scale":"1/1","start_time":"<epoch>"},"method":"get"}` then `{"play":{"auto_switch_date":"0","scale":"1/1","auto_seek":"0","start_time":"<epoch>"},"method":"do"}`.
   `download` returns `-51416` on this model. Playback runs at about 1x real time, does not stop at `end_time` and rolls on into the following recordings.

Result: H.264 1920x1080 plus G.711 audio from SD-card recordings, with the camera on the LAN and nothing sent to the cloud. P2P session setup takes about 0.6 s.

## How I found it
By capturing the official Android app's own traffic on my phone while it played a recording, and reading how the app behaves. No keys or credentials are involved in any of the steps above.

## Offer
I have a small asyncio UDT client (about 300 lines, loopback-tested including packet loss) and the loopback TCP bridge that lets `HttpMediaSession` talk to it. If you want this in pytapo (for example `pytapo/media_stream/p2p.py` plus a `Tapo.getP2PMediaSession()` helper, gated on a missing `localCtrl` component), tell me the scope you prefer and I will open a PR with tests. It would be useful to know whether other models/firmwares without `localCtrl` behave the same.
