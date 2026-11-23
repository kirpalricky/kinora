# Security policy

## Reporting a vulnerability

Please report privately, not in a public issue: use GitHub's "Report a vulnerability" (Security tab, private advisory) on this repository. Include the affected version, steps to reproduce and impact. Do not include camera passwords, video or thumbnails.

You can expect an acknowledgement within a few days. This is a volunteer project; fixes are best effort.

## Security model

- Kinora is a local web app and listens on localhost only by default. Exposing it to a network or the internet is not supported unless you add your own access control.
- Camera and TP-Link account passwords are stored in the OS keyring (or an opt-in private file in the data folder), never in `cameras.json`, and are never written to logs. Report any case where one appears in a log, URL or process list.
- Kinora makes no telemetry or update requests.
- Cameras lock out after repeated failed logins; a built-in login gate limits attempts. Reports of ways to bypass the gate are in scope.
