import sys


def utf8_streams() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: pytest capture objects and some wrappers have no reconfigure
            pass