#!/bin/sh
# Packaging smoke test: build a wheel, install it in a throwaway venv, run `kinora`, fetch the UI over 127.0.0.1.
# Needs pip access to an index (or a warm cache) for the dependencies. Touches only 127.0.0.1 and a temp home: no camera.
# Run from anywhere: PY=python sh tests/package_smoke.sh
cd "$(dirname "$0")/.." || exit 1
PY="${PY:-python3}"
TMP="$(mktemp -d)"
PID=""
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT INT TERM
fail() { echo "FAIL: $*" >&2; [ -f "$TMP/server.log" ] && tail -20 "$TMP/server.log" >&2; exit 1; }

echo "== build wheel"
mkdir "$TMP/dist"
if "$PY" -m build --version >/dev/null 2>&1; then
  "$PY" -m build --wheel --outdir "$TMP/dist" . >"$TMP/build.log" 2>&1 || { cat "$TMP/build.log"; fail "python -m build"; }
else
  "$PY" -m pip wheel . --no-deps -w "$TMP/dist" >"$TMP/build.log" 2>&1 || { cat "$TMP/build.log"; fail "pip wheel"; }
fi
WHEEL="$(ls "$TMP"/dist/*.whl | head -1)"
[ -f "$WHEEL" ] || fail "no wheel built"
"$PY" - "$WHEEL" <<'PYEOF' || fail "wheel contents"
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
need = ["kinora/web/index.html", "kinora/web/static/app.js", "kinora/web/static/hls.min.js",
        "kinora/web/static/setup.js", "kinora/web/static/m3.css", "kinora/web/static/app.css", "kinora/cli.py"]
missing = [n for n in need if n not in names]
if missing:
    sys.exit("missing from wheel: " + ", ".join(missing))
PYEOF

echo "== install in a temp venv"
"$PY" -m venv "$TMP/venv" || fail "venv"
"$TMP/venv/bin/python" -m pip install --quiet "$WHEEL" >"$TMP/pip.log" 2>&1 || { cat "$TMP/pip.log"; fail "pip install (needs an index or a warm cache)"; }
K="$TMP/venv/bin/kinora"
export KINORA_NO_BROWSER=1
export KINORA_HOME="$TMP/home"

echo "== kinora --version / doctor"
"$K" --version || fail "--version"
"$K" doctor; RC=$?
if [ "$RC" -ne 0 ]; then
  if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then fail "doctor exit $RC with ffmpeg present"; fi
  echo "(doctor exit $RC allowed: ffmpeg missing)"
fi

echo "== start server"
PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
# the single-instance lock is machine-wide (user cache dir): skip it so a real Kinora on this machine is not disturbed
export KINORA_NO_INSTANCE_LOCK=1
"$K" --no-browser --port "$PORT" --home "$TMP/home" >"$TMP/server.log" 2>&1 &
PID=$!
i=0
until "$PY" -c "import socket; socket.create_connection(('127.0.0.1',$PORT),0.5).close()" 2>/dev/null; do
  i=$((i+1)); [ "$i" -gt 60 ] && fail "server did not start"
  kill -0 "$PID" 2>/dev/null || fail "server exited"
  sleep 0.5
done

echo "== fetch the UI"
get() {  # path: asserts 200, body goes to $TMP/body (Host header as the browser sends it)
  "$PY" - "$PORT" "$1" "$TMP/body" <<'PYEOF'
import sys, urllib.request
port, path, out = sys.argv[1:4]
r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": f"localhost:{port}"}), timeout=10)
assert r.status == 200, r.status
open(out, "wb").write(r.read())
PYEOF
}
for p in / /static/app.js /static/hls.min.js /static/setup.js /static/m3.css /static/app.css /api/setup/state; do
  get "$p" || fail "GET $p"
  echo "200 $p"
  if [ "$p" = "/" ]; then
    grep -q '/static/app.js' "$TMP/body" && grep -q '/static/hls.min.js' "$TMP/body" && grep -q '/static/app.css' "$TMP/body" || fail "index.html does not reference the assets"
  fi
done
echo "PACKAGE SMOKE PASS"
