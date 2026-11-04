"""Mocks only. No network, no keys, no mainnet sends (gate 4)."""
import base64, json, os, socket, tempfile, unittest
from pathlib import Path
from mogit.guard import inspect, GuardError, parse
from mogit.ledger import Ledger, LedgerError
from mogit.flow import Session

FX = Path(__file__).parent / "fixtures"
TREMP = (FX / "tremp_create.b64").read_text().strip()
SA = (FX / "super_agent_create.b64").read_text().strip()
_real = socket.socket
def _no_net(*a, **k): raise AssertionError("network used in tests")

class M:
    def __init__(s, tx=TREMP, sim=None, ok=True, sol="0.02059164", usd="1.93"):
        s.tx, s.simr, s.ok, s.q, s.sent = tx, sim or {"payer_lamports_delta": -20591640}, ok, {"sol": sol, "usd": usd,
            "mint": "EarneMetyCsEqac2EEiLUh3VdR6bMre6cqM9rftNc6jB"}, []
    def quote(s): return s.q
    def build(s, c, q): return s.tx
    def simulate(s, tx): s.simulated = True; return s.simr
    def submit(s, signed): s.sent.append(signed); return {"ok": s.ok, "sig": "MOCKSIG", "err": "x"}
class Chain:
    def __init__(s, b="1"): s.b = b
    def balance_sol(s): return s.b
class Vault:
    def sign(s, tx, th): return "SIGNED:" + th
class Img:
    def verify(s, p): return None

def ledger(spent="15.91"):
    d = tempfile.mkdtemp(); p = os.path.join(d, "ledger.json")
    Path(p).write_text(json.dumps({"cap_usd": "30", "spent_usd": spent})); return Ledger(p)

def run(sess, *msgs):
    out = None
    for m in msgs: out = sess.handle(m)
    return out
HAPPY = ["/mogit", "Grok Coin", "GROK", "Grok Coin. Launched by grok-bot.", "none"]

class T(unittest.TestCase):
    def setUp(s): socket.socket = _no_net
    def tearDown(s): socket.socket = _real
    # gate 3: real txs pass, tampering fails
    def test_real_txs_pass(s):
        s.assertEqual(inspect(TREMP)["mint"], "EarneMetyCsEqac2EEiLUh3VdR6bMre6cqM9rftNc6jB")
        s.assertEqual(inspect(SA)["mint"], "5kDrkzSEGH8jJK2ibjFDX2QEHVuUzPNuRc7eVUpVrzSM")
    def test_tampered_program_refused(s):
        raw = bytearray(base64.b64decode(TREMP)); k = parse(bytes(raw))["keys"]
        # flip one byte inside the last account key (program id)
        raw = bytearray(base64.b64decode(TREMP)); off = 1 + 64 * raw[0] + 3 + 1 + 32 * (len(k) - 1) + 5
        raw[off] ^= 1
        with s.assertRaises(GuardError): inspect(bytes(raw))
    def test_wrong_mint_refused(s):
        with s.assertRaises(GuardError): inspect(TREMP, expected_mint="So11111111111111111111111111111111111111112")
    def test_truncated_refused(s):
        with s.assertRaises(GuardError): inspect(base64.b64decode(TREMP)[:-3])
    # gate 1
    def test_missing_ledger_refuses(s):
        sess = Session("grok-bot", Ledger("/nonexistent/l.json"), M(), Chain(), Vault(), Img())
        s.assertIn("REFUSED", sess.handle("/mogit"))
    def test_cap_hit(s):
        sess = Session("grok-bot", ledger("29.50"), M(), Chain(), Vault(), Img())
        s.assertIn("budget cap hit", sess.handle("/mogit"))
    def test_underfunded(s):
        sess = Session("grok-bot", ledger(), M(), Chain("0.001"), Vault(), Img())
        s.assertIn("underfunded", sess.handle("/mogit"))
    def test_ledger_only_after_success(s):
        L = ledger(); m = M(ok=False); sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        out = run(sess, *HAPPY, "LAUNCH"); s.assertIn("no budget spent", out)
        s.assertEqual(str(L.load()["spent_usd"]), "15.91")
    def test_happy_path_commits(s):
        L = ledger(); m = M(); sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        card = run(sess, *HAPPY); s.assertIn("terms_hash", card); s.assertTrue(m.simulated)
        s.assertIn("LAUNCHED", sess.handle("LAUNCH")); s.assertEqual(str(L.load()["spent_usd"]), "17.84")
    # gate 2
    def test_sim_fail_no_send(s):
        m = M(sim={"err": "InsufficientFunds"}); sess = Session("grok-bot", ledger(), m, Chain(), Vault(), Img())
        s.assertIn("simulation failed", run(sess, *HAPPY)); s.assertEqual(m.sent, [])
    # human gate + rules
    def test_no_launch_word_no_send(s):
        m = M(); sess = Session("grok-bot", ledger(), m, Chain(), Vault(), Img())
        run(sess, *HAPPY); s.assertIn("REFUSED", sess.handle("launch")); s.assertEqual(m.sent, [])
    def test_expired_quote_requotes(s):
        now = [0]; m = M(); sess = Session("grok-bot", ledger(), m, Chain(), Vault(), Img(), clock=lambda: now[0])
        run(sess, *HAPPY); now[0] = 999; s.assertIn("re-quoted", sess.handle("LAUNCH")); s.assertEqual(m.sent, [])
    def test_real_name_blocked(s):
        sess = Session("grok-bot", ledger(), M(), Chain(), Vault(), Img())
        out = run(sess, "/mogit", "Elon Coin", "Elon", "Elon Inu"); s.assertIn("REFUSED", out)
    def test_hype_refused(s):
        sess = Session("grok-bot", ledger(), M(), Chain(), Vault(), Img())
        out = run(sess, "/mogit", "Grok Coin", "GROK", "to the moon", "100x gem", "lfg"); s.assertIn("REFUSED", out)
    def test_image_path_not_proof(s):
        m = M(); sess = Session("grok-bot", ledger(), m, Chain(), Vault(), Img())
        out = run(sess, *HAPPY[:-1], "a.png", "b.png", "c.png"); s.assertIn("REFUSED", out); s.assertEqual(m.sent, [])
    def test_bundled_dev_buy_refused(s):
        raw = bytearray(base64.b64decode(TREMP)); # change instruction count 1 -> 2 => parse/guard refuses
        k = len(parse(bytes(raw))["keys"]); off = 1 + 64 * raw[0] + 3 + 1 + 32 * k + 32
        raw[off] = 2
        with s.assertRaises(GuardError): inspect(bytes(raw))

if __name__ == "__main__": unittest.main()
