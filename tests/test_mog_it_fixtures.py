"""Offline pair previews through Engine.ingest and independent receipt fixtures."""
import base64
import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from bot.adapters import Adapters
from bot.engine import BootRefused, Engine
from bot.live.pairs import (
    CURVE, FINALIZED, POOL, PUMP_SWAP, SOL_SENTINEL, SPL_TOKEN, TOKEN_2022,
    WRAPPED_SOL, PairDataError, PumpPairs, QuoteMint, QuoteRegistry, normalize_sol,
)
from bot.live.solana import find_pda
from bot.pair_review import (
    EvaluatorResult, LiquidityMeasurement, PairContext, PairPreview, QuoteCandidate,
    measure_curve, validate_decision,
)
from bot.parse import b58decode, parse
from bot.policy import load
from bot.pumpguard import CREATE_V2, PUMP
from bot.store import Store
from mogit.guard import b58


def synthetic(label):
    return b58(hashlib.sha256(("justmogit fixture " + label).encode()).digest())


ORIGINAL = synthetic("original")
NEW = synthetic("new mint")
OTHER = synthetic("other mint")
ROGUE = synthetic("not registered")
NVDA_X = synthetic("xstocks nvda")
NVDA_B = synthetic("backpack nvda")
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SIGNATURE = b58(hashlib.sha512(b"fixture transaction signature").digest())


def account(owner, data):
    return {"owner": owner, "executable": False, "data": [base64.b64encode(data).decode(), "base64"]}


def curve_address(mint):
    return find_pda([b"bonding-curve", b58decode(mint)], PUMP)[0]


def mint_account(program=SPL_TOKEN):
    data = bytearray(82)
    data[44:46] = b"\x06\x01"
    return account(program, data)


def curve_accounts(mint, quote, *, complete=False, program=SPL_TOKEN):
    data = bytearray(115)
    data[:8] = CURVE
    data[32:40] = (80_000_000_000).to_bytes(8, "little")
    data[48] = int(complete)
    data[83:115] = b58decode(quote)
    return {mint: mint_account(program), curve_address(mint): account(PUMP, data)}


def pool_account(mint, quote, *, actual_base=None, actual_quote=None):
    base = b58decode(mint)
    authority = find_pda([b"pool-authority", base], PUMP)[0]
    address, bump = find_pda(
        [b"pool", bytes(2), b58decode(authority), base, b58decode(quote)], PUMP_SWAP,
    )
    data = bytearray(245)
    data[:8] = POOL
    data[8] = bump
    data[11:43] = b58decode(authority)
    data[43:75] = b58decode(actual_base or mint)
    data[75:107] = b58decode(actual_quote or quote)
    return address, account(PUMP_SWAP, data)


def rpc_for(accounts, *, status="finalized", tx=None):
    def call(method, params):
        if method == "getAccountInfo":
            assert params[1] == FINALIZED
            return {"value": accounts.get(params[0])}
        if method == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": status, "err": None}]}
        if method == "getTransaction":
            assert params[1]["commitment"] == "finalized"
            return tx
        raise AssertionError(method)
    return Mock(call=Mock(side_effect=call))


@pytest.fixture
def registry():
    return (QuoteMint(WRAPPED_SOL, "sol", 1), QuoteMint(USDC, "global", 1),
            QuoteMint(NVDA_X, "quoteControl", 1), QuoteMint(NVDA_B, "quoteControl", 1))


def test_registry_decoder_matches_real_sdk_capture():
    fixture = json.loads((Path(__file__).parent / "fixtures/pump_quote_registry.json").read_text())
    rpc = Mock()
    rpc.call.return_value = fixture["accounts"]
    rows = QuoteRegistry(rpc).refresh()
    assert [[r.mint, r.source, str(r.initial_virtual_quote_reserves)] for r in rows] == fixture["expected"]
    assert WRAPPED_SOL in {r.mint for r in rows}
    assert SOL_SENTINEL not in {r.mint for r in rows}
    rpc.call.assert_called_once()
    method, (addresses, opts) = rpc.call.call_args.args
    assert method == "getMultipleAccounts" and len(addresses) == 2 and opts == FINALIZED
    assert addresses[1] == "6z6GDdfb2AjR9ZhJmAUQ5cipJCVxQvLJhB2H8mCwTFBP"


