"""Developer shim: `python server.py` from a checkout. The code lives in kinora/server.py; `import server` returns that module."""
import sys

if __name__ == "__main__":
    # one server per machine, then the one-time data folder move: both before kinora.server copies any config path
    from kinora import instance
    instance.startup()
    from kinora import server as _server
    _server.main()
else:
    from kinora import server as _server
    sys.modules[__name__] = _server
