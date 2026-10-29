import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASCII_ENV = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "LANG": "C",
             "PYTHONIOENCODING": "ascii:strict", "USUAL_NO_DOTENV": "1"}
for _k in [k for k in ASCII_ENV if k.startswith("NEBIUS_")]:
    ASCII_ENV.pop(_k)


def run_py(code, cwd=ROOT, timeout=240):
    return subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=cwd, env=ASCII_ENV,
                          capture_output=True, text=True, timeout=timeout)


def test_the_child_process_really_has_an_ascii_default():
    r = run_py("import locale; print(locale.getpreferredencoding(False)); open('/tmp/_e.txt','w').write('\\u20a6')")
    assert r.returncode != 0 and "UnicodeEncodeError" in r.stderr      


def test_no_text_file_io_without_an_explicit_encoding():
    bad = []
    for p in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name not in ("read_text", "write_text", "open") or any(k.arg == "encoding" for k in n.keywords):
                continue
            mode = next((a.value for a in n.args[:2] if isinstance(a, ast.Constant) and isinstance(a.value, str)
                         and a.value in ("r", "w", "a", "rb", "wb", "ab", "r+", "x")), None)
            mode = next((k.value.value for k in n.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)), mode)
            if mode and "b" in mode:
                continue
            if name == "open" and isinstance(f, ast.Attribute) and getattr(f.value, "id", "") in ("webbrowser", "os", "zipfile", "io", "sqlite3"):
                continue
            bad.append(f"{p.relative_to(ROOT)}:{n.lineno} {name}()")
    assert not bad, "text I/O without encoding= (breaks on Windows):\n" + "\n".join(bad)


def test_smoke_cases_are_read_as_real_naira_signs_not_mojibake():
    r = run_py("import json, pathlib\n"
               "rows = json.loads(pathlib.Path('scripts/smoke_cases.json').read_text(encoding='utf-8'))\n"
               "sh3 = [x for x in rows if x['id'] == 'sh3'][0]\n"
               "print(ascii(sh3['text']), ascii(sh3['expect']['phrase']))")
    assert r.returncode == 0, r.stderr
    assert "\\u20a6" in r.stdout and "\\xe2" not in r.stdout                             


def test_mutation_results_with_naira_signs_save_and_reload_under_an_ascii_default(tmp_path):
    code = f"""
import json, pathlib, sys
sys.path.insert(0, '.')
from polygraph import runner as R, compare as CMP, cases as C
from polygraph.mutations import all_mutations
R.RESULTS_DIR = pathlib.Path({str(tmp_path / 'res')!r})
muts = [m for m in all_mutations(C.DEV[3:4]) if m['mutation'] == 'amount_naira_sign']
assert muts and '\\u20a6' in muts[0]['text']
rows = R.run_system(R.systems(None)['B1'], R.Bench(), muts)
path = R.save_results('dev', 'B1', rows, {{'system': 'B1', 'name': 'x', 'split': 'dev', 'kind': 'mutations'}}, '-mutations')
raw = path.read_bytes()
assert all(b < 128 for b in raw), 'result file must be pure ASCII'
meta, loaded = CMP.load(path)
assert '\\u20a6' in next(iter(loaded.values()))['text']
print('OK')
"""
    r = run_py(code)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_the_exact_failing_command_now_completes_under_an_ascii_default(tmp_path):
    """python -m polygraph.runner --split dev --systems B1 --mutations-only (the command that crashed on Windows)."""
    code = f"""
import pathlib, sys
sys.path.insert(0, '.')
from polygraph import runner as R
R.RESULTS_DIR = pathlib.Path({str(tmp_path / 'res')!r})
rc = R.cli(['--split', 'dev', '--systems', 'B1', '--mutations-only', '--limit', '6'])
files = sorted(p.name for p in R.RESULTS_DIR.iterdir())
print('RC', rc, files)
"""
    r = run_py(code)
    assert r.returncode == 0, r.stderr[-800:]
    assert "mutation families for B1" in r.stdout and "RC 0" in r.stdout
    assert "-mutations.json" in r.stdout and "-dev-B1.json'" not in r.stdout             
    assert not [l for l in r.stdout.splitlines() if "_partial" in l]                      


def test_progress_is_saved_per_case_and_survives_a_crash_at_the_end(tmp_path, monkeypatch):
    from polygraph import runner as R
    monkeypatch.setattr(R, "RESULTS_DIR", tmp_path / "res")
    def boom(*a, **k):
        raise RuntimeError("simulated crash while saving")
    monkeypatch.setattr(R, "save_results", boom)
    with pytest.raises(RuntimeError):
        R.main(["--split", "dev", "--systems", "B1", "--limit", "3"])
    partials = list((tmp_path / "res").glob("_partial-*.jsonl"))
    assert len(partials) == 1
    lines = partials[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["id"] == "D01"                          
    monkeypatch.undo()
    monkeypatch.setattr(R, "RESULTS_DIR", tmp_path / "res2")
    R.main(["--split", "dev", "--systems", "B1", "--limit", "3"])
    assert not list((tmp_path / "res2").glob("_partial-*"))                               
    assert len(list((tmp_path / "res2").glob("*-dev-B1.json"))) == 1


def test_mutations_only_skips_the_base_run(tmp_path, monkeypatch, capsys):
    from polygraph import runner as R
    monkeypatch.setattr(R, "RESULTS_DIR", tmp_path / "res")
    R.main(["--split", "dev", "--systems", "B1", "--limit", "4", "--mutations-only"])
    names = sorted(p.name for p in (tmp_path / "res").iterdir())
    assert len(names) == 1 and names[0].endswith("-dev-B1-mutations.json")
    out = capsys.readouterr().out
    assert "mutation families" in out and "### B1" not in out                                 


def test_utf8_streams_never_raises_even_on_streams_that_cannot_change(capsys):
    from app.console import utf8_streams
    utf8_streams()
    print("\u20a650,000")                                                                      