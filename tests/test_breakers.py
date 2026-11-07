"""Adversarial mock tests — try to break the 4 gates. Zero network/mainnet (gate 4).
Does not import or mutate mogit/ source beyond public APIs. Findings go in the handoff."""
import base64, json, os, socket, tempfile, unittest
from decimal import Decimal
from pathlib import Path
from mogit.guard import inspect, GuardError, parse, PINNED, CREATE_DISCRIMINATOR, PROGRAM_ID, LAUNCH_WALLET
from mogit.ledger import Ledger, LedgerError, CAP_USD
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
    def __init__(self, tx=TREMP, sim=None, ok=True, sol="0.02059164", usd="1.93", mint=None):
        self.tx = tx
        self.simr = sim if sim is not None else {"payer_lamports_delta": -20591640}
        self.ok = ok
        self.q = {"sol": sol, "usd": usd, "mint": mint or "EarneMetyCsEqac2EEiLUh3VdR6bMre6cqM9rftNc6jB",
                  "quote_mint": "So11111111111111111111111111111111111111112", "first_buy_lamports": 0}
        self.sent = []
        self.simulated = False
    def quote(self): return dict(self.q)
    def build(self, c, q): return self.tx
    def simulate(self, tx): self.simulated = True; return dict(self.simr)
    def submit(self, signed): self.sent.append(signed); return {"ok": self.ok, "sig": "MOCKSIG", "err": "x"}

class Chain:
    def __init__(self, b="1"): self.b = b
    def balance_sol(self): return self.b

class Vault:
    def sign(self, tx, th): return "SIGNED:" + th

class Img:
    def __init__(self, sha="deadbeef"): self.sha = sha
    def verify(self, p): return self.sha

def make_ledger(spent="15.91", cap="30", launches=None, raw=None):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "ledger.json")
    if raw is not None:
        Path(p).write_text(raw)
    else:
        data = {"cap_usd": cap, "spent_usd": spent, "kill": False}
        if launches is not None:
            data["launches"] = launches
        Path(p).write_text(json.dumps(data))
    return Ledger(p), p

def run(sess, *msgs):
    out = None
    for m in msgs:
        out = sess.handle(m)
    return out

HAPPY = ["/mogit", "Grok Coin", "GROK", "Grok Coin. Launched by grok-bot.", "none"]

def tremp_raw():
    return bytearray(base64.b64decode(TREMP))

def account_offset(raw, acc_idx):
    """Byte offset of instruction account pubkey #acc_idx inside the message (TREMP fixture)."""
    tx = parse(bytes(raw))
    # Find the ix account list positions by re-walking like parse
    n, i = 0, 0
    # compact-u16 nsigs
    v = s = 0
    while True:
        c = raw[i]; i += 1; v |= (c & 0x7F) << s; s += 7
        if not c & 0x80: break
    n = v
    i += 64 * n
    i += 3  # header
    # nkeys compact
    v = s = 0; start = i
    while True:
        c = raw[i]; i += 1; v |= (c & 0x7F) << s; s += 7
        if not c & 0x80: break
    nk = v
    keys_off = i
    i += 32 * nk + 32  # keys + recenthash
    # nix
    v = s = 0
    while True:
        c = raw[i]; i += 1; v |= (c & 0x7F) << s; s += 7
        if not c & 0x80: break
    i += 1  # program index
    # na
    v = s = 0
    while True:
        c = raw[i]; i += 1; v |= (c & 0x7F) << s; s += 7
        if not c & 0x80: break
    # account indices start at i; each is 1 byte into keys table — we need key bytes themselves
    # For PINNED we mutate the key table entry for that account's pubkey
    acc_key = tx["instructions"][0]["accounts"][acc_idx]
    # locate which key index that is
    key_i = tx["keys"].index(acc_key)
    return keys_off + 32 * key_i

