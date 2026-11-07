"""Engine flow tests with fake adapters. Zero network, zero keys, zero real money."""
import io
import socket
import unittest

from PIL import Image

from bot.adapters import AdapterUnwired, Adapters
from bot.engine import BootRefused, Engine, Refused
from bot.policy import load
from bot.store import Store

socket.socket = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network forbidden in tests"))
CA = "So11111111111111111111111111111111111111112"
CA2 = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SOL = 1_000_000_000

class Clock:
    t = 1_800_000_000.0
    def __call__(self): return self.t

class X:
    def __init__(self): self.replies, self.media = [], []
    def reply(self, tweet_id, text, media_ids=None, idempotency_key=None):
        self.replies.append((tweet_id, text, media_ids)); return f"r{len(self.replies)}"
    def profile_image(self, uid):
        b = io.BytesIO(); Image.new("RGB", (400, 400), (200, 180, 160)).save(b, "PNG"); return b.getvalue()
    def upload_media(self, png, alt_text): self.media.append((png, alt_text)); return "m1"

class Dossier:
    def __init__(self, coin=None, snap=None):
        from bot.rug import CoinSnapshot
        self.coin = coin or {"mint": CA, "name": "Bingus", "symbol": "BINGUS", "description": "cat coin",
                             "image_uri": "https://ipfs.io/ipfs/abc", "twitter": "https://x.com/bingus"}
        self.snap = snap or CoinSnapshot(quote_symbol="SOL", venue="pump.fun", graduated=True,
                                         lp_pulled=True, price_drop_pct_window=90)
    def das_asset(self, ca): return {"mint": ca, "name": self.coin["name"], "symbol": self.coin["symbol"]}
    def pump_coin(self, ca): return {**self.coin, "mint": ca}
    def snapshot(self, ca): return self.snap
    def inspect_image(self, uri): return {"nsfw_score": 0.0}
    def fee_weight(self, mint): return 1000.0

class Pair:
    def __init__(self, clock, ok=True): self.clock, self.ok = clock, ok
    def probe(self, quote): return {"launchable": self.ok, "expires_at": self.clock() + 60}

class Pump:
    def __init__(self): self.built, self.collected = [], 0
    def build_create(self, **kw): self.built.append(kw); return b"TX"
    def claimable(self): return 40_000_000
    def claim_cost(self): return 5_000
    def build_claim(self): self.collected += 1; return b"CLAIM"

class Guard:
    def __init__(self, ok=True): self.ok = ok
    def inspect(self, tx, **kw):
        if not self.ok: raise ValueError("guard: name mismatch")
    def inspect_claim(self, tx):
        if tx != b"CLAIM": raise ValueError("not a claim")

class Vault:
    def new_mint(self, job_id): return "MintPubkey" + job_id[:6]
    def sign(self, tx, job_id): return b"SIGNED"
    def signature_of(self, signed): return "SIG1"

class Rpc:
    def __init__(self, cost=20_300_000, status="confirmed", expired=False):
        self.cost, self.st, self.expired, self.sent = cost, status, expired, 0
    def simulate(self, tx):
        if tx == b"CLAIM": return {"ok": True, "payer_lamports_delta": -40_000_000}
        return {"ok": True, "payer_lamports_delta": self.cost}
    def send(self, signed): self.sent += 1
    def status(self, sig, timeout_s): return self.st
    def blockhash_expired(self, sig): return self.expired

class Payments:
    def __init__(self, paid=True): self.paid = paid
    def job_address(self, job_id): return "PayAddr" + job_id[:6]
    def verify(self, job_id, lamports): return {"ok": True, "signature": "PAY", "payer": "P"} if self.paid else None

def make(policy_over=None, bankroll=5 * SOL, **adapters):
    clock = Clock()
    p = load(overrides={"allowlist": ["111", "222", "333", "444"], **(policy_over or {})})
    x = adapters.pop("x", X())
    base = dict(x=x, dossier=Dossier(), pair=Pair(clock), pump=Pump(), guard=Guard(), vault=Vault(),
                rpc=Rpc(), payments=Payments())
    base.update(adapters)
    s = Store(clock=clock)
    e = Engine(s, p, Adapters(**base), clock=clock)
    e.boot()
    s.put("bankroll_available", bankroll)
    e.set_kill(False)
    return e, s, x, clock

