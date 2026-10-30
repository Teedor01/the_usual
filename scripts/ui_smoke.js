// DOM-level smoke test of the real page against the real server (jsdom). Not a visual test: it checks that the
// page's logic works end to end (render, click, confirm, reset banner, loading state, interrupted confirm).
const { JSDOM } = require("jsdom");
const fs = require("fs"), path = require("path");
const BASE = process.env.BASE, SESSIONS = process.env.SESSIONS_DIR;
let failures = 0;
const ok = (name, cond, extra) => { console.log((cond ? "PASS " : "FAIL ") + name + (cond ? "" : "  -> " + (extra || ""))); if (!cond) failures++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  let netMode = "normal", delayInstructions = 0;
  const shim = async (url, opts = {}) => {
    const u = new URL(url, BASE).toString();
    const { signal, ...rest } = opts;                       // jsdom's AbortSignal is not Node's; the timeout path is not under test
    if (delayInstructions && u.endsWith("/instructions")) await sleep(delayInstructions);
    if (netMode === "drop_before_commit" && u.includes("/commit")) { netMode = "normal"; throw new TypeError("network down"); }
    const res = await fetch(u, rest);
    if (netMode === "drop_after_commit" && u.includes("/commit")) { netMode = "normal"; throw new TypeError("response lost after the server committed"); }
    return res;
  };
  const dom = await JSDOM.fromURL(BASE + "/", {
    runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = shim;
      if (!w.crypto || !w.crypto.randomUUID) Object.defineProperty(w, "crypto", { value: { randomUUID: () => require("crypto").randomUUID() } });
      w.matchMedia = () => ({ matches: false });
      w.Element.prototype.scrollIntoView = () => {};
    },
  });
  const w = dom.window, d = w.document, $ = (s) => d.querySelector(s), $$ = (s) => [...d.querySelectorAll(s)];
  const until = async (pred, what, ms = 15000) => { const t = Date.now(); while (Date.now() - t < ms) { try { if (pred()) return true; } catch {} await sleep(50); } console.log("  (timed out waiting for: " + what + ")"); return false; };
  const say = async (text) => { $("#text").value = text; $("#form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true })); };
  const slipText = () => ($("#decision").textContent || "").replace(/\s+/g, " ");

  ok("page loads and creates a session", await until(() => w.sessionStorage.getItem("usual_sid") && $$("#people .person").length >= 6, "session and ledger"));
  const sid1 = w.sessionStorage.getItem("usual_sid");
  ok("parser is labelled honestly (no model credentials)", /rule-parser/.test($("#parser").textContent), $("#parser").textContent);
  ok("unapproved payee is shown as such in the ledger", /Bassey Udo[\s\S]*Not an approved payee/.test($("#people").textContent));
  ok("example chips are present", $$("#chips button").length >= 4);

  // loading state is visible while the server is slow
  delayInstructions = 700;
  await say("Pay Emeka the balance for those chairs.");
  ok("loading slip appears while waiting", await until(() => $("#decision .slip[aria-busy]") && /Reading/.test(slipText()), "loading slip", 3000), slipText());
  ok("submit button is disabled and relabelled while working", $("#go").disabled === true && /Preparing/.test($("#go").textContent));
  delayInstructions = 0;

  ok("a draft is shown", await until(() => $(".slip.stage"), "stage slip"), slipText());
  ok("draft says nothing has been paid", /Nothing has been paid/.test(slipText()) && /₦50,000/.test(slipText()) && /Emeka Okoro/.test(slipText()), slipText());
  ok("draft shows the arithmetic and evidence", /Agreed: 100 chairs/.test(slipText()) && /Outstanding/.test(slipText()) && $("#decision details"));
  ok("submit button restored", $("#go").disabled === false && /Prepare it/.test($("#go").textContent));
  ok("a Confirm button exists only on a draft", !!$("#confirm"));

  $("#confirm").click();
  ok("confirming records the simulated payment", await until(() => $(".slip.done"), "done slip"), slipText());
  ok("confirmation says no real money moved", /Recorded \(simulated\)/.test(slipText()) && /No real money moved/.test(slipText()));
  ok("ledger refreshes: nothing now outstanding for Emeka", await until(() => /100 chairs: ₦0 outstanding/.test($("#people").textContent), "ledger refresh"));

  await say("Pay Emeka the balance for those chairs.");
  ok("repeat instruction is a question, not a payment", await until(() => $(".slip.ask") && /Nothing is outstanding/.test(slipText()), "nothing owed"), slipText());
  ok("no Confirm button on a question", !$("#confirm"));

  await say("Pay Chidi the balance.");
  ok("the previous answer is replaced immediately (no stale result)", await until(() => !/Nothing is outstanding/.test(slipText()), "old slip cleared", 1500), slipText());
  ok("ambiguous name asks which person", await until(() => /More than one person/.test(slipText()) && $(".slip.ask .opts button"), "options"), slipText());
  const nwosu = $$(".slip.ask .opts button").find((b) => /Chidi Nwosu/.test(b.textContent));
  ok("options show each person's balance", !!nwosu && /outstanding/.test(nwosu.textContent), $$(".slip.ask .opts button").map((b) => b.textContent).join(" | "));
  nwosu.click();
  ok("choosing a person produces the right draft", await until(() => $(".slip.stage") && /₦60,000/.test(slipText()), "stage after pick"), slipText());
  const cancel = $$("#decision button").find((b) => /Cancel/.test(b.textContent));
  cancel.click();
  ok("cancelling a draft pays nothing", await until(() => /Draft cancelled/.test(slipText()), "cancelled"), slipText());

  await say("Pay Bassey 80k");
  ok("unapproved payee is refused with no options", await until(() => $(".slip.refuse") && /not an approved payee/i.test(slipText()), "refuse"), slipText());
  ok("a refusal has no Confirm button", !$("#confirm"));

  // interrupted confirmation: the server COMMITS, the response is lost, the user presses Confirm again
  await say("Pay Amaka 50k");
  ok("a draft for Amaka appears", await until(() => $(".slip.stage") && /₦50,000/.test(slipText()), "amaka draft"), slipText());
  netMode = "drop_after_commit";
  $("#confirm").click();
  ok("lost response shows an honest message and re-enables Confirm", await until(() => /Connection lost/.test(slipText()) && $("#confirm") && !$("#confirm").disabled, "connection lost"), slipText());
  $("#confirm").click();
  ok("pressing Confirm again is safe and records it", await until(() => $(".slip.done"), "replay done"), slipText());
  const ledger = await (await fetch(BASE + "/api/sessions/" + w.sessionStorage.getItem("usual_sid") + "/ledger")).json();
  const amakaPays = ledger.entries.filter((e) => e.person === "Amaka Obi" && e.id.startsWith("PAY-"));
  ok("exactly ONE payment exists after the replayed confirmation", amakaPays.length === 1 && amakaPays[0].amount_kobo === 5000000, JSON.stringify(amakaPays));

  // request never delivered
  await say("Pay Amaka 20k");
  await until(() => $(".slip.stage") && /₦20,000/.test(slipText()), "second amaka draft");
  netMode = "drop_before_commit";
  $("#confirm").click();
  await until(() => /Connection lost/.test(slipText()) && $("#confirm") && !$("#confirm").disabled, "lost before delivery");
  $("#confirm").click();
  ok("undelivered confirmation can be retried and records once", await until(() => $(".slip.done"), "retry done"), slipText());

  // session loss: remove the session database out from under the page
  for (const f of fs.readdirSync(SESSIONS)) fs.unlinkSync(path.join(SESSIONS, f));
  await say("Pay Amaka 10k");
  ok("a lost session shows the reset banner", await until(() => $("#banner").style.display === "block", "banner"), "");
  ok("banner says nothing was restored", /not restored/.test($("#banner").textContent) && !/\brecovered\b|\bresumed\b/i.test($("#banner").textContent));
  const sid2 = w.sessionStorage.getItem("usual_sid");
  ok("a NEW session id was created", !!sid2 && sid2 !== sid1, sid1 + " vs " + sid2);
  ok("old drafts and history are gone from the screen", await until(() => $$("#decision .slip").length === 0, "cleared"));
  ok("the new session starts from the seed ledger", await until(() => /100 chairs: ₦50,000 outstanding/.test($("#people").textContent), "fresh ledger"));
  $("#banner-ok").click();
  ok("the banner can be dismissed", $("#banner").style.display === "none");

  console.log(failures ? `\n${failures} UI check(s) FAILED` : "\nall UI checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error("UI smoke crashed:", e); process.exit(2); });
