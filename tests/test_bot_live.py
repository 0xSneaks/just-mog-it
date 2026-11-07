"""Live-adapter tests against a fake HTTP transport (no network) + pump guard on synthetic txs."""
import base64
import json
import os
import socket
import unittest

from mogit.guard import GuardError, b58
from bot.live.http import Http
from bot.live.payments import Payments
from bot.live.pumpportal import COLLECT_CREATOR_FEE, PumpPortal, inspect_claim
from bot.live.solana import Rpc, find_pda, on_curve
from bot.live.vault import Vault
from bot.live.x import XClient
from bot.parse import b58decode
from bot import pumpguard as G

socket.socket = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network forbidden in tests"))

WALLET = "2LYQUArYnaYR2YTePK99mUiYU5mFXn1qZqe5b3xGsvHt"
MINT = "So11111111111111111111111111111111111111112"
OTHER = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

def cu16(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n: return out

def bstr(s): return len(s.encode()).to_bytes(4, "little") + s.encode()

def create_data(name="Bingus", sym="BINGUS", uri="https://ipfs.io/ipfs/Qm1", creator=WALLET):
    return G.CREATE + bstr(name) + bstr(sym) + bstr(uri) + b58decode(creator)

def build_tx(ixs, keys, nreq=2, v0=False, alts=0, nsig=None):
    """ixs: [(program, [account keys], data)]; keys: ordered account keys."""
    idx = {k: i for i, k in enumerate(keys)}
    msg = (b"\x80" if v0 else b"") + bytes([nreq, 0, 0]) + cu16(len(keys)) + b"".join(b58decode(k) for k in keys)
    msg += b"\x11" * 32 + cu16(len(ixs))
    for p, acc, data in ixs:
        msg += bytes([idx[p]]) + cu16(len(acc)) + bytes(idx[a] for a in acc) + cu16(len(data)) + data
    if v0:
        msg += cu16(alts) + (b"\x00" * 34 if alts else b"")
    n = nsig if nsig is not None else nreq
    return cu16(n) + b"\x00" * 64 * n + msg

KEYS = [WALLET, MINT, G.PUMP, G.COMPUTE_BUDGET]

def good(**kw):
    ixs = [(G.COMPUTE_BUDGET, [], b"\x02" + (200_000).to_bytes(4, "little")),
           (G.COMPUTE_BUDGET, [], b"\x03" + (1_000_000).to_bytes(8, "little")),
           (G.PUMP, [MINT, WALLET], create_data(**kw))]
    return ixs

def chk(raw, **kw):
    a = dict(bot_wallet=WALLET, expected_mint=MINT, expected_name="Bingus", expected_symbol="BINGUS")
    a.update(kw)
    return G.inspect(raw, **a)

class TestPumpGuard(unittest.TestCase):
    def test_accepts_clean_create_legacy_and_v0(self):
        for v0 in (False, True):
            out = chk(build_tx(good(), KEYS, v0=v0))
            self.assertEqual((out["name"], out["creator"], out["priority_lamports"]), ("Bingus", WALLET, 200_000))

    def test_refusals(self):
        cases = {
            "creator": build_tx(good(creator=OTHER), KEYS + [OTHER]),
            "name": build_tx(good(name="Bingu5"), KEYS),
            "uri": build_tx(good(uri="https://evil.example/m.json"), KEYS),
            "http": build_tx(good(uri="http://ipfs.io/ipfs/x"), KEYS),
            "alt": build_tx(good(), KEYS, v0=True, alts=1),
            "v2": build_tx([(G.PUMP, [MINT, WALLET], G.CREATE_V2 + b"x")], KEYS),
            "extra_prog": build_tx(good() + [("11111111111111111111111111111111", [WALLET], b"\x02")],
                                   KEYS + ["11111111111111111111111111111111"]),
            "two_creates": build_tx(good() + good()[2:], KEYS),
            "buy_with_sol": build_tx(good() + [(G.PUMP, [MINT], G.BUY + (1).to_bytes(8, "little") +
                                                (5_000_000).to_bytes(8, "little"))], KEYS),
            "third_signer": build_tx(good(), [WALLET, MINT, OTHER, G.PUMP, G.COMPUTE_BUDGET], nreq=3),
            "sig_count": build_tx(good(), KEYS, nsig=1),
            "fee_payer": build_tx(good(), [MINT, WALLET, G.PUMP, G.COMPUTE_BUDGET]),
            "truncated": build_tx(good(), KEYS)[:-10],
        }
        for name, raw in cases.items():
            with self.assertRaises(GuardError, msg=name):
                chk(raw)
        with self.assertRaises(GuardError):
            chk(build_tx(good(), KEYS), priority_cap_lamports=100_000)
        with self.assertRaises(GuardError):
            chk(build_tx(good(), KEYS), dev_buy_lamports=1)

    def test_zero_sol_buy_allowed(self):
        raw = build_tx(good() + [(G.PUMP, [MINT], G.BUY + (0).to_bytes(8, "little") * 2)], KEYS)
        chk(raw)

    def test_claim_guard(self):
        ok = build_tx([(G.PUMP, [WALLET], COLLECT_CREATOR_FEE)], [WALLET, G.PUMP], nreq=1)
        self.assertTrue(inspect_claim(ok, WALLET))
        bad = build_tx([(G.PUMP, [WALLET], COLLECT_CREATOR_FEE), (G.PUMP, [MINT, WALLET], create_data())],
                       [WALLET, MINT, G.PUMP], nreq=1)
        with self.assertRaises(GuardError):
            inspect_claim(bad, WALLET)

class TestPDA(unittest.TestCase):
    def test_matches_real_event_authorities(self):
        # VERIFIED: 8Ks12p... is account 14 in the real Meteora DBC launch fixtures (mogit/guard.py PINNED)
        self.assertEqual(find_pda([b"__event_authority"], "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN")[0],
                         "8Ks12pbrD6PXxfty1hVQiE9sc289zgU1zHkvXhrSdriF")
        self.assertEqual(find_pda([b"__event_authority"], G.PUMP)[0], "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
        self.assertTrue(on_curve(b58decode(WALLET)))

class FakeHttp:
    """Routes (method, url-prefix) -> handler(body) returning (status, bytes|obj)."""
    def __init__(self, routes): self.routes, self.calls = routes, []
    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        for (m, prefix), fn in self.routes.items():
            if m == method and url.startswith(prefix):
                st, out = fn(body, url)
                return st, out if isinstance(out, bytes) else json.dumps(out).encode()
        return 404, b"no route"

class TestX(unittest.TestCase):
    def test_mentions_reply_upload(self):
        mentions = {"data": [{"id": "3", "author_id": "9", "text": "@justmogit_bot mog me"},
                             {"id": "2", "author_id": "8", "text": "just mog it", "referenced_tweets": [{"type": "quoted", "id": "1"}]},
                             {"id": "0", "author_id": "BOT", "text": "self"}],
                    "includes": {"users": [{"id": "8", "username": "alice"}], "tweets": [{"id": "1", "text": f"CA {MINT}"}]},
                    "meta": {"newest_id": "3"}}
        t = FakeHttp({("GET", "https://api.x.com/2/users/BOT/mentions"): lambda b, u: (200, mentions),
                      ("POST", "https://api.x.com/2/tweets"): lambda b, u: (201, {"data": {"id": "r1"}}),
                      ("POST", "https://api.x.com/2/media/upload"): lambda b, u: (200, {"data": {"id": "m9"}}),
                      ("POST", "https://api.x.com/2/media/metadata"): lambda b, u: (200, {})})
        x = XClient(Http(t), token="T", bot_user_id="BOT")
        ms, newest = x.mentions()
        self.assertEqual([m["id"] for m in ms], ["2", "3"])
        self.assertEqual((ms[0]["handle"], ms[0]["quoted_text"]), ("alice", f"CA {MINT}"))
        self.assertEqual(newest, "3")
        self.assertEqual(x.reply("2", "hi", media_ids=["m9"]), "r1")
        body = json.loads(t.calls[-1][3])
        self.assertEqual(body, {"text": "hi", "reply": {"in_reply_to_tweet_id": "2"}, "media": {"media_ids": ["m9"]}})
        self.assertEqual(x.upload_media(b"\x89PNG" + b"0" * 10, alt_text="a"), "m9")
        cmds = [c[3] for c in t.calls if c[1].endswith("/media/upload")]
        self.assertTrue(b"INIT" in cmds[0] and b"APPEND" in cmds[1] and b"FINALIZE" in cmds[2])
        self.assertTrue(all(c[2]["Authorization"] == "Bearer T" for c in t.calls))

class TestRpcPaymentsVault(unittest.TestCase):
    def _rpc(self, results):
        def handler(body, url):
            req = json.loads(body); r = results[req["method"]]
            return 200, {"jsonrpc": "2.0", "id": 1, "result": r(req["params"]) if callable(r) else r}
        return Rpc(Http(FakeHttp({("POST", "https://rpc"): handler})), urls=["https://rpc"], sleep=lambda s: None,
                   clock=iter(range(0, 1000)).__next__)

    def test_simulate_delta_and_status(self):
        raw = build_tx(good(), KEYS)
        rpc = self._rpc({"getBalance": {"value": 1_000_000_000},
                         "simulateTransaction": {"value": {"err": None, "accounts": [{"lamports": 979_700_000}]}},
                         "getSignatureStatuses": {"value": [{"err": None, "confirmationStatus": "confirmed"}]}})
        self.assertEqual(rpc.simulate(raw)["payer_lamports_delta"], 20_300_000)
        self.assertEqual(rpc.status("S", timeout_s=5), "confirmed")
        rpc2 = self._rpc({"getSignatureStatuses": {"value": [None]}, "isBlockhashValid": {"value": False}})
        self.assertIsNone(rpc2.status("S", timeout_s=3))
        self.assertFalse(rpc2.blockhash_expired("unknown-sig"))      # unknown blockhash => wait, never rebuild

    def test_payment_verifier(self):
        job, addr = "job123", "FeeAddr1111111111111111111111111111111111"
        tx = {"meta": {"err": None}, "transaction": {"message": {"instructions": [
            {"program": "system", "parsed": {"type": "transfer", "info": {"source": "Payer", "destination": addr,
                                                                        "lamports": 20_000_000}}},
            {"programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "parsed": job}]}}}
        rpc = self._rpc({"getSignaturesForAddress": [{"signature": "PAY1", "err": None, "memo": f"[6] {job}"}],
                         "getTransaction": tx})
        self.assertEqual(Payments(rpc, addr).verify(job, 20_000_000)["payer"], "Payer")
        self.assertIsNone(Payments(rpc, addr).verify(job, 30_000_000))           # underpaid
        self.assertIsNone(Payments(rpc, addr, used=lambda s: True).verify(job, 20_000_000))   # replay
        self.assertIsNone(Payments(rpc, addr).verify("otherjob", 20_000_000))

    def test_vault_signature_of(self):
        self.assertIsNone(Vault.signature_of(b"\x01" + b"\x00" * 64 + b"msg"))
        sig = b"\x07" * 64
        self.assertEqual(Vault.signature_of(b"\x01" + sig + b"msg"), b58(sig))

class TestPumpPortal(unittest.TestCase):
    def test_create_request_has_zero_dev_buy_and_pinned_uri(self):
        pin = iter(["IMG", "META"])
        t = FakeHttp({("POST", "https://uploads.pinata.cloud"): lambda b, u: (200, {"data": {"cid": next(pin)}}),
                      ("POST", "https://pumpportal.fun/api/trade-local"): lambda b, u: (200, b"TXBYTES")})
        pp = PumpPortal(WALLET, rpc=None, http=Http(t), pinata_jwt="J")
        out = pp.build_create(name="Bingus", symbol="BINGUS", description="d", image_url="x", socials={},
                              mint=MINT, dev_buy_sol="0", priority_cap_sol="0.005", image_fetch=lambda u: b"img")
        self.assertEqual(out, b"TXBYTES")
        body = json.loads(t.calls[-1][3])
        self.assertEqual((body["action"], body["amount"], body["pool"], body["mint"]), ("create", 0, "pump", MINT))
        self.assertEqual(body["tokenMetadata"], {"name": "Bingus", "symbol": "BINGUS", "uri": "https://ipfs.io/ipfs/META"})
        with self.assertRaises(ValueError):
            pp.build_create(name="a", symbol="A", description="", image_url="x", socials={}, mint=MINT,
                            dev_buy_sol="0.1", priority_cap_sol="0.005")

if __name__ == "__main__":
    unittest.main()


class TestLaunchMetadataReader(unittest.TestCase):
    """scripts/read_launch_metadata.py decoder (offline; synthetic account bytes)."""
    def test_decode_and_storage(self):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location(
            "rlm", os.path.join(os.path.dirname(__file__), "..", "scripts", "read_launch_metadata.py"))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        s = lambda t, pad=0: len(t.encode() + b"\0" * pad).to_bytes(4, "little") + t.encode() + b"\0" * pad
        ua, mint = bytes([7]) * 32, bytes([9]) * 32
        uri = "https://gateway.pinata.cloud/ipfs/QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG"
        data = (b"\x04" + ua + mint + s("Jose", 4) + s("JOSE") + s(uri) + (0).to_bytes(2, "little")
                + b"\x01" + (1).to_bytes(4, "little") + bytes(32) + b"\x01\x64" + b"\x00" + b"\x01" + b"\xff" * 8)
        d = m.decode_metadata(data)
        self.assertEqual((d["name"], d["symbol"], d["uri"], d["isMutable"]), ("Jose", "JOSE", uri, True))
        self.assertEqual(d["updateAuthority"], b58(ua))
        self.assertTrue(m.uri_storage(uri).startswith("content-addressed"))
        self.assertTrue(m.uri_storage("ar://abc").startswith("content-addressed"))
        self.assertTrue(m.uri_storage("https://mogz.fun/meta/x.json").startswith("mutable host"))
        self.assertTrue(m.metadata_pda("3cHj1iCgZb18Q8ZrqbsAtwNGjn6gifQRqnCedHhdmogz"))
