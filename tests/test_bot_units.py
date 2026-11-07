"""Unit tests for the X-bot surface pure logic. No network: socket is patched to raise."""
import io
import socket
import unittest
from decimal import Decimal

from PIL import Image

from bot import fees, metadata, mogme, parse, replies, rug, screen, spelling
from bot.policy import PolicyError, load

socket.socket = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network forbidden in tests"))
CA = "So11111111111111111111111111111111111111112"
CA2 = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

class TestPolicy(unittest.TestCase):
    def test_loads_owner_defaults(self):
        p = load()
        self.assertEqual(p.launches_per_day, 3)
        self.assertEqual(p.d("cost_cap_sol"), Decimal("0.025"))
        self.assertEqual(p.fee_split, {"mogger": 25, "holders": 25, "bankroll": 50})
        self.assertFalse(p.mog_me_enabled)
        self.assertTrue(p.kill_switch_on_at_boot)

    def test_refuses_bad_values(self):
        for o in ({"fee_split": {"mogger": 50, "holders": 25, "bankroll": 50}}, {"dev_buy_sol": "0.1"},
                  {"venue": "mogz.fun"}, {"pair_quote": "USDC"}, {"trigger_policy": "yolo"}):
            with self.assertRaises(PolicyError, msg=o):
                load(overrides=o)

class TestParse(unittest.TestCase):
    def test_mog_me_variants(self):
        for t in ("@justmogit_bot mog me", "@justmogit_bot MOG ME!", "@justmogit_bot mogme",
                  "@justmogit_bot pls mog me", "@justmogit_bot mog me bro"):
            self.assertEqual(parse.parse(t).kind, "mog_me", t)

    def test_coin_wins_over_mog_me(self):
        self.assertNotEqual(parse.parse("@justmogit_bot mog me $PEPE").kind, "mog_me")
        self.assertNotEqual(parse.parse(f"@justmogit_bot mog me {CA}").kind, "mog_me")

    def test_mog_coin(self):
        t = parse.parse(f"@justmogit_bot just mog it {CA} $WIF")
        self.assertEqual((t.kind, t.cas, t.tickers), ("mog_coin", [CA], ["WIF"]))
        t = parse.parse(f"just mog it https://pump.fun/coin/{CA2}")
        self.assertEqual((t.kind, t.cas), ("mog_coin", [CA2]))
        self.assertEqual(parse.parse("@justmogit_bot just mog it", quoted_tweet_id="9").kind, "mog_coin")

    def test_mog_it_without_target_is_none(self):
        self.assertEqual(parse.parse("@justmogit_bot just mog it").kind, "none")

    def test_bad_base58_not_ca(self):
        self.assertFalse(parse.is_pubkey("0OIl" * 9))
        self.assertTrue(parse.is_pubkey(CA))

    def test_yes_stop(self):
        self.assertEqual(parse.parse("@justmogit_bot YES").kind, "yes")
        self.assertEqual(parse.parse("@justmogit_bot stop").kind, "stop")

class TestScreen(unittest.TestCase):
    def test_hits(self):
        for t, c in (("RIP legend coin", "death"), ("school shooting coin", "violence"),
                     ("TRUMP 2028", "politics"), ("baby doge for kids", "kids"),
                     ("free sol airdrop claim now", "scam"), ("my ex coin", "private_person")):
            self.assertEqual(screen.screen(t), (False, c), t)

    def test_no_substring_false_positives(self):
        for t in ("Melon", "personality", "Bingus", "Season", "dogwifhat"):
            self.assertEqual(screen.screen(t), (True, None), t)

    def test_image_inspection(self):
        self.assertEqual(screen.screen("cat", inspection={"is_real_person": True})[1], "private_person")
        self.assertEqual(screen.screen("cat", inspection={"nsfw_score": 0.5})[1], "nsfw")
        self.assertTrue(screen.screen("cat", inspection={"nsfw_score": 0.1})[0])