def test_bad_registry_program_is_rejected():
    rpc = Mock()
    rpc.call.return_value = {"value": [mint_account(), None]}
    with pytest.raises(PairDataError):
        QuoteRegistry(rpc).refresh()


@pytest.mark.parametrize("mint,want", [(SOL_SENTINEL, WRAPPED_SOL), (WRAPPED_SOL, WRAPPED_SOL), (USDC, USDC)])
def test_normalize_sol(mint, want):
    assert normalize_sol(mint) == want


def test_curve_uses_derived_address_and_normalizes_sol():
    rpc = rpc_for(curve_accounts(ORIGINAL, SOL_SENTINEL))
    pair = PumpPairs(rpc).resolve_pair(ORIGINAL)
    assert (pair.base_mint, pair.quote_mint, pair.pool, pair.venue) == (
        ORIGINAL, WRAPPED_SOL, curve_address(ORIGINAL), "bonding_curve",
    )
    assert PumpPairs(rpc).quote_decimals(ORIGINAL) == 6
    assert parse(f"@justmogit_bot mog it {ORIGINAL}").cas == [ORIGINAL]


def test_completed_curve_uses_canonical_pumpswap_pool():
    addr, state = pool_account(ORIGINAL, WRAPPED_SOL)
    accounts = curve_accounts(ORIGINAL, SOL_SENTINEL, complete=True) | {addr: state}
    pair = PumpPairs(rpc_for(accounts)).resolve_pair(ORIGINAL)
    assert (pair.venue, pair.pool, pair.quote_mint) == ("pumpswap", addr, WRAPPED_SOL)


def fixture_engine(registry, accounts, decision, *, candidates=None, liquidity="80000", floor="50000",
                   store_path=":memory:"):
    rpc = rpc_for(accounts)
    evaluator = Mock(return_value=decision)
    review = PairPreview(
        PumpPairs(rpc), Mock(refresh=Mock(return_value=registry)),
        lambda _pair: PairContext(
            "x402/PING", "NVDA", candidates or
            (QuoteCandidate(WRAPPED_SOL, "crypto"), QuoteCandidate(USDC, "stablecoin")),
            measure_curve(80_000_000_000, 6, 1) if liquidity == "80000" else
            LiquidityMeasurement("curve_reserves", liquidity),
            floor,
        ),
        evaluator,
    )
    x, vault, pump, payments = Mock(), Mock(), Mock(), Mock()
    store = Store(store_path)
    engine = Engine(store, load(overrides={"allowlist": ["111"]}),
                    Adapters(x=x, vault=vault, pump=pump, rpc=rpc, payments=payments), pair_preview=review)
    engine.boot()
    engine.set_kill(False)
    return engine, evaluator


def mention(mint=ORIGINAL, **changes):
    return {"id": "fixture-mention", "author_id": "111", "verified_type": "blue",
            "followers_count": 60, "text": f"@justmogit_bot mog it {mint}"} | changes


def preview_record(engine):
    row, = engine.s.ledger("pair_preview")
    assert row["lamports"] == 0
    for name in ("rpc", "x", "vault", "pump", "payments"):
        adapter = getattr(engine.a, name)
        if name == "rpc":
            adapter.send.assert_not_called()
        else:
            assert adapter.mock_calls == []
    assert not engine.s.ledger("launch")
    assert engine.s.db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
    return json.loads(row["body"])


@pytest.mark.parametrize("wrong", [None, "base", "quote", "owner"])
def test_bad_pool_never_reaches_evaluator(registry, wrong):
    addr, state = pool_account(ORIGINAL, WRAPPED_SOL,
                               actual_base=OTHER if wrong == "base" else None,
                               actual_quote=USDC if wrong == "quote" else None)
    if wrong == "owner":
        state["owner"] = PUMP
    accounts = curve_accounts(ORIGINAL, SOL_SENTINEL, complete=True)
    if wrong is not None:
        accounts[addr] = state
    engine, evaluator = fixture_engine(registry, accounts, None)
    assert engine.ingest(mention()) == ("PAIR_REVIEWED", "reject")
    evaluator.assert_not_called()
    assert preview_record(engine)["reason"] == "unresolved_pool"


