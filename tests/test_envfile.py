import io
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient 

from app import envfile as E 
from app.api import create_app 
from app.config import Settings 
from tests.helpers import ManualTime  

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("text,expected", [
    ("A=1\nB=two", {"A": "1", "B": "two"}),
    ("# comment\n\n  A = spaced  \n", {"A": "spaced"}),
    ("export A=1", {"A": "1"}),
    ('A="quoted value"\nB=\'single\'', {"A": "quoted value", "B": "single"}),
    ("A=value  # trailing comment", {"A": "value"}),
    ("A=https://x.invalid/v1/#frag", {"A": "https://x.invalid/v1/#frag"}),              
    ('A={"k": "v", "n": 1}', {"A": '{"k": "v", "n": 1}'}),                                
    ("A=\nB=2", {"A": "", "B": "2"}),
    ("not a line\n=novalue\n1BAD=x\nGOOD=ok", {"GOOD": "ok"}),
    ("A=1\r\nB=2\r\n", {"A": "1", "B": "2"}),                                              
    ("USUAL_DATA_DIR=C:\\usual-data", {"USUAL_DATA_DIR": "C:\\usual-data"}),                 
    ("A=x=y=z", {"A": "x=y=z"}),
])
def test_parse(text, expected):
    assert E.parse(text) == expected


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le-bom"])
def test_windows_encodings_load(tmp_path, encoding):
    body = "NEBIUS_MODEL=nvidia/Nemotron-3_5-Lightning\r\nNEBIUS_API_KEY=real-looking-key-123\r\n"
    raw = (b"\xff\xfe" + body.encode("utf-16-le")) if encoding == "utf-16-le-bom" else body.encode(encoding)
    f = tmp_path / ".env"; f.write_bytes(raw)
    env = {}
    res = E.load_dotenv([f], environ=env)
    assert env == {"NEBIUS_MODEL": "nvidia/Nemotron-3_5-Lightning", "NEBIUS_API_KEY": "real-looking-key-123"}
    assert res.errors == [] and sorted(res.loaded) == ["NEBIUS_API_KEY", "NEBIUS_MODEL"]


def test_real_environment_always_wins_unless_override(tmp_path):
    f = tmp_path / ".env"; f.write_text("A=from_file\nB=from_file")
    env = {"A": "from_terminal"}
    res = E.load_dotenv([f], environ=env)
    assert env == {"A": "from_terminal", "B": "from_file"} and res.kept_existing == ["A"] and res.loaded == ["B"]
    E.load_dotenv([f], override=True, environ=env)
    assert env["A"] == "from_file"


def test_placeholders_are_skipped_and_reported(tmp_path):
    f = tmp_path / ".env"; f.write_text("NEBIUS_API_KEY=paste-your-key-here\nNEBIUS_MODEL=real/model")
    env = {}
    res = E.load_dotenv([f], environ=env)
    assert "NEBIUS_API_KEY" not in env and env["NEBIUS_MODEL"] == "real/model" and res.placeholders == ["NEBIUS_API_KEY"]
    buf = io.StringIO(); E.report(res, buf)
    assert "NEBIUS_API_KEY still has placeholder text" in buf.getvalue()


def test_the_shipped_env_example_is_safe_to_copy_as_is(tmp_path):
    env = {}
    res = E.load_dotenv([ROOT / ".env.example"], environ=env)
    assert "NEBIUS_API_KEY" in res.placeholders and "NEBIUS_API_KEY" not in env         
    assert env["NEBIUS_MODEL"] == "nvidia/Nemotron-3_5-Lightning" and env["NEBIUS_BASE_URL"].startswith("https://")
    assert int(env["NEBIUS_MAX_TOKENS"]) >= 4096


def test_values_are_never_printed(tmp_path):
    f = tmp_path / ".env"; f.write_text("NEBIUS_API_KEY=SUPER-SECRET-VALUE\nNEBIUS_MODEL=m")
    res = E.load_dotenv([f], environ={})
    buf = io.StringIO(); E.report(res, buf)
    assert "NEBIUS_API_KEY" in buf.getvalue() and "SUPER-SECRET-VALUE" not in buf.getvalue()


