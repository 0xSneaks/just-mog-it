"""Tests for the money-core fixes (claude-code review 2026-11-04 + tester HOLEs).
FIX 1 guard decode/assert, FIX 2 sim-vs-quote + actual cost, FIX 3 pending
reservations, FIX 4 confirm lock, plus the terms_hash/header secondary fixes.
Mocks only — no network, no keys, no mainnet."""
import base64, json, os, socket, tempfile, threading, unittest
from decimal import Decimal
from pathlib import Path
from mogit.guard import inspect, GuardError, parse, _cu16, b58, CREATE_DISCRIMINATOR
from mogit.ledger import Ledger, LedgerError
from mogit.flow import Session

FX = Path(__file__).parent / "fixtures"
TREMP = (FX / "tremp_create.b64").read_text().strip()
SA = (FX / "super_agent_create.b64").read_text().strip()
_real_socket = socket.socket
_real_getaddrinfo = socket.getaddrinfo

def _no_socket(*a, **k):
    raise AssertionError("network used in tests (socket)")

def _no_getaddrinfo(*a, **k):
    raise AssertionError("network used in tests (getaddrinfo)")

class M:
    def __init__(self, tx=TREMP, sim=None, ok=True, sol="0.02059164", usd="1.93", mint=None, submit_fn=None):
        self.tx = tx
        self.simr = sim if sim is not None else {"payer_lamports_delta": -20591640}
        self.ok = ok
        self.q = {"sol": sol, "usd": usd, "mint": mint or "EarneMetyCsEqac2EEiLUh3VdR6bMre6cqM9rftNc6jB"}
        self.sent = []
        self.submit_fn = submit_fn
    def quote(self): return dict(self.q)
    def build(self, c, q): return self.tx
    def simulate(self, tx): return dict(self.simr)
    def submit(self, signed):
        self.sent.append(signed)
        if self.submit_fn: return self.submit_fn(signed)
        return {"ok": self.ok, "sig": "MOCKSIG", "err": "x"}

class Chain:
    def __init__(self, b="1"): self.b = b
    def balance_sol(self): return self.b

class Vault:
    def sign(self, tx, th): return "SIGNED:" + th

class SignedVault:
    """Returns the built tx as-is: the fixtures are real signed txs, so the first
    signature extracted from the signed payload is a real, checkable value (C2)."""
    def sign(self, tx, th): return tx

def first_sig_of(tx_b64):
    raw = base64.b64decode(tx_b64)
    n, i = _cu16(raw, 0)
    return b58(raw[i:i + 64])

class Img:
    def verify(self, p): return "deadbeef"

def make_ledger(spent="15.91", cap="30"):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "ledger.json")
    Path(p).write_text(json.dumps({"cap_usd": cap, "spent_usd": spent}))
    return Ledger(p), p

def run(sess, *msgs):
    out = None
    for m in msgs:
        out = sess.handle(m)
    return out

HAPPY = ["/mogit", "Grok Coin", "GROK", "Grok Coin. Launched by grok-bot.", "none"]

def split_tx(raw):
    """(prefix ending just before the ix-data length, data) for the single instruction."""
    i = 0
    n, i = _cu16(raw, i); i += 64 * n
    i += 3
    nk, i = _cu16(raw, i); i += 32 * nk + 32
    nix, i = _cu16(raw, i)
    assert nix == 1
    i += 1
    na, i = _cu16(raw, i); i += na
    nd, i = _cu16(raw, i)
    return raw[:i - 1], bytes(raw[i:i + nd])  # fixtures encode nd in one byte

def cu16(v):
    out = bytearray()
    while True:
        b = v & 0x7F; v >>= 7
        if v: out.append(b | 0x80)
        else: out.append(b); return bytes(out)

def rebuild(raw, new_data):
    prefix, _ = split_tx(raw)
    return prefix + cu16(len(new_data)) + new_data

def borsh_data(name, symbol, uri):
    out = CREATE_DISCRIMINATOR
    for s in (name, symbol, uri):
        b = s.encode()
        out += len(b).to_bytes(4, "little") + b
    return out

TREMP_URI = "https://gateway.pinata.cloud/ipfs/Qmb7UwmF3wEHugQhkGfCJE1S4a4rfdGRzVEegBBiDGskRH"