def mention(i, uid="111", text=None):
    return {"id": str(i), "author_id": uid, "handle": "u" + uid, "text": text or f"@justmogit_bot just mog it {CA}"}

def run_to_reserved(e, m):
    st, _ = e.ingest(m)
    assert st == "PAYMENT_PENDING", st
    job = e.s.jobs("PAYMENT_PENDING")[-1]["job_id"]
    return job, e.payment_check(job)

class TestBoot(unittest.TestCase):
    def test_public_boot_needs_x_approval(self):
        e = Engine(Store(), load(), Adapters(), public=True)
        with self.assertRaises(BootRefused):
            e.boot()
        Engine(Store(), load(overrides={"x_reply_approval_ref": "X-CASE-1"}), Adapters(), public=True).boot()

    def test_kill_on_at_boot_ignores_everything(self):
        s = Store(); e = Engine(s, load(overrides={"allowlist": ["111"]}), Adapters()); e.boot()
        self.assertTrue(e.killed())
        self.assertEqual(e.ingest(mention(1)), ("ignored", "kill"))
        self.assertEqual(s.jobs(), [])

    def test_unwired_adapters_fail_closed_before_money(self):
        s = Store(); e = Engine(s, load(overrides={"allowlist": ["111"]}), Adapters()); e.boot()
        s.put("bankroll_available", 5 * SOL); e.set_kill(False)
        with self.assertRaises(AdapterUnwired):
            e.ingest(mention(1))
        self.assertEqual(s.get("bankroll_available"), 5 * SOL)
        self.assertEqual(s.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 0)

class TestHappyPath(unittest.TestCase):
    def test_full_launch(self):
        e, s, x, _ = make()
        job, st = run_to_reserved(e, mention(1))
        self.assertEqual(st, ("RESERVED", "awaiting owner approval"))
        self.assertEqual(s.get("bankroll_available"), 5 * SOL - 25_000_000)
        with self.assertRaises(Refused):
            e.arm(job)                                             # no owner yes yet
        e.approve(job, "sneaks")
        e.arm(job)
        st, mint = e.fire(job)
        self.assertEqual(st, "LEDGERED")
        self.assertEqual(s.get("bankroll_available"), 5 * SOL - 20_300_000)   # unused hold refunded
        row = s.ledger("launch")[0]
        self.assertEqual((row["mint"], row["lamports"]), (mint, 20_300_000))
        self.assertIn(mint, x.replies[-1][1]); self.assertNotIn("http", x.replies[-1][1])
        built = e.a.pump.built[0]
        self.assertEqual((built["name"], built["symbol"], built["dev_buy_sol"]), ("Bingus", "BINGUS", "0"))
        self.assertTrue(built["description"].startswith("$BINGUS was live on the wrong pair"))