def test_relaunch_usdc_to_sol_is_only_a_preview(registry):
    decision = EvaluatorResult("relaunch", "fixture", ORIGINAL, WRAPPED_SOL)
    engine, evaluator = fixture_engine(registry, curve_accounts(ORIGINAL, USDC), decision)
    assert engine.ingest(mention()) == ("PAIR_REVIEWED", "relaunch")
    assert evaluator.call_args.args[0].quote_mint == USDC
    assert engine.ingest(mention()) == ("duplicate_tweet", None)
    evaluator.assert_called_once()
    record = preview_record(engine)
    assert record["selected_quote_mint"] == WRAPPED_SOL
    assert record["reply_preview"] is None


@pytest.mark.parametrize("quote", [USDC, SOL_SENTINEL])
def test_matching_pair_keeps_with_threaded_reply_preview(registry, quote):
    selected = normalize_sol(quote)
    decision = EvaluatorResult("keep", "fixture", ORIGINAL, selected)
    engine, evaluator = fixture_engine(registry, curve_accounts(ORIGINAL, quote), decision)
    assert engine.ingest(mention()) == ("PAIR_REVIEWED", "keep")
    record = preview_record(engine)
    assert record["mention_id"] == mention()["id"]
    assert record["reply_preview"].startswith("Keeping this one")
    assert evaluator.call_args.args[0].quote_class == ("stablecoin" if quote == USDC else "crypto")


@pytest.mark.parametrize("selected,floor,liquidity,want", [
    (ROGUE, "50000", "80000", "quote_not_supported"),
    (USDC, "50000", "80000", "same_quote"),
    (WRAPPED_SOL, None, "80000", "unset_floor"),
    (WRAPPED_SOL, "50000", None, "missing_liquidity"),
    (WRAPPED_SOL, "50000", "0", "low_liquidity"),
])
def test_relaunch_fails_closed(registry, selected, floor, liquidity, want):
    decision = EvaluatorResult("relaunch", "fixture", ORIGINAL, selected)
    engine, _ = fixture_engine(registry, curve_accounts(ORIGINAL, USDC),
                               decision, floor=floor, liquidity=liquidity)
    assert engine.ingest(mention()) == ("PAIR_REVIEWED", "reject")
    assert preview_record(engine)["reason"] == want


def test_wrong_target_and_unapproved_narrative_quote_are_rejected(registry):
    pair = PumpPairs(rpc_for(curve_accounts(ORIGINAL, USDC))).resolve_pair(ORIGINAL)
    context = PairContext("fixture", "", (QuoteCandidate(USDC, "stablecoin"),))
    raw = EvaluatorResult("relaunch", "fixture", ORIGINAL, WRAPPED_SOL)
    assert validate_decision(raw, pair, registry, context).reason == "quote_not_approved_for_narrative"
    assert validate_decision(replace(raw, target_mint=OTHER), pair, registry, context).reason == "target_mismatch"


def test_ambiguous_quote_queues_exact_mints_and_revalidates_operator_choice(registry, tmp_path):
    choices = (QuoteCandidate(NVDA_X, "equity", "xStocks"),
               QuoteCandidate(NVDA_B, "equity", "Backpack Securities", "Sunrise"),
               QuoteCandidate(ROGUE, "unknown"))
    decision = EvaluatorResult("ask", "fixture", ORIGINAL, None)
    store_path = tmp_path / "queue.sqlite"
    engine, evaluator = fixture_engine(
        registry, curve_accounts(ORIGINAL, USDC), decision, candidates=choices, store_path=store_path,
    )
    assert engine.ingest(mention()) == ("PAIR_REVIEWED", "ask")
    assert preview_record(engine)["reply_preview"] is None
    assert {c.mint for c in evaluator.call_args.args[1].candidates} == {NVDA_X, NVDA_B}
    row, = engine.s.ledger("pair_review_request")
    assert {c["mint"] for c in json.loads(row["body"])["candidates"]} == {NVDA_X, NVDA_B}
    with pytest.raises(ValueError, match="current quote eligibility"):
        engine.s.resolve_pair_review(row["job_id"], NVDA_B, by="owner", eligible_mints={NVDA_X})
    engine.s.resolve_pair_review(row["job_id"], NVDA_X, by="owner", eligible_mints={NVDA_X})
    assert engine.s.pair_review_choice("x402/PING", "pump.fun", "NVDA")["mint"] == NVDA_X
    with pytest.raises(ValueError, match="already resolved"):
        engine.s.resolve_pair_review(row["job_id"], NVDA_B, by="owner", eligible_mints={NVDA_B})
    engine.s.db.close()
    persisted = Store(store_path)
    assert persisted.pair_review_choice("x402/PING", "pump.fun", "NVDA") == {
        "mint": NVDA_X, "by": "owner", "job_id": row["job_id"],
    }
    persisted.db.close()