class Fixes(unittest.TestCase):
    def setUp(self):
        socket.socket = _no_socket
        socket.getaddrinfo = _no_getaddrinfo
    def tearDown(self):
        socket.socket = _real_socket
        socket.getaddrinfo = _real_getaddrinfo

    # --- FIX 1: guard decodes + asserts the coin the tx actually creates ---
    def test_fixtures_decode_to_their_real_coin(self):
        g = inspect(TREMP)
        self.assertEqual((g["name"], g["symbol"], g["uri"]), ("Doland Tremp", "TREMP", TREMP_URI))
        g = inspect(SA, expected_name="Super-Agent", expected_symbol="SA")
        self.assertEqual(g["name"], "Super-Agent")

    def test_renamed_token_refused(self):
        raw = base64.b64decode(TREMP)
        evil = rebuild(raw, borsh_data("Doland Tremq", "TREMP", TREMP_URI))
        self.assertEqual(inspect(evil)["name"], "Doland Tremq")  # decode itself works
        with self.assertRaises(GuardError):
            inspect(evil, expected_name="Doland Tremp")

    def test_swapped_symbol_refused(self):
        with self.assertRaises(GuardError):
            inspect(TREMP, expected_symbol="TREMPP")
        raw = base64.b64decode(TREMP)
        evil = rebuild(raw, borsh_data("Doland Tremp", "FAKE", TREMP_URI))
        with self.assertRaises(GuardError):
            inspect(evil, expected_symbol="TREMP")

    def test_swapped_uri_host_refused_by_allowlist(self):
        raw = base64.b64decode(TREMP)
        evil_uri = TREMP_URI.replace("gateway.pinata.cloud", "gateway.pinata.cloux")
        evil = rebuild(raw, borsh_data("Doland Tremp", "TREMP", evil_uri))
        with self.assertRaises(GuardError):
            inspect(evil)  # allowlist enforced even with no expected name/symbol
        self.assertEqual(inspect(evil, uri_hosts={"gateway.pinata.cloux"})["uri"], evil_uri)

    def test_trailing_data_after_fields_refused(self):
        raw = base64.b64decode(TREMP)
        _, data = split_tx(raw)
        evil = rebuild(raw, data + b"\x00")
        with self.assertRaises(GuardError):
            inspect(evil)

    def test_truncated_borsh_refused(self):
        raw = base64.b64decode(TREMP)
        _, data = split_tx(raw)
        with self.assertRaises(GuardError):
            inspect(rebuild(raw, data[:-2]))

    # --- secondary: message header + signature count pinned ---
    def test_header_tamper_refused(self):
        raw = bytearray(base64.b64decode(TREMP))
        raw[1 + 64 * raw[0] + 2] = 7  # readonly-unsigned count 8 -> 7
        with self.assertRaises(GuardError):
            inspect(bytes(raw))
        raw = bytearray(base64.b64decode(TREMP))
        raw[1 + 64 * raw[0]] = 3  # required signatures 2 -> 3 (sig count no longer matches)
        with self.assertRaises(GuardError):
            inspect(bytes(raw))

    # --- FIX 2: simulated spend checked against the quote; actual cost committed ---
    def test_sim_spend_over_quote_refused(self):
        L, _ = make_ledger()
        m = M(sim={"payer_lamports_delta": -30_000_000})  # quote is 20,591,640 lamports
        sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        out = run(sess, *HAPPY)
        self.assertIn("REFUSED", out)
        self.assertIn("exceeds quote", out)
        self.assertEqual(m.sent, [])
        self.assertEqual(str(L.load()["spent_usd"]), "15.91")
        self.assertEqual(L.load()["pending"], [])

    def test_sim_positive_delta_refused(self):
        m = M(sim={"payer_lamports_delta": 5})
        sess = Session("grok-bot", make_ledger()[0], m, Chain(), Vault(), Img())
        self.assertIn("REFUSED", run(sess, *HAPPY))
        self.assertEqual(m.sent, [])

    def test_actual_cost_committed_not_quote(self):
        L, _ = make_ledger()
        m = M(sim={"payer_lamports_delta": -20_641_640})  # 50,000 lamports over quote, inside tolerance
        sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        out = run(sess, *HAPPY, "LAUNCH")
        self.assertIn("LAUNCHED", out)
        spent = L.load()["spent_usd"]
        self.assertGreater(spent, Decimal("17.84"))  # more than the quoted 1.93 on top of 15.91
        self.assertLess(spent, Decimal("17.85"))
        recorded = Decimal(L.load()["launches"][0]["cost_usd"])
        self.assertLess(abs(recorded - (spent - Decimal("15.91"))), Decimal("0.000001"))
        self.assertNotEqual(recorded, Decimal("1.93"))  # not the quoted USD

    def test_delta_zero_sim_commits_the_quote(self):
        # C1 (claude-code re-review): a sim returning payer_lamports_delta 0 must not
        # record $0.00 — the committed cost is max(quote, simulated) = the quote.
        L, _ = make_ledger()
        m = M(sim={"payer_lamports_delta": 0})
        sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        out = run(sess, *HAPPY, "LAUNCH")
        self.assertIn("LAUNCHED", out)
        self.assertEqual(str(L.load()["spent_usd"]), "17.84")  # 15.91 + quoted 1.93, not 15.91 + 0
        self.assertEqual(L.load()["launches"][0]["cost_usd"], "1.93")

    # --- FIX 3: pending reservation before submit, reconciled after ---
    def test_reservation_counts_against_cap(self):
        L, _ = make_ledger(spent="28.07")
        rid = L.reserve("1.93", {"mint": "M"})
        self.assertEqual(L.load()["remaining_usd"], Decimal("0"))
        with self.assertRaises(LedgerError):
            L.check("0.01")
        self.assertTrue(L.release(rid))
        self.assertEqual(L.load()["remaining_usd"], Decimal("1.93"))

    def test_settle_converts_reservation_to_spend(self):
        L, _ = make_ledger()
        rid = L.reserve("1.93", {"mint": "M"})
        L.settle(rid, {"mint": "M", "tx": "SIG"})
        st = L.load()
        self.assertEqual(str(st["spent_usd"]), "17.84")
        self.assertEqual(st["pending"], [])
        self.assertEqual(st["launches"][0]["tx"], "SIG")
        with self.assertRaises(LedgerError):
            L.settle(rid, {"mint": "M"})  # already settled

    def test_failed_submit_holds_reservation_unconfirmed(self):
        # C2 (claude-code re-review): ok:false is NOT a definitive failure — a send
        # reported failed can still land. The reservation stays, marked unconfirmed,
        # with the signed tx's first signature recorded in the pending entry.
        L, _ = make_ledger()
        m = M(ok=False)
        sess = Session("grok-bot", L, m, Chain(), SignedVault(), Img())
        out = run(sess, *HAPPY, "LAUNCH")
        self.assertIn("unconfirmed", out)
        st = L.load()
        self.assertEqual(len(st["pending"]), 1)
        entry = st["pending"][0]
        self.assertEqual(entry["status"], "unconfirmed")
        self.assertEqual(entry["tx_sig"], first_sig_of(TREMP))  # recorded before submit
        self.assertEqual(str(st["spent_usd"]), "15.91")         # nothing settled
        self.assertEqual(st["remaining_usd"], Decimal("30") - Decimal("15.91") - Decimal("1.93"))
        self.assertEqual(st["launches"], [])

    def test_pre_send_rejection_releases_reservation(self):
        # C2: the ONLY ok:false path that releases — a definitive pre-send rejection
        # (preflight/simulation refused, never broadcast), flagged pre_send by the adapter.
        def pre_send_fail(signed):
            return {"ok": False, "sig": None, "err": "preflight refused", "pre_send": True}
        L, _ = make_ledger()
        m = M(submit_fn=pre_send_fail)
        sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        out = run(sess, *HAPPY, "LAUNCH")
        self.assertIn("no budget spent", out)
        self.assertEqual(L.load()["pending"], [])
        self.assertEqual(str(L.load()["spent_usd"]), "15.91")

    def test_reconcile_releases_unconfirmed_reservation(self):
        # C2: an unconfirmed reservation is released only via the reconcile path —
        # release() is that mechanism (expired blockhash + signature not found).
        L, _ = make_ledger()
        m = M(ok=False)
        sess = Session("grok-bot", L, m, Chain(), SignedVault(), Img())
        run(sess, *HAPPY, "LAUNCH")
        st = L.load()
        self.assertEqual(len(st["pending"]), 1)
        rid = st["pending"][0]["id"]
        self.assertTrue(L.release(rid))  # reconcile: blockhash expired, tx_sig not found on-chain
        st = L.load()
        self.assertEqual(st["pending"], [])
        self.assertEqual(st["remaining_usd"], Decimal("30") - Decimal("15.91"))
        self.assertEqual(str(st["spent_usd"]), "15.91")

    def test_submit_exception_holds_reservation(self):
        def boom(signed): raise RuntimeError("timeout after send")
        L, _ = make_ledger()
        m = M(submit_fn=boom)
        sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        out = run(sess, *HAPPY, "LAUNCH")
        self.assertIn("REFUSED", out)
        self.assertIn("held against the cap", out)
        st = L.load()
        self.assertEqual(len(st["pending"]), 1)
        self.assertEqual(st["remaining_usd"], Decimal("30") - Decimal("15.91") - Decimal("1.93"))
        self.assertEqual(str(st["spent_usd"]), "15.91")

    def test_settle_failure_after_send_keeps_pending(self):
        class BrokenSettle(Ledger):
            def settle(self, rid, record): raise LedgerError("disk on fire")
        d = tempfile.mkdtemp(); p = os.path.join(d, "ledger.json")
        Path(p).write_text(json.dumps({"cap_usd": "30", "spent_usd": "15.91"}))
        L = BrokenSettle(p)
        m = M()
        sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        out = run(sess, *HAPPY, "LAUNCH")
        self.assertIn("LAUNCHED", out)
        self.assertIn("WARNING", out)
        self.assertEqual(len(m.sent), 1)             # the send really happened
        st = L.load()
        self.assertEqual(len(st["pending"]), 1)      # ...and its cost is still held against the cap
        self.assertEqual(str(st["spent_usd"]), "15.91")

    # --- FIX 4: one session in confirm at a time ---
    def test_concurrent_confirms_never_double_spend(self):
        L, _ = make_ledger(spent="28.07")  # room for exactly one 1.93 launch
        entered, release = threading.Event(), threading.Event()
        def slow_submit(signed):
            entered.set()
            release.wait(10)
            return {"ok": True, "sig": "SIGA", "err": None}
        ma = M(submit_fn=slow_submit)
        mb = M()
        sa = Session("grok-bot", L, ma, Chain(), Vault(), Img())
        sb = Session("grok-bot", L, mb, Chain(), Vault(), Img())
        self.assertIn("terms_hash", run(sa, *HAPPY))
        self.assertIn("terms_hash", run(sb, *HAPPY))
        results = {}
        ta = threading.Thread(target=lambda: results.__setitem__("a", sa.handle("LAUNCH")))
        tb = threading.Thread(target=lambda: results.__setitem__("b", sb.handle("LAUNCH")))
        ta.start()
        self.assertTrue(entered.wait(5))       # A is inside submit, holding the lock + reservation
        self.assertEqual(len(L.load()["pending"]), 1)
        tb.start()
        tb.join(0.5)
        self.assertTrue(tb.is_alive())         # B is blocked on the lock, not racing the cap check
        self.assertEqual(mb.sent, [])
        release.set()
        ta.join(5); tb.join(5)
        self.assertIn("LAUNCHED", results["a"])
        self.assertIn("REFUSED", results["b"])  # cap was consumed by A before B could check
        self.assertEqual(mb.sent, [])
        st = L.load()
        self.assertEqual(str(st["spent_usd"]), "30.00")
        self.assertEqual(len(st["launches"]), 1)
        self.assertEqual(st["pending"], [])

    # --- secondary: terms_hash binds the exact tx bytes ---
    def test_terms_hash_binds_tx_bytes(self):
        m = M()
        sess = Session("grok-bot", make_ledger()[0], m, Chain(), Vault(), Img())
        self.assertIn("terms_hash", run(sess, *HAPPY))
        sess.tx = SA  # swap the built tx after the card was approved
        out = sess.handle("LAUNCH")
        self.assertIn("REFUSED", out)
        self.assertIn("terms changed", out)
        self.assertEqual(m.sent, [])

if __name__ == "__main__":
    unittest.main()
