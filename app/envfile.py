import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = re.compile(r"^paste[-_ ]", re.I)
KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):        
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")                            


def parse(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if not KEY.fullmatch(k):
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].rstrip()   # trailing "  # comment" on unquoted values
        out[k] = v
    return out


@dataclass
class LoadResult:
    files: list = field(default_factory=list)
    loaded: list = field(default_factory=list)          # names set from the file
    kept_existing: list = field(default_factory=list)   # names the real environment already had (file ignored for them)
    placeholders: list = field(default_factory=list)    # names whose value is still "paste-..."
    errors: list = field(default_factory=list)


def load_dotenv(paths=None, override=False, environ=None) -> LoadResult:
    env = os.environ if environ is None else environ
    res = LoadResult()
    if env.get("USUAL_NO_DOTENV") == "1":
        return res
    cands = paths if paths is not None else [ROOT / ".env", Path.cwd() / ".env"]
    seen = set()
    for p in cands:
        p = Path(p)
        try:
            key = p.resolve()
        except OSError:
            continue
        if key in seen or not p.is_file():
            continue
        seen.add(key)
        try:
            vals = parse(decode(p.read_bytes()))
        except (UnicodeDecodeError, OSError) as e:
            res.errors.append(f"{p}: could not read as UTF-8 or UTF-16 text ({type(e).__name__})")
            continue
        res.files.append(str(p))
        for k, v in vals.items():
            if PLACEHOLDER.match(v):
                res.placeholders.append(k)
            elif k in env and not override:
                res.kept_existing.append(k)
            else:
                env[k] = v
                res.loaded.append(k)
    return res


def report(res: LoadResult, stream=None) -> None:
    """Names only. Never values."""
    out = stream or sys.stderr
    if res.files:
        print(f".env: read {', '.join(res.files)}", file=out)
        if res.loaded:
            print(f".env: loaded {', '.join(sorted(set(res.loaded)))}", file=out)
        if res.kept_existing:
            print(f".env: left alone (already set in your terminal): {', '.join(sorted(set(res.kept_existing)))}", file=out)
    for k in sorted(set(res.placeholders)):
        print(f".env: WARNING {k} still has placeholder text ('paste-...'); it was NOT loaded. Put the real value in.", file=out)
    for e in res.errors:
        print(f".env: WARNING {e}", file=out)