class TestGates(unittest.TestCase):
    def test_not_allowlisted_silent(self):
        e, s, x, _ = make()
        self.assertEqual(e.ingest(mention(1, uid="999")), ("REJECTED", "not_allowlisted"))
        self.assertEqual(x.replies, [])

    def test_hard_skip(self):
        e, s, x, _ = make()
        self.assertEqual(e.ingest(mention(1, text=f"@justmogit_bot just mog it {CA} RIP")), ("REJECTED", "hard_skip"))

    def test_dedupe_same_tweet_and_same_target(self):
        e, s, x, _ = make()
        e.ingest(mention(1))
        self.assertEqual(e.ingest(mention(1)), ("duplicate_tweet", None))
        self.assertEqual(e.ingest(mention(2, uid="222")), ("REJECTED", "duplicate"))

    def test_already_mogged(self):
        e, s, x, _ = make()
        job, _ = run_to_reserved(e, mention(1)); e.approve(job, "s"); e.arm(job); e.fire(job)
        self.assertEqual(e.ingest(mention(2, uid="222")), ("REJECTED", "duplicate"))
        self.assertIn("Already mogged", x.replies[-1][1])

    def test_cooldown(self):
        e, s, x, c = make(dossier=Dossier())
        e.ingest(mention(1, text=f"just mog it {CA}"))
        self.assertEqual(e.ingest(mention(2, text=f"just mog it {CA2}")), ("REJECTED", "cooldown"))

    def test_daily_cap_under_lock(self):
        e, s, x, c = make()
        cas = [CA, CA2, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCbVQjFGNoY", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]
        res = []
        for i, ca in enumerate(cas):
            r = e.ingest(mention(i, uid=str(111 * (i + 1)), text=f"just mog it {ca}"))
            if r[0] == "PAYMENT_PENDING":
                res.append(e.payment_check(s.jobs("PAYMENT_PENDING")[-1]["job_id"]))
            else:
                res.append(r)
        self.assertEqual([r[0] for r in res], ["RESERVED", "RESERVED", "RESERVED", "REJECTED"])
        self.assertEqual(res[3][1], "cap")

    def test_paid_but_capped_logs_refund_owed(self):
        e, s, x, _ = make(policy_over={"launches_per_day": 1})
        e.ingest(mention(1)); j1 = s.jobs("PAYMENT_PENDING")[0]["job_id"]
        self.assertEqual(e.ingest(mention(2, uid="222", text=f"just mog it {CA2}")), ("REJECTED", "cap"))
        s.db.execute("UPDATE jobs SET state='QUEUED' WHERE job_id=?", (j1,))     # simulate a race past gate
        s.db.execute("INSERT INTO reservations(job_id,lamports,state,day,created_at,expires_at) "
                     "VALUES('other',1,'HELD',?,0,9e18)", (e.day(),))
        s.db.execute("UPDATE jobs SET state='PAYMENT_PENDING' WHERE job_id=?", (j1,))
        self.assertEqual(e.payment_check(j1), ("REJECTED", "cap"))
        self.assertEqual(len(s.ledger("fee_refund_owed")), 1)

    def test_bankroll_low(self):
        e, s, x, _ = make(bankroll=10_000_000)
        self.assertEqual(e.ingest(mention(1)), ("REJECTED", "bankroll"))

    def test_payment_required(self):
        e, s, x, _ = make(payments=Payments(paid=False))
        e.ingest(mention(1)); job = s.jobs()[0]["job_id"]
        self.assertEqual(e.payment_check(job), "PAYMENT_PENDING")
        self.assertEqual(s.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 0)

    def test_not_eligible(self):
        from bot.rug import CoinSnapshot
        e, s, x, _ = make(dossier=Dossier(snap=CoinSnapshot(quote_symbol="SOL", price_drop_pct_window=95)))
        self.assertEqual(e.ingest(mention(1)), ("REJECTED", "not_eligible"))
        self.assertFalse(any("SOL to" in r[1] for r in x.replies))      # never asked to pay

class TestMoneySafety(unittest.TestCase):
    def _armed(self, **kw):
        e, s, x, c = make(**kw)
        job, _ = run_to_reserved(e, mention(1)); e.approve(job, "s"); e.arm(job)
        return e, s, job

    def test_over_cost_cap_refused_and_released(self):
        e, s, job = self._armed(rpc=Rpc(cost=30_000_000))
        with self.assertRaises(Refused):
            e.fire(job)
        self.assertEqual(s.job(job)["state"], "REJECTED")
        self.assertEqual(s.get("bankroll_available"), 5 * SOL)

    def test_guard_mismatch_never_signs(self):
        e, s, job = self._armed(guard=Guard(ok=False))
        with self.assertRaises(Refused):
            e.fire(job)
        self.assertEqual(e.a.rpc.sent, 0)
        self.assertEqual(s.get("bankroll_available"), 5 * SOL)

    def test_unknown_keeps_hold_survives_kill_then_reconciles(self):
        e, s, job = self._armed(rpc=Rpc(status=None))
        self.assertEqual(e.fire(job), ("UNKNOWN", None))
        e.set_kill(True)
        self.assertEqual(s.job(job)["state"], "UNKNOWN")
        self.assertEqual(s.get("bankroll_available"), 5 * SOL - 25_000_000)
        e.a.rpc.st = "finalized"
        self.assertEqual(e.reconcile(job)[0], "LEDGERED")
        self.assertEqual(e.a.rpc.sent, 1)                       # no rebuild, no second mint

    def test_unknown_rebuild_same_mint_only_after_expiry(self):
        e, s, job = self._armed(rpc=Rpc(status=None))
        e.fire(job); mint = s.job(job)["mint"]
        self.assertEqual(e.reconcile(job)[0], "UNKNOWN")         # blockhash not expired: wait
        e.a.rpc.expired, e.a.rpc.st = True, None
        e.reconcile(job)
        self.assertEqual(s.job(job)["mint"], mint)
        self.assertEqual(e.a.rpc.sent, 2)

    def test_failed_tx_releases(self):
        e, s, job = self._armed(rpc=Rpc(status="failed"))
        self.assertEqual(e.fire(job), ("FAILED", None))
        self.assertEqual(s.get("bankroll_available"), 5 * SOL)

    def test_kill_cancels_reserved_and_releases(self):
        e, s, x, c = make()
        job, _ = run_to_reserved(e, mention(1))
        e.set_kill(True)
        self.assertEqual(s.job(job)["state"], "CANCELLED")
        self.assertEqual(s.get("bankroll_available"), 5 * SOL)

    def test_reservation_expiry(self):
        e, s, x, c = make()
        job, _ = run_to_reserved(e, mention(1))
        c.t += 11 * 60
        self.assertEqual(e.expire(), [job])
        self.assertEqual(s.get("bankroll_available"), 5 * SOL)

class TestFeeLoop(unittest.TestCase):
    def test_sweep_books_split(self):
        e, s, x, _ = make()
        job, _ = run_to_reserved(e, mention(1)); e.approve(job, "s"); e.arm(job); e.fire(job)
        before = s.get("bankroll_available")
        (mint, parts), = e.sweep_fees()
        self.assertEqual(parts, {"mogger": 10_000_000, "holders": 10_000_000, "bankroll": 20_000_000})
        self.assertEqual(s.get("bankroll_available"), before + 20_000_000)
        from bot import fees
        self.assertEqual(fees.holder_liability(s), 10_000_000)
        self.assertEqual(fees.mogger_payable(s, e.p, mint, "111", "WALLET"), 10_000_000)
        self.assertEqual(fees.mogger_payable(s, e.p, mint, "111", None), 0)

class TestMogMe(unittest.TestCase):
    def test_disabled_by_default(self):
        e, s, x, _ = make()
        self.assertEqual(e.ingest(mention(1, text="@justmogit_bot mog me")), ("ignored", "mog_me_disabled"))

    def test_enabled_flow_and_limit(self):
        class Faces:
            def eyes(self, b): return ((170, 190), (230, 190))
        e, s, x, _ = make(policy_over={"mog_me_enabled": True}, faces=Faces())
        self.assertEqual(e.ingest(mention(1, text="@justmogit_bot mog me")), ("mog_me_done", "m1"))
        self.assertEqual(x.replies[-1][2], ["m1"])
        self.assertIn("wraparound", x.media[0][1])
        self.assertEqual(e.ingest(mention(2, text="@justmogit_bot mog me")), ("ignored", "rate_limited"))

    def test_no_face(self):
        class Faces:
            def eyes(self, b): return None
        e, s, x, _ = make(policy_over={"mog_me_enabled": True}, faces=Faces())
        self.assertEqual(e.ingest(mention(1, text="@justmogit_bot mog me")), ("mog_me_failed", "no_face"))
        self.assertEqual(x.media, [])

if __name__ == "__main__":
    unittest.main()