class TestSpelling(unittest.TestCase):
    def test_normalize_and_fire(self):
        L = spelling.lock("Bin​gus ", "BINGUS", confirmed_by="onchain", source_name="Bingus",
                          source_ticker="BINGUS")
        self.assertEqual((L.name, L.ticker, L.fireable), ("Bingus", "BINGUS", True))

    def test_homoglyph_blocked(self):
        L = spelling.lock("Pаpa", "PAPA", confirmed_by="onchain")   # Cyrillic 'а'
        self.assertFalse(L.fireable)
        self.assertIn("mixed_scripts_name", L.errors)

    def test_diff_and_unconfirmed_block(self):
        self.assertFalse(spelling.lock("Bingus", "BING", confirmed_by="onchain", source_ticker="BINGUS").fireable)
        self.assertFalse(spelling.lock("Bingus", "BINGUS").fireable)
        self.assertFalse(spelling.lock("x" * 33, "X", confirmed_by="onchain").fireable)
        self.assertFalse(spelling.lock("A", "TOOLONGTICKER", confirmed_by="onchain").fireable)

class TestRug(unittest.TestCase):
    p = load()

    def test_strict_needs_drop_plus_signal(self):
        S = rug.CoinSnapshot
        self.assertFalse(rug.evaluate(S(quote_symbol="SOL", price_drop_pct_window=90), self.p)[0])
        ok, why = rug.evaluate(S(quote_symbol="SOL", dev_sold_pct_of_holdings=80, price_drop_pct_window=75), self.p)
        self.assertTrue(ok); self.assertEqual(why, ["dev_dumped", "price_crashed"])

    def test_lp_ignored_on_bonding_curve(self):
        s = rug.CoinSnapshot(quote_symbol="SOL", venue="pump.fun", graduated=False, lp_pulled=True,
                             price_drop_pct_window=90)
        self.assertFalse(rug.evaluate(s, self.p)[0])

    def test_any_rule(self):
        p = load(overrides={"rug": {**self.p.rug, "rule": "any"}})
        self.assertTrue(rug.evaluate(rug.CoinSnapshot(quote_symbol="SOL", price_drop_pct_window=90), p)[0])

    def test_wrong_pair(self):
        ok, why = rug.evaluate(rug.CoinSnapshot(quote_symbol="FARTCOIN"), self.p)
        self.assertEqual((ok, why), (True, ["non_sol_pair"]))
        self.assertFalse(rug.evaluate(rug.CoinSnapshot(quote_symbol="SOL", pair_liquidity_sol=None), self.p)[0])

class TestMetadata(unittest.TestCase):
    p = load()

    def test_prefix_and_truncation(self):
        d = metadata.build_description(self.p, "BINGUS", CA, "x" * 5000)
        self.assertTrue(d.startswith(f"$BINGUS was live on the wrong pair — not SOL. JustMogIt pulled the token's own picture and metadata and relaunched it paired with SOL. That's a mog. Original CA: {CA}."))
        self.assertLessEqual(len(d), self.p.description_max_chars)

    def test_strips_impersonation_words(self):
        self.assertNotIn("official", metadata.build_description(self.p, "A", CA, "The official coin").lower())

    def test_resolve_onchain_never_copies_creator(self):
        coin = {"mint": CA, "name": "Bingus", "symbol": "BINGUS", "description": "cat",
                "image_uri": "https://ipfs.io/ipfs/x", "creator": "DEVWALLET", "twitter": "https://x.com/b",
                "telegram": "javascript:alert(1)", "website": "http://insecure.example"}
        d = metadata.resolve(self.p, ca=CA, das_asset={"mint": CA, "name": "Bingus", "symbol": "BINGUS"},
                             pump_coin=coin)
        self.assertEqual((d.confirmed_by, d.confidence), ("onchain", 1.0))
        self.assertEqual(d.socials, {"twitter": "https://x.com/b"})
        self.assertNotIn("DEVWALLET", str(d.__dict__))
        self.assertTrue(d.spelling.fireable)

    def test_ocr_needs_confirmation_and_low_conf_rejects(self):
        d = metadata.resolve(self.p, ca=CA, ocr={"name": "Bingus", "symbol": "BINGUS", "confidence": 0.93})
        self.assertFalse(d.spelling.fireable)
        with self.assertRaises(metadata.DossierError):
            metadata.resolve(self.p, ca=CA, ocr={"name": "B", "symbol": "B", "confidence": 0.5})

class TestFees(unittest.TestCase):
    p = load()

    def test_split_is_exact(self):
        for amt in (0, 1, 7, 99, 10_000_003):
            s = fees.split(self.p, amt)
            self.assertEqual(sum(s.values()), amt)
            self.assertGreaterEqual(s["bankroll"], s["mogger"])
        self.assertEqual(fees.split(self.p, 1000), {"mogger": 250, "holders": 250, "bankroll": 500})

    def test_tiers_and_distribution(self):
        self.assertEqual(fees.multiplier(self.p, "0.2"), Decimal("1.0"))
        self.assertEqual(fees.multiplier(self.p, "1.2"), Decimal("1.5"))
        out = fees.distribute_holders(self.p, 1_000_000, {"a": "1.0", "b": "0.2", "bot": "5"}, exclude={"bot"})
        self.assertNotIn("bot", out)
        self.assertGreater(out["a"], 5 * out["b"])            # 1.0*1.5 vs 0.2*1.0 -> 7.5x
        self.assertLessEqual(sum(out.values()), 1_000_000)

    def test_claim_threshold(self):
        self.assertFalse(fees.should_claim(self.p, 5_200_000, 5_000))
        self.assertTrue(fees.should_claim(self.p, 20_000_000, 5_000))
        self.assertFalse(fees.should_claim(self.p, 20_000_000, 2_000_000))   # 20x tx cost rule

class TestReplies(unittest.TestCase):
    def test_launch_reply_plain_text(self):
        d = type("D", (), {"ticker": "BINGUS"})
        t = replies.launched(d, CA, "pump.fun")
        self.assertIn(CA, t); self.assertNotIn("pump.fun", t); self.assertNotIn("http", t)

    def test_launch_reply_venue_from_record(self):
        d = type("D", (), {"ticker": "JOSE"})
        self.assertIn("on pumpfun.", replies.launched(d, CA, "pump.fun"))
        m = replies.launched(d, CA, "mogz.fun")
        self.assertIn("on mogz.", m); self.assertNotIn("pumpfun", m); self.assertNotIn("mogz.fun", m)
        for bad in (None, "", "float", "raydium"):
            with self.assertRaises(replies.ReplyError):
                replies.launched(d, CA, bad)

    def test_url_refused(self):
        with self.assertRaises(replies.ReplyError):
            replies._check("live on pump.fun")
        for code in replies.REJECT:
            replies.rejection(code, mint=CA, cap=3)                 # none may contain a URL

class TestMogMe(unittest.TestCase):
    def _pfp(self, size=(400, 300)):
        b = io.BytesIO(); Image.new("RGB", size, (200, 180, 160)).save(b, "PNG"); return b.getvalue()

    def test_square_png_only_glasses_region_changes(self):
        out = Image.open(io.BytesIO(mogme.render(self._pfp(), ((170, 140), (230, 140))))).convert("RGB")
        self.assertEqual(out.size, (1000, 1000))
        self.assertEqual(out.getpixel((500, 950)), (200, 180, 160))   # chin untouched
        self.assertNotEqual(out.getpixel((500, 470)), (200, 180, 160))

    def test_edge_eyes_clip_and_bad_eyes_refused(self):
        mogme.render(self._pfp((300, 300)), ((2, 5), (60, 8)))
        with self.assertRaises(ValueError):
            mogme.render(self._pfp(), ((150, 150), (151, 150)))

if __name__ == "__main__":
    unittest.main()
