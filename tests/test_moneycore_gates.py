"""Money-core gates (mogit/), fail-closed. Mocks only, no network.
kill switch (persisted, default ON, checked before build and before sign) · max 3 launches/day ·
per-launch hard cap 0.025 SOL on simulated spend (and on the quote) · SOL-only quote · first buy 0."""
import json, os, socket, tempfile, unittest
from pathlib import Path

from mogit import gates
from mogit.flow import Session
from mogit.ledger import Ledger
from tests.test_gates import HAPPY, M, Chain, Img, Vault, _no_net, _real, run

DAY = 86_400
NOW = 20_000 * DAY + 3_600  # 01:00 UTC on some day


def ledger(kill=False, launches=(), pending=(), extra=None):
    d = tempfile.mkdtemp(); p = os.path.join(d, "ledger.json")
    data = {"cap_usd": "30", "spent_usd": "0", "launches": list(launches), "pending": list(pending)}
    if kill is not None:
        data["kill"] = kill
    data.update(extra or {})
    Path(p).write_text(json.dumps(data)); return Ledger(p), p


def sess(L, m=None, clock=lambda: NOW):
    return Session("grok-bot", L, m or M(), Chain(), Vault(), Img(), clock=clock)


class Gates(unittest.TestCase):
    def setUp(s): socket.socket = _no_net
    def tearDown(s): socket.socket = _real

    # ---- constants pinned
    def test_constants(s):
        s.assertEqual(gates.PER_LAUNCH_CAP_LAMPORTS, 25_000_000)
        s.assertEqual(gates.MAX_LAUNCHES_PER_DAY, 3)
        s.assertEqual(gates.SOL_MINT, "So11111111111111111111111111111111111111112")

    # ---- happy path still launches when every gate passes
    def test_all_gates_pass_launches(s):
        L, _ = ledger(); m = M()
        out = run(sess(L, m), *HAPPY, "LAUNCH")
        s.assertTrue(out.startswith("LAUNCHED"), out); s.assertEqual(len(m.sent), 1)
        s.assertEqual(len(L.load()["launches"]), 1)

    # ---- kill switch
    def test_kill_missing_key_is_on(s):
        L, _ = ledger(kill=None); m = M()
        s.assertIn("kill switch", sess(L, m).handle("/mogit")); s.assertEqual(m.sent, [])

    def test_kill_non_bool_is_on(s):
        for v in ("false", 0, None, "off"):
            L, _ = ledger(extra={"kill": v})
            s.assertTrue(L.kill_on(), v)

    def test_kill_on_refuses_before_build(s):
        L, _ = ledger(kill=True); m = M()
        m.build = lambda c, q: (_ for _ in ()).throw(AssertionError("built while killed"))
        s.assertIn("kill switch", sess(L, m).handle("/mogit")); s.assertEqual(m.sent, [])

    def test_kill_flipped_on_after_card_refuses_before_sign(s):
        L, _ = ledger(); m = M(); signed = []
        se = sess(L, m); se.vault.sign = lambda tx, th: signed.append(tx) or "SIGNED"
        s.assertIn("CONFIRM CARD", run(se, *HAPPY))
        L.set_kill(True)
        out = se.handle("LAUNCH")
        s.assertIn("REFUSED", out); s.assertIn("kill switch", out)
        s.assertEqual(signed, []); s.assertEqual(m.sent, [])

    def test_boot_sets_kill_on_and_persists(s):
        L, p = ledger(kill=False)
        L.boot()
        s.assertIs(json.loads(Path(p).read_text())["kill"], True)
        s.assertTrue(Ledger(p).kill_on())
        L.set_kill(False); s.assertFalse(Ledger(p).kill_on())

    # ---- 3 launches per UTC day (settled + pending both count)
    def test_daily_limit_counts_settled_and_pending(s):
        today = [{"mint": "a", "ts": NOW - 60}, {"mint": "b", "ts": NOW - 120}]
        L, _ = ledger(launches=today, pending=[{"id": "x", "cost_usd": "1", "status": "pending", "ts": NOW - 5}])
        m = M(); s.assertIn("3 launches", sess(L, m).handle("/mogit")); s.assertEqual(m.sent, [])

    def test_yesterday_does_not_count(s):
        old = [{"mint": str(i), "ts": NOW - DAY} for i in range(5)]
        L, _ = ledger(launches=old)
        s.assertIn("Name?", sess(L).handle("/mogit"))

    def test_entry_without_timestamp_counts_as_today(s):
        L, _ = ledger(launches=[{"mint": "a"}, {"mint": "b"}, {"mint": "c"}])
        s.assertIn("3 launches", sess(L).handle("/mogit"))

    def test_third_launch_ok_fourth_refused_same_day(s):
        L, _ = ledger(launches=[{"mint": "a", "ts": NOW}, {"mint": "b", "ts": NOW}])
        s.assertTrue(run(sess(L, M()), *HAPPY, "LAUNCH").startswith("LAUNCHED"))
        s.assertIn("3 launches", sess(L, M()).handle("/mogit"))

    def test_reservation_is_timestamped(s):
        L, _ = ledger()
        rid = L.reserve("1", {"mint": "z", "ts": NOW})
        s.assertEqual([p["ts"] for p in L.load()["pending"] if p["id"] == rid], [NOW])

    # ---- per-launch hard cap 0.025 SOL
    def test_simulated_spend_over_cap_refused(s):
        m = M(sim={"payer_lamports_delta": -25_000_001}, sol="0.0249")
        out = run(sess(ledger()[0], m), *HAPPY)
        s.assertIn("0.025 SOL", out); s.assertEqual(m.sent, [])

    def test_fractional_over_cap_simulation_delta_is_not_truncated(s):
        m = M(sim={"payer_lamports_delta": -25_000_000.1}, sol="0.025")
        out = run(sess(ledger()[0], m), *HAPPY)
        s.assertIn("whole number of lamports", out); s.assertEqual(m.sent, [])

    def test_malformed_simulation_delta_refused_before_confirm(s):
        m = M(sim={"payer_lamports_delta": "not-lamports"})
        out = run(sess(ledger()[0], m), *HAPPY)
        s.assertIn("simulation delta is invalid", out); s.assertEqual(m.sent, [])

    def test_simulated_spend_at_cap_allowed(s):
        m = M(sim={"payer_lamports_delta": -25_000_000}, sol="0.025")
        s.assertIn("CONFIRM CARD", run(sess(ledger()[0], m), *HAPPY))

    def test_quote_over_cap_refused_before_build(s):
        m = M(sol="0.0251")
        s.assertIn("0.025 SOL", sess(ledger()[0], m).handle("/mogit"))

    def test_negative_quote_refused_before_build(s):
        m = M(sol="-0.001")
        out = sess(ledger()[0], m).handle("/mogit")
        s.assertIn("REFUSED", out); s.assertIn("non-negative", out)
        s.assertFalse(hasattr(m, "simulated")); s.assertEqual(m.sent, [])

    def test_malformed_quote_refused_before_build(s):
        m = M(sol="not-a-number")
        out = sess(ledger()[0], m).handle("/mogit")
        s.assertIn("REFUSED", out); s.assertIn("invalid", out)
        s.assertFalse(hasattr(m, "simulated")); s.assertEqual(m.sent, [])

    # ---- SOL-only quote, first buy 0
    def test_non_sol_quote_refused(s):
        for bad in ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", None, ""):
            m = M(); m.q["quote_mint"] = bad
            s.assertIn("SOL pairs only", sess(ledger()[0], m).handle("/mogit"), bad)

    def test_missing_quote_mint_refused(s):
        m = M(); del m.q["quote_mint"]
        s.assertIn("SOL pairs only", sess(ledger()[0], m).handle("/mogit"))

    def test_first_buy_nonzero_or_missing_refused(s):
        for bad in (1, "0", 0.0, None, True):
            m = M(); m.q["first_buy_lamports"] = bad
            s.assertIn("first buy", sess(ledger()[0], m).handle("/mogit"), bad)
        m = M(); del m.q["first_buy_lamports"]
        s.assertIn("first buy", sess(ledger()[0], m).handle("/mogit"))

    def test_full_pinned_card_cost_checked_before_sign(s):
        # Quote is $1.93 and fits the $1.932 remaining balance. Simulated fees
        # raise the pinned card cost slightly above that remainder.
        L, _ = ledger(kill=False, extra={"spent_usd": "28.068"})
        m = M(sim={"payer_lamports_delta": -20_641_640})
        se = sess(L, m); signed = []
        se.vault.sign = lambda tx, th: signed.append(tx) or "SIGNED"
        card = run(se, *HAPPY)
        s.assertIn("CONFIRM CARD", card)
        s.assertIn("cost.quoted_usd: 1.93", card)
        s.assertIn("budget.remaining_usd: -0.002", card)
        out = se.handle("LAUNCH")
        s.assertIn("REFUSED", out); s.assertIn("budget cap hit", out)
        s.assertEqual(signed, []); s.assertEqual(m.sent, [])

    def test_quote_gates_rechecked_before_sign(s):
        L, _ = ledger(); m = M(); se = sess(L, m)
        s.assertIn("CONFIRM CARD", run(se, *HAPPY))
        m.q["first_buy_lamports"] = 5
        s.assertIn("first buy", se.handle("LAUNCH")); s.assertEqual(m.sent, [])


if __name__ == "__main__":
    unittest.main()
