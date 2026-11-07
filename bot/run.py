"""Runner + owner console for @justmogit_bot.

  python3 -m bot.run loop [--dry-run]     poll mentions every 60s and work the queue
  python3 -m bot.run status               kill switch, bankroll, today's launches, open jobs
  python3 -m bot.run approve JOB          owner yes for one RESERVED launch (§0.10)
  python3 -m bot.run launch JOB           arm + fire an approved job
  python3 -m bot.run kill on|off          kill switch (ON cancels every pre-send job)
  python3 -m bot.run bankroll SOL         set spendable bankroll (capped at the wallet's real balance)
  python3 -m bot.run mogme-preview        read mentions once, render "mog me" previews into the queue (posts nothing)
  python3 -m bot.run mogme-list [STATUS]  show the "mog me" approval queue
  python3 -m bot.run mogme-approve ID NAME REASON...   owner yes, with a short reason
  python3 -m bot.run mogme-reject ID NAME REASON...    wrong size / off the eyes / wrong angle /
                                          style mismatch / flat overlay / other[: note]
  python3 -m bot.run mogme-ref ID         copy an APPROVED gemini mog into the style-reference folder
  python3 -m bot.run mogme-post           post APPROVED items (refused while the kill switch is ON)

--dry-run: reads X and Solana, posts nothing (replies printed), no vault → nothing can sign.
Live mode refuses to boot without policy x_reply_approval_ref (X's written approval).

Env (names only, values never in the repo): JMI_DB, JMI_BOT_WALLET, JMI_FEE_ADDRESS,
JMI_X_BOT_USER_ID, JMI_X_USER_TOKEN, JMI_RPC_URLS, JMI_DAS_URL, JMI_PINATA_JWT,
JMI_GEMINI_KEY, JMI_GEMINI_MODEL, JMI_VAULT_URL, JMI_VAULT_TOKEN,
IMAGE_EDITOR (stub|gemini, default stub), GEMINI_API_KEY (gemini only), JMI_MOGME_QUEUE,
JMI_MOGME_REFS (style references, default mogme_refs)"""
import os
import sys
import time
import traceback
from decimal import Decimal

from . import fees
from . import parse as P
from .adapters import Adapters
from .engine import Engine, Refused
from .policy import load
from .pumpguard import PumpGuard
from .store import Store

class DryX:
    def __init__(self, real): self.real = real
    def mentions(self, since_id=None): return self.real.mentions(since_id)
    def profile_image(self, uid): return self.real.profile_image(uid)
    def reply(self, tweet_id, text, media_ids=None, idempotency_key=None):
        print(f"[dry-run reply → {tweet_id}] {text!r} media={media_ids}"); return f"dry-{tweet_id}"
    def upload_media(self, png, alt_text=None):
        print(f"[dry-run media {len(png)} bytes] alt={alt_text!r}"); return "dry-media"

def build(dry_run, store):
    from .live.dossier import Dossier
    from .live.gemini import Gemini
    from .live.http import env
    from .live.payments import Payments
    from .live.pumpportal import PumpPortal
    from .live.solana import Rpc
    from .live.vault import Vault
    from .live.x import XClient
    p = load()
    wallet = env("JMI_BOT_WALLET")
    rpc, gem = Rpc(), Gemini()
    used = lambda sig: store.db.execute("SELECT 1 FROM jobs WHERE payment_tx=?", (sig,)).fetchone() is not None
    x = XClient()
    a = dict(x=DryX(x) if dry_run else x, dossier=Dossier(gemini=gem), pump=PumpPortal(wallet, rpc), rpc=rpc,
             guard=PumpGuard(wallet, fees.lamports(p.priority_fee_cap_sol)), faces=gem,
             payments=Payments(rpc, os.environ.get("JMI_FEE_ADDRESS") or wallet, used=used),
             pair=_Pair())
    if not dry_run:
        a["vault"] = Vault()
    return p, Adapters(**a), wallet, rpc

class _Pair:
    """§5 probe. PumpPortal builds a fresh create per job and the guard + simulation check it
    right before signing, so the probe only asserts SOL (the only quote §0.6 allows)."""
    def probe(self, quote):
        return {"launchable": quote == "SOL", "expires_at": time.time() + 60}

def mogme_flow(store, p, x):
    """The "mog me" approval queue. IMAGE_EDITOR picks the editor (default stub); gemini without
    GEMINI_API_KEY raises MogMeConfigError, which refuses boot."""
    from .mogme_queue import MogMeFlow, build_editor
    return MogMeFlow(store, p, x, build_editor(), os.environ.get("JMI_MOGME_QUEUE", "mogme_queue"))

