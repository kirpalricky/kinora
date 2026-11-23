# Contributing

- Run the offline test suite before sending a change: `sh tests/run_all.sh` (it must end ALL PASS). It needs no camera and no network.
- Tests and tools must never contact a real camera. Use `tests/fakecam.py` and mocks. Repeated failed logins lock a real camera for about 30 minutes.
- Privacy: no video, thumbnails, screenshots of the player, passwords, IP addresses, MAC addresses, serial or device ids, or account emails in issues, pull requests, commits or test fixtures. Use example values (`10.0.0.x`, `example.test`).
- Keep changes small and describe what and why in the pull request. See `CODEMAP.md` for where code lives.
- By contributing you agree your work is licensed under the MIT licence (`LICENSE`).