class Breakers(unittest.TestCase):
    def setUp(self):
        socket.socket = _no_socket
        socket.getaddrinfo = _no_getaddrinfo
    def tearDown(self):
        socket.socket = _real_socket
        socket.getaddrinfo = _real_getaddrinfo

    # --- gate 4: network ban stricter ---
    def test_getaddrinfo_also_banned(self):
        with self.assertRaises(AssertionError):
            socket.getaddrinfo("example.com", 80)

    # --- gate 1: ledger hardness ---
    def test_empty_ledger_file_refuses(self):
        L, _ = make_ledger(raw="")
        with self.assertRaises(LedgerError):
            L.load()
    def test_missing_spent_key_refuses(self):
        L, _ = make_ledger(raw=json.dumps({"cap_usd": "30"}))
        with self.assertRaises(LedgerError):
            L.load()
    def test_cap_above_lifetime_refuses(self):
        L, _ = make_ledger(cap="30.01")
        with self.assertRaises(LedgerError):
            L.load()
    def test_corrupt_json_refuses(self):
        L, _ = make_ledger(raw="{not json")
        with self.assertRaises(LedgerError):
            L.load()
    def test_spent_already_over_cap_blocks_entry(self):
        L, _ = make_ledger(spent="30.01")
        sess = Session("grok-bot", L, M(), Chain(), Vault(), Img())
        self.assertIn("REFUSED", sess.handle("/mogit"))
    def test_exact_remaining_allows_check(self):
        # remaining 1.93, cost 1.93 → should pass check
        L, _ = make_ledger(spent="28.07")
        st = L.check("1.93")
        self.assertEqual(st["remaining_usd"], Decimal("1.93"))
    def test_one_cent_over_remaining_refuses(self):
        L, _ = make_ledger(spent="28.07")
        with self.assertRaises(LedgerError):
            L.check("1.94")
    def test_negative_spent_still_loads(self):
        # document behavior: negative spent enlarges remaining — potential hole if ledger is writable by attacker
        L, _ = make_ledger(spent="-5")
        st = L.load()
        self.assertEqual(st["remaining_usd"], Decimal("35"))

    # --- gate 3: pin every account + shape ---
    def test_each_pinned_account_tamper_refused(self):
        for idx in PINNED:
            raw = tremp_raw()
            off = account_offset(raw, idx)
            raw[off] ^= 0x01
            with self.assertRaises(GuardError, msg=f"account {idx} should refuse"):
                inspect(bytes(raw))
    def test_wrong_discriminator_refused(self):
        raw = tremp_raw()
        tx = parse(bytes(raw))
        # data starts after program idx + account indices compact lengths — flip first data byte via rebuild walk
        # easier: find discriminator bytes uniquely
        disc = CREATE_DISCRIMINATOR
        pos = bytes(raw).find(disc)
        self.assertGreaterEqual(pos, 0)
        raw[pos] ^= 1
        with self.assertRaises(GuardError):
            inspect(bytes(raw))
    def test_versioned_tx_refused(self):
        raw = tremp_raw()
        # after signatures, set version bit 0x80 on message header byte
        n = raw[0]  # nsigs compact single-byte for these fixtures
        i = 1 + 64 * n
        raw[i] = raw[i] | 0x80
        with self.assertRaises(GuardError):
            inspect(bytes(raw))
    def test_empty_bytes_refused(self):
        with self.assertRaises(GuardError):
            inspect(b"")
    def test_trailing_bytes_refused(self):
        with self.assertRaises(GuardError):
            inspect(base64.b64decode(TREMP) + b"\x00")
    def test_wrong_account_count_refused(self):
        # flip na from 16 to 15 by finding the compact-u16 after program index
        raw = tremp_raw()
        n = raw[0]
        i = 1 + 64 * n + 3
        # skip nkeys
        v = s = 0
        while True:
            c = raw[i]; i += 1; v |= (c & 0x7F) << s; s += 7
            if not c & 0x80: break
        i += 32 * v + 32
        # nix
        v = s = 0
        while True:
            c = raw[i]; i += 1; v |= (c & 0x7F) << s; s += 7
            if not c & 0x80: break
        i += 1  # program index
        # na is next compact — fixtures use single byte 16
        self.assertEqual(raw[i], 16)
        raw[i] = 15
        with self.assertRaises(GuardError):
            inspect(bytes(raw))
    def test_sa_fixture_still_passes(self):
        g = inspect(SA)
        self.assertEqual(g["fee_payer"], LAUNCH_WALLET)
        self.assertEqual(g["mint"], "5kDrkzSEGH8jJK2ibjFDX2QEHVuUzPNuRc7eVUpVrzSM")

    # --- gate 2 / flow: crash & race holes ---
    def test_sim_ok_missing_lamports_should_refuse_not_crash(self):  # HOLE-1 fixed: refuse, not crash
        """HOLE: sim without err but missing payer_lamports_delta → KeyError instead of REFUSED."""
        m = M(sim={})  # no err, no payer_lamports_delta
        sess = Session("grok-bot", make_ledger()[0], m, Chain(), Vault(), Img())
        try:
            out = run(sess, *HAPPY)
        except KeyError as e:
            self.fail(f"HOLE: sim missing lamports crashed with KeyError {e!r}; should REFUSE")
        self.assertIn("REFUSED", out)
        self.assertEqual(m.sent, [])

    def test_balance_drops_between_card_and_launch(self):
        bal = ["1"]
        class DropChain:
            def balance_sol(self): return bal[0]
        m = M(); L, _ = make_ledger()
        sess = Session("grok-bot", L, m, DropChain(), Vault(), Img())
        card = run(sess, *HAPPY)
        self.assertIn("terms_hash", card)
        bal[0] = "0.001"
        out = sess.handle("LAUNCH")
        self.assertIn("REFUSED", out)
        self.assertEqual(m.sent, [])
        self.assertEqual(str(L.load()["spent_usd"]), "15.91")

    def test_cap_fills_between_card_and_launch(self):
        L, path = make_ledger(spent="15.91")
        m = M()
        sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        self.assertIn("terms_hash", run(sess, *HAPPY))
        # poison remaining before LAUNCH
        Path(path).write_text(json.dumps({"cap_usd": "30", "spent_usd": "29.50"}))
        out = sess.handle("LAUNCH")
        self.assertIn("REFUSED", out)
        self.assertEqual(m.sent, [])

    def test_launch_strips_whitespace_still_requires_exact_word(self):
        # strip() is intentional: " LAUNCH\n" confirms; "LAUNCHX" / "launch" do not
        m = M(); sess = Session("grok-bot", make_ledger()[0], m, Chain(), Vault(), Img())
        run(sess, *HAPPY)
        out = sess.handle("  LAUNCH\n")
        self.assertIn("LAUNCHED", out)
        self.assertEqual(len(m.sent), 1)
        m2 = M(); sess2 = Session("grok-bot", make_ledger()[0], m2, Chain(), Vault(), Img())
        run(sess2, *HAPPY)
        self.assertIn("REFUSED", sess2.handle("LAUNCHX"))
        self.assertEqual(m2.sent, [])

    def test_cancel_mid_flow_no_send(self):
        m = M(); sess = Session("grok-bot", make_ledger()[0], m, Chain(), Vault(), Img())
        run(sess, "/mogit", "Grok Coin")
        self.assertIn("aborted", sess.handle("cancel"))
        self.assertEqual(m.sent, [])
        self.assertEqual(sess.state, "idle")

    def test_quit_after_card_no_send(self):
        m = M(); sess = Session("grok-bot", make_ledger()[0], m, Chain(), Vault(), Img())
        run(sess, *HAPPY)
        self.assertIn("aborted", sess.handle("quit"))
        self.assertEqual(m.sent, [])

    # --- content rules / false positives ---
    def test_melon_not_elon_false_positive(self):  # HOLE-2 fixed: word-boundary ban
        """HOLE: substring ban blocks 'Melon' because 'elon' in 'melon'."""
        sess = Session("grok-bot", make_ledger()[0], M(), Chain(), Vault(), Img())
        out = run(sess, "/mogit", "Melon Coin")
        # Prefer: ask for ticker. If REFUSED, document overblock.
        if "REFUSED" in out or "real person" in out.lower() or "invalid" in out.lower():
            self.fail("HOLE: Melon blocked by elon substring ban")
        self.assertIn("Ticker", out)

    def test_owner_yes_allows_banned_name(self):
        sess = Session("grok-bot", make_ledger()[0], M(), Chain(), Vault(), Img(),
                       owner_yes_names=("Elon Coin",))
        out = run(sess, "/mogit", "Elon Coin")
        self.assertIn("Ticker", out)

    def test_name_too_long_after_retries_refuses(self):
        sess = Session("grok-bot", make_ledger()[0], M(), Chain(), Vault(), Img())
        out = run(sess, "/mogit", "x" * 33, "y" * 33, "z" * 33)
        self.assertIn("REFUSED", out)

    def test_ticker_starts_with_digit_refused(self):
        sess = Session("grok-bot", make_ledger()[0], M(), Chain(), Vault(), Img())
        out = run(sess, "/mogit", "Grok Coin", "9GROK", "9GROK", "9GROK")
        self.assertIn("REFUSED", out)

    def test_description_missing_launcher_refused(self):
        sess = Session("grok-bot", make_ledger()[0], M(), Chain(), Vault(), Img())
        out = run(sess, "/mogit", "Grok Coin", "GROK", "Just a coin.", "Just a coin.", "Just a coin.")
        self.assertIn("REFUSED", out)

    def test_verified_image_sha_lands_on_card(self):
        m = M(); img = Img(sha="abc123")
        sess = Session("grok-bot", make_ledger()[0], m, Chain(), Vault(), img)
        card = run(sess, "/mogit", "Grok Coin", "GROK", "Grok Coin. Launched by grok-bot.", "/tmp/x.png")
        self.assertIn("abc123", card)
        self.assertIn("terms_hash", card)

    def test_failed_submit_does_not_append_launch_record(self):
        L, _ = make_ledger(); m = M(ok=False)
        sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        run(sess, *HAPPY, "LAUNCH")
        self.assertEqual(L.load().get("launches", []), [])

    def test_happy_path_records_launch(self):
        L, _ = make_ledger(); m = M()
        sess = Session("grok-bot", L, m, Chain(), Vault(), Img())
        out = run(sess, *HAPPY, "LAUNCH")
        self.assertIn("LAUNCHED", out)
        self.assertEqual(len(L.load()["launches"]), 1)
        self.assertEqual(L.load()["launches"][0]["mint"], "EarneMetyCsEqac2EEiLUh3VdR6bMre6cqM9rftNc6jB")

    def test_mint_swap_in_built_tx_refused(self):
        # build returns SA tx while quote mint is TREMP mint
        m = M(tx=SA, mint="EarneMetyCsEqac2EEiLUh3VdR6bMre6cqM9rftNc6jB")
        sess = Session("grok-bot", make_ledger()[0], m, Chain(), Vault(), Img())
        out = run(sess, *HAPPY)
        self.assertIn("REFUSED", out)
        self.assertIn("guard", out.lower())
        self.assertEqual(m.sent, [])

if __name__ == "__main__":
    unittest.main()
