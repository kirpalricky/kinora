# pytapo / Tapo camera notes (what the README doesn't say)

Researched 2026-11-21. Installed pytapo 3.4.34 (latest at the time). Source tags:
**[M]** maintainer/official, **[C]** read from pytapo code, **[U]** user/community report (guess or observation).
There is **no official TP-Link documentation** of the lockout policy, the USER_ID codes or C100 playback.

## Lockout ("Temporary Suspension: Try again in N seconds")
- [U, single report, one camera model] The camera keeps a counter (starts at 10) that drops by one on every *login-phase request*,
  possibly including discovery probes. At zero it locks for 1800 s. A correct login resets it to 10.
  (HomeAssistant-Tapo-Control issue 1462, C460 fw 1.2.2). Treat as likely, not confirmed: pytapo's own docstrings
  (tpap.py ~61, 100-105) say probes count only on cameras that use the encrypt_type-3 login.
- [M] pytapo 3.4.27 (2026-10-14): "Skips redundant login probes on legacy-login cameras, reducing attempts counted toward lockouts".
- [C] pytapo comment: "login-phase requests count towards the lockout".
- [U] kopiro/homebridge-tapo-camera issue 155 (polled every 15 s) reported 41 s, 607 s and 1274 s: these are *remaining* seconds (`sec_left`) when the error was seen, not lockout lengths.
- Not documented anywhere I found: per-IP vs per-account, whether retrying extends it, whether a camera reboot clears it.
- Community advice [U]: stop every client, wait out the timer, then log in once.

## USER_ID codes (-71101 / -71102 / -71103 / -71105)
- Nothing documents USER_ID_FULL / USER_ID_EMPLOYED, how many IDs a camera holds, or whether the phone app competes.
- [C] -71101 in getUserID: forced reload + one retry. -71103 / -71105 in getRecordings: refresh userID, retry once.
- [U] -71105 = PLAYBACK_SEARCH_FAILED; pytapo issue 234: getRecordings failed on a 25-hour DST day on a C211 (suggestion: getRecordingsUTC).

## Other error codes
- -40211: new-firmware login (TPAP / SPAKE2+) rejected a legacy login. Not a wrong password. Do not retry. [U, issue 218] pytapo now has a tpap transport.
- -40401: bad sequence / undecryptable / old session. [C] pytapo retries it (stale stok, close + relogin). [U, unverified] logging in while an old session is live reportedly also gives -40401.
- -40209: plain request sent to TPAP (needs multipleRequest). -40413: normal probe reply on C200. [U, issue 218]
- -60102: no definition; one user thinks camera-internal timeout/DNS (C200). [U, issue 66]
- -40421 is in pytapo's TPAP SESSION_ERRORS (tpap.py ~30); -40210 = PROTOCOL_FORMAT_ERROR and both are in the retryable set (transport/pytapo/const.py). [C]
- Camera busy (another client holds the stream slot): -52405, -52407, -52417, -52435. [C downloader.py]

## Concurrency
- [M] Tapo FAQ (via HA README): up to 2 simultaneous video streams, shared between the phone app and RTSP.
- [U] A TP-Link moderator said no more than 3 devices can watch live at once. Not a published spec.
- Nothing found on port-8800 specific limits or request pacing. That media downloads/thumbnails occupy one of the ~2 slots is inferred from the downloader's busy codes, not documented. pytapo 3.4.27 reports "Camera busy" immediately when the slot is taken.
- [M] Using an SD card together with Tapo Care disables RTSP/ONVIF (README).

## Downloads / C100
- Nothing states whether the C100 can or cannot download/play back SD recordings (pytapo issue 51 was a credentials problem on C200/C210).
  A TP-Link moderator post (community.tp-link.com topic 712672) says clips can be viewed but not downloaded on the C100. Treat as likely, unproven.
- [C] Downloader: `method="download"` default (about 10x realtime, stops at end_time); `"playback"` is 1x realtime. It falls back to playback
  only if the camera answers the download request with an error. Defaults: window_size 200 (retry 50), stall_timeout 120 s,
  recordings ending within 60 s of now are skipped.
- [U] Issue 200: C120 fw 1.9.2 would not download; PR 221 switches to download (tested C510W, C410).

## Practice
- Reuse of one Tapo object is not documented; code only closes/relogs on -40401. No logout exists (there is `close()`).
- Credentials: camera account from the Tapo app first; else `admin` + TP-Link cloud password. Recordings need the cloud password. [M README]
  Which works depends on model and firmware (we saw: C100 and C210 = admin + cloud; C200 = camera user + cloudPassword).
- [M README] Turn on "Third-Party Compatibility" in the Tapo app.
- HA integration retries every 60 s (mains cameras) / 10 min (battery cameras). No general polling guidance from the maintainer.
- No documented ONVIF Profile G / replay or other local API for SD recordings; port 8800 (pytapo) is the only route.

## Sources
- https://github.com/JurajNyiri/pytapo (README, releases, issues 51, 66, 177, 200, 211, 218, 221, 232, 234)
- https://github.com/JurajNyiri/HomeAssistant-Tapo-Control (README FAQ, issues 599, 609, 781, 1462)
- https://github.com/kopiro/homebridge-tapo-camera/issues/155
- https://community.tp-link.com/en/home/forum/topic/712672 and /720802

## Costs seen in the pytapo code (from the third review)
- A failed legacy (pyTapo) login costs the camera about 4 requests: probe, nonce, then the nested -40413 retry repeats both (pyTapo.py ~748-760). `Tapo(retryStok=False)` removes that nested retry.
- `discover_login` / `rejects_legacy_login` run once per `Tapo()` construction when no `transportMethod` is passed (__init__.py ~76-91): the only probes outside our login gate.
- Every `Downloader` run also makes a `getAudioConfig` API call (downloader.py ~133), so a thumbnail is one media session plus one API call.
