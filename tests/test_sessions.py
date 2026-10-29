import re
import sqlite3
from pathlib import Path

import pytest

from app import db, lifecycle as L, seed
from app.sessions import BadSessionId, SessionGone
from tests.helpers import intent


def test_SI1_sessions_are_independent(env):
    a, b = env.new(), env.new()
    assert a.path != b.path and a.sid != b.sid
    v = a.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k"))
    assert a.commit(v)[0] == 200
    assert a.count("ledger_entry", "source_action_id IS NOT NULL") == 1 and b.count("ledger_entry", "source_action_id IS NOT NULL") == 0


def test_SI1b_unknown_action_in_other_session_is_404_and_writes_nothing(env):
    a, b = env.new(), env.new()
    v = a.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k"))
    status, raw = L.commit_action(b.ctx, v["draft"]["action_id"], "k", v["draft"]["payload_hash"])
    assert status == 404 and b.count("commit_attempt") == 0 and b.count("ledger_entry", "source_action_id IS NOT NULL") == 0


def test_SI2_template_is_immutable(env, template):
    path, h = template
    s = env.new()
    v = s.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k"))
    s.commit(v)
    assert seed.file_hash(path) == h                                        
    ro = db.connect(path, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO meta(key, value) VALUES ('x','y')")           
    ro.close()
    assert seed.file_hash(path) == h


def test_SI3_session_ids_are_unguessable_and_paths_are_not_derived_from_them(env):
    s = env.new()
    assert re.fullmatch(r"[0-9a-f]{32}", s.sid) and s.sid not in s.path.name and s.sid != env.new().sid


def test_SR1_missing_file_is_gone_forever_and_never_recreated(env):
    a = env.new()
    gone_path = a.path
    gone_path.unlink()
    for ext in ("-wal", "-shm"):
        Path(str(gone_path) + ext).unlink(missing_ok=True)
    with pytest.raises(SessionGone):
        env.reg.resolve(a.sid)
    b = env.new()                                                           
    assert b.sid != a.sid
    with pytest.raises(SessionGone):                                        
        env.reg.resolve(a.sid)
    assert not gone_path.exists()                                          
    assert env.reg.state_of(a.sid) == ("reset", "missing_file")


def test_SR2_replacement_session_is_fresh_template_data_not_the_old_one(env, template):
    a = env.new()
    a.commit(a.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k")))
    a.path.unlink()
    with pytest.raises(SessionGone):
        env.reg.resolve(a.sid)
    b = env.new()
    tpl = sqlite3.connect(f"file:{template[0]}?mode=ro&immutable=1", uri=True)
    expected = sorted(r[0] for r in tpl.execute("SELECT id FROM ledger_entry"))
    assert sorted(r["id"] for r in b.q("SELECT id FROM ledger_entry")) == expected
    for t in ("staged_action", "instruction", "decision", "commit_attempt", "action_event"):
        assert b.count(t) == 0, t


@pytest.mark.parametrize("bad", ["../x", "../../etc/passwd", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "", None, 123, "0" * 32 + "/"])
def test_SR3_malformed_ids_rejected_without_touching_the_filesystem(env, bad):
    before = sorted(p.name for p in env.reg.sessions_dir.iterdir())
    with pytest.raises(BadSessionId):
        env.reg.resolve(bad)
    assert sorted(p.name for p in env.reg.sessions_dir.iterdir()) == before


def test_SR3b_unknown_well_formed_id_is_just_gone(env):
    with pytest.raises(SessionGone):
        env.reg.resolve("0" * 32)


def test_SR4_no_fallback_to_template_or_other_sessions(env, template, monkeypatch):
    a, other = env.new(), env.new()
    a.path.unlink()
    opened = []
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *x, **k: (opened.append(str(x[0]) if x else ""), real(*x, **k))[1])
    with pytest.raises(SessionGone):
        env.reg.resolve(a.sid)
    joined = "\n".join(opened)
    assert str(template[0]) not in joined and str(other.path) not in joined and str(a.path) not in joined


def test_SR_corrupt_file_is_reset_not_repaired(env):
    a = env.new()
    a.path.write_bytes(b"this is not a sqlite database" * 100)
    for ext in ("-wal", "-shm"):
        Path(str(a.path) + ext).unlink(missing_ok=True)
    with pytest.raises(SessionGone):
        env.reg.resolve(a.sid)
    assert env.reg.state_of(a.sid)[0] == "reset"


def test_SR_tampered_schema_session_is_refused(env):
    a = env.new()
    a.op("DROP TRIGGER tr_ledger_no_delete")
    with pytest.raises(SessionGone):
        env.reg.resolve(a.sid)
    assert env.reg.state_of(a.sid)[0] == "reset"


def test_SR_idle_expiry_is_permanent(env):
    a = env.new()
    env.time.advance(env.reg.idle_seconds + 1)
    with pytest.raises(SessionGone):
        env.reg.resolve(a.sid)
    env.time.t = env.time.t.replace(year=2029)                   
    with pytest.raises(SessionGone):
        env.reg.resolve(a.sid)
    assert env.reg.state_of(a.sid)[0] == "expired"


def test_SR_capacity_limit(env):
    env.reg.max_active = 2
    env.new(); env.new()
    from app.sessions import Capacity
    with pytest.raises(Capacity):
        env.reg.create_session()


def test_SR5_ui_never_implies_restoration():
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert "were not restored" in html and "fresh sample data" in html
    stripped = html.replace("were not restored", "")
    for word in ("restored", "resumed", "recovered", "welcome back", "pick up where", "continue where"):
        assert word not in stripped.lower(), word
    assert 'id="banner"' in html and 'role="alert"' in html and "banner-ok" in html
    assert "sessionStorage" in html and "newSession(true)" in html            # 410 always yields a NEW session plus the banner


def test_SR6_commit_key_replayed_under_a_different_session(env):
    a = env.new()
    v = a.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k"))
    assert a.commit(v, key="KEY1")[0] == 200
    b = env.new()
    status, raw = L.commit_action(b.ctx, v["draft"]["action_id"], "KEY1", v["draft"]["payload_hash"])
    assert status == 404 and b.count("commit_attempt") == 0 and b.count("ledger_entry", "source_action_id IS NOT NULL") == 0