def test_curve_liquidity_uses_raw_units_and_keeps_missing_distinct_from_zero():
    assert measure_curve(2_000_000_000, 9, "123").usd == Decimal("246")
    assert measure_curve(0, 6, "1").usd == 0
    assert measure_curve(0, 6, None).usd is None


def test_executable_liquidity_is_required_for_pumpswap(registry):
    address, state = pool_account(ORIGINAL, USDC)
    accounts = curve_accounts(ORIGINAL, USDC, complete=True) | {address: state}
    pair = PumpPairs(rpc_for(accounts)).resolve_pair(ORIGINAL)
    raw = EvaluatorResult("relaunch", "fixture", ORIGINAL, WRAPPED_SOL)
    context = PairContext("fixture", "", (QuoteCandidate(WRAPPED_SOL, "crypto"),),
                          LiquidityMeasurement("curve_reserves", Decimal(100)), Decimal(50))
    assert validate_decision(raw, pair, registry, context).reason == "missing_liquidity"
    assert validate_decision(raw, pair, registry, replace(
        context, liquidity=LiquidityMeasurement("executable", Decimal(100)),
    )).action == "relaunch"


@pytest.mark.parametrize("changes", [{"followers_count": 59}, {"verified_type": None}, {"followers_count": True}])
def test_author_gate_runs_before_model(registry, changes):
    engine, evaluator = fixture_engine(registry, {}, None)
    assert engine.ingest(mention(**changes)) == ("ignored", "author_gate")
    evaluator.assert_not_called()


def test_public_engine_refuses_pair_preview():
    with pytest.raises(BootRefused):
        Engine(Store(), load(), Adapters(), public=True, pair_preview=Mock())


def create_receipt(quote=None):
    keys = [NEW, OTHER, curve_address(NEW)] + [OTHER] * 4 + [TOKEN_2022] + [OTHER] * 8
    if quote is not None:
        keys.extend([quote, OTHER, SPL_TOKEN])
    return {"meta": {"err": None}, "transaction": {"signatures": [SIGNATURE], "message": {
        "accountKeys": keys + [PUMP],
        "instructions": [{"programIdIndex": len(keys), "accounts": list(range(len(keys))),
                          "data": b58(CREATE_V2 + b"fixture")}],
    }}}


@pytest.mark.parametrize("status,tx_quote,curve_quote,program,ok", [
    ("finalized", None, SOL_SENTINEL, TOKEN_2022, True),
    ("finalized", WRAPPED_SOL, SOL_SENTINEL, TOKEN_2022, True),
    ("confirmed", None, SOL_SENTINEL, TOKEN_2022, False),
    ("finalized", USDC, SOL_SENTINEL, TOKEN_2022, False),
    ("finalized", None, USDC, TOKEN_2022, False),
    ("finalized", None, SOL_SENTINEL, SPL_TOKEN, False),
])
def test_receipt_independently_verifies_finalized_mint_and_quote(status, tx_quote, curve_quote, program, ok):
    accounts = curve_accounts(NEW, curve_quote, program=program)
    rpc = rpc_for(accounts, status=status, tx=create_receipt(tx_quote))
    result = PumpPairs(rpc).verify_create(SIGNATURE, NEW, WRAPPED_SOL)
    if ok:
        assert (result.base_mint, result.quote_mint) == (NEW, WRAPPED_SOL)
    else:
        assert result is None
