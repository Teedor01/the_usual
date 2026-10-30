import os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "shots"); OUT.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("PORT", "8079")); BASE = f"http://127.0.0.1:{PORT}"
data = tempfile.mkdtemp()
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api:create_app", "--factory", "--port", str(PORT), "--log-level", "warning"],
                       cwd=ROOT, env={**os.environ, "USUAL_DATA_DIR": data}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(60):
        try: urllib.request.urlopen(BASE + "/api/info", timeout=1); break
        except Exception: time.sleep(0.25)
    errors, problems = [], []
    with sync_playwright() as p:
        b = p.chromium.launch()
        def run(name, w, h, steps):
            ctx = b.new_context(viewport={"width": w, "height": h}); pg = ctx.new_page()
            pg.on("console", lambda m: errors.append((name, m.type, m.text)) if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: errors.append((name, "pageerror", str(e))))
            pg.goto(BASE + "/"); pg.wait_for_selector("#people .person")
            steps(pg, name)
            over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if over > 0: problems.append(f"{name}: horizontal overflow of {over}px")
            ctx.close()
        def say(pg, text, sel):
            pg.fill("#text", text); pg.click("#go"); pg.wait_for_selector(sel, timeout=20000); pg.wait_for_timeout(500)
        def desktop(pg, n):
            pg.screenshot(path=str(OUT / f"{n}-1-home.png"))
            say(pg, "Abeg pay Emeka the remaining 50k for chairs", ".slip.stage")
            pg.click("#decision summary"); pg.screenshot(path=str(OUT / f"{n}-2-draft.png"))
            pg.click("#confirm"); pg.wait_for_selector(".slip.done"); pg.wait_for_timeout(500); pg.screenshot(path=str(OUT / f"{n}-3-done.png"))
            say(pg, "Pay Chidi the balance.", ".slip.ask .opts button"); pg.screenshot(path=str(OUT / f"{n}-4-ask.png"))
            say(pg, "Pay Bassey 80k", ".slip.refuse"); pg.screenshot(path=str(OUT / f"{n}-5-refuse.png"))
        def phone(pg, n):
            pg.screenshot(path=str(OUT / f"{n}-1-home.png"), full_page=True)
            say(pg, "Pay Amaka 50k", ".slip.stage"); pg.screenshot(path=str(OUT / f"{n}-2-draft.png"), full_page=True)
        run("desktop", 1280, 900, desktop); run("phone", 390, 844, phone)
        b.close()
    print("screenshots:", sorted(x.name for x in OUT.iterdir()))
    print("console/page errors:", errors or "none"); print("layout problems:", problems or "none")
finally:
    srv.terminate()