def test_missing_unreadable_and_disabled(tmp_path):
    assert E.load_dotenv([tmp_path / "nope.env"], environ={}).files == []
    bad = tmp_path / "bad.env"; bad.write_bytes(b"\xff\xfd\x80\x81 not text \x00\xc3\x28")
    res = E.load_dotenv([bad], environ={})
    assert res.errors and "could not read" in res.errors[0]                            
    f = tmp_path / ".env"; f.write_text("A=1")
    env = {"USUAL_NO_DOTENV": "1"}
    assert E.load_dotenv([f], environ=env).files == [] and "A" not in env


def test_later_file_cannot_override_earlier(tmp_path):
    a, b = tmp_path / "a.env", tmp_path / "b.env"
    a.write_text("K=first"); b.write_text("K=second")
    env = {}
    E.load_dotenv([a, b], environ=env)
    assert env["K"] == "first"


# ---- where it may run --------------------------------------------------------------------------------------------
def point_loader_at(monkeypatch, tmp_path, body):
    (tmp_path / ".env").write_text(body)
    monkeypatch.setattr(E, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", os.environ.copy())     
    os.environ.pop("USUAL_NO_DOTENV", None)


def test_real_entry_point_reads_dotenv_and_goes_live(monkeypatch, tmp_path):
    point_loader_at(monkeypatch, tmp_path, "NEBIUS_API_KEY=k\nNEBIUS_BASE_URL=https://x.invalid/v1\nNEBIUS_MODEL=nvidia/Test\n"
                                           f"USUAL_DATA_DIR={tmp_path / 'data'}\nUSUAL_MAX_CALLS_PER_SESSION=7\n")
    c = TestClient(create_app())                               
    info = c.get("/api/info").json()
    assert info["live_model"] is True and info["parser"] == "nvidia/Test" and info["max_model_calls_per_session"] == 7
    assert (tmp_path / "data" / "template.db").exists()        


def test_tests_and_embedders_that_pass_settings_never_read_dotenv(monkeypatch, tmp_path):
    point_loader_at(monkeypatch, tmp_path, "NEBIUS_API_KEY=k\nNEBIUS_BASE_URL=https://x.invalid/v1\nNEBIUS_MODEL=nvidia/Test\n")
    c = TestClient(create_app(Settings(tmp_path / "d", time_source=ManualTime())))
    assert c.get("/api/info").json()["live_model"] is False  


def test_benchmark_main_does_not_read_dotenv_but_cli_does(monkeypatch, tmp_path, capsys):
    from polygraph import runner as R
    calls = []
    monkeypatch.setattr(R, "load_dotenv", lambda: calls.append("load") or E.LoadResult())
    monkeypatch.setattr(R, "FROZEN", tmp_path / "F.json")
    monkeypatch.setattr(R, "RUNS_LOG", tmp_path / "log")
    R.main(["--split", "heldout", "--systems", "B1"])         
    assert calls == []                                          
    R.cli(["--split", "heldout", "--systems", "B1"])
    assert calls == ["load"]


def test_suite_isolation_blocks_credentials_from_the_shell():
    """Run one probe test in a child pytest whose environment HAS credentials; the autouse fixture must hide them."""
    probe = ROOT / "tests" / "_probe_env.py"
    probe.write_text("import os\ndef test_probe():\n    assert not [k for k in os.environ if k.startswith('NEBIUS_')]\n"
                     "    assert os.environ.get('USUAL_NO_DOTENV') == '1'\n")
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider", "--rootdir", str(ROOT),
                            "-c", "/dev/null" if os.name != "nt" else "NUL"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120,
                           env={**os.environ, "NEBIUS_API_KEY": "real", "NEBIUS_MODEL": "m", "NEBIUS_BASE_URL": "https://x.invalid"})
        assert r.returncode == 0, r.stdout + r.stderr
    finally:
        probe.unlink(missing_ok=True)