def loop(dry_run):
    store = Store(os.environ.get("JMI_DB", "justmogit.db"))
    p, a, wallet, rpc = build(dry_run, store)
    e = Engine(store, p, a, public=not dry_run, mogme=mogme_flow(store, p, a.x))
    print("boot:", e.boot())
    last_sweep = 0
    while True:
        try:
            ms, newest = a.x.mentions(store.get("since_id"))
            for m in ms:
                try:
                    print(m["id"], e.ingest(m))
                except Exception:
                    traceback.print_exc()
            if newest:
                store.put("since_id", newest)
            for j in store.jobs("PAYMENT_PENDING"):
                print(j["job_id"], e.payment_check(j["job_id"]))
            e.expire()
            for j in store.jobs("UNKNOWN"):
                print(j["job_id"], e.reconcile(j["job_id"]))
            for j in store.jobs("CONFIRMED"):
                print(j["job_id"], e.reply_launch(j["job_id"]))
            if not dry_run and time.time() - last_sweep > 86_400:
                print("sweep:", e.sweep_fees()); last_sweep = time.time()
        except Exception:
            traceback.print_exc()
        time.sleep(60)                                        # mention-poll fallback cadence (§10)

def mogme_cmd(cmd, args):
    from .live.x import XClient
    from .mogme_queue import MogMeError
    store = Store(os.environ.get("JMI_DB", "justmogit.db"))
    p = load()
    x = XClient()
    try:
        flow = mogme_flow(store, p, x)
    except MogMeError as err:
        print("refused at boot:", err); return 2
    if cmd == "mogme-preview":                 # read-only on X: mentions in, previews to disk
        ms, newest = x.mentions(store.get("mogme_preview_since_id"))
        for m in ms:
            if P.parse(m.get("text", ""), m.get("quoted_tweet_id")).kind == "mog_me":
                print(m["id"], flow.handle(m))
        if newest:
            store.put("mogme_preview_since_id", newest)
        return 0
    if cmd == "mogme-list":
        hits = sum(1 for r in store.ledger("mogme_budget_hit") if r["ts"] >= time.time() - 86_400)
        print(f"mog-me spend today ${flow.spent()} of ${p.raw['mog_me']['daily_budget_usd']} "
              f"(ceiling ${p.raw['mog_me']['daily_budget_max_usd']}); budget hits last 24h: {hits}")
        for it in flow.items(args[0] if args else None):
            print(it["mention_id"], it["status"], it["handle"], it["image_path"], it["reply_text"])
        return 0
    try:
        if cmd == "mogme-approve":
            print(flow.approve(args[0], args[1], " ".join(args[2:]))["status"]); return 0
        if cmd == "mogme-reject":
            print(flow.reject(args[0], args[1], " ".join(args[2:]))["status"]); return 0
        if cmd == "mogme-ref":
            print(flow.add_ref(args[0], os.environ.get("JMI_MOGME_REFS", "mogme_refs"))); return 0
    except (IndexError, MogMeError) as err:
        print("refused:", err); return 2
    if cmd == "mogme-post":
        if flow.killed():
            print("kill switch is ON: nothing posted"); return 0
        print(flow.post_ready()); return 0
    print(__doc__); return 2

def main(argv):
    if not argv:
        print(__doc__); return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "loop":
        return loop("--dry-run" in args)
    store = Store(os.environ.get("JMI_DB", "justmogit.db"))
    p = load()
    if cmd.startswith("mogme-"):
        return mogme_cmd(cmd, args)
    if cmd == "status":
        e = Engine(store, p, Adapters())
        print({"kill": e.killed(), "bankroll_sol": str(Decimal(store.get("bankroll_available", 0)) / 10**9),
               "launches_today": e._launches_today(), "holder_liability_lamports": fees.holder_liability(store)})
        for j in store.jobs(*[s for s in ("PAYMENT_PENDING", "SPELLING_PENDING", "RESERVED", "ARMED", "UNKNOWN",
                                         "CONFIRMED")]):
            pl = store.payload(j["job_id"]).get("dossier") or {}
            print(j["job_id"], j["state"], pl.get("ticker"), "approved" if j["approved_by"] else "")
        return 0
    if cmd == "kill":
        Engine(store, p, Adapters()).set_kill(args[0] == "on"); print("kill", args[0]); return 0
    _, a, wallet, rpc = build(False, store)
    e = Engine(store, p, a)
    if cmd == "approve":
        e.approve(args[0], "owner"); print("approved", args[0]); return 0
    if cmd == "launch":
        try:
            e.arm(args[0]); print(e.fire(args[0]))
        except Refused as r:
            print("refused:", r.code, r.kw); return 1
        return 0
    if cmd == "bankroll":
        want, real = fees.lamports(args[0]), rpc.balance(wallet)
        held = store.db.execute("SELECT COALESCE(SUM(lamports),0) FROM reservations WHERE state='HELD'").fetchone()[0]
        val = min(want, max(real - held - fees.holder_liability(store), 0))
        store.put("bankroll_available", val); print("bankroll lamports", val, "(wallet", real, ")"); return 0
    print(__doc__); return 2

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
