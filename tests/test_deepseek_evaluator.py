"""Offline contract tests for the opt-in DeepSeek evaluator."""
import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest

from bot.adapters import Adapters
from bot.engine import BootRefused, Engine
from bot.live import deepseek
from bot.live.pairs import ResolvedPair, WRAPPED_SOL
from bot.pair_review import EvaluatorResult, LiquidityMeasurement, PairContext, QuoteCandidate
from bot.policy import load
from bot.store import Store
from mogit.guard import b58

BASE, CURRENT, OTHER = (b58(bytes([n]) * 32) for n in (21, 22, 23))
PAIR = ResolvedPair(BASE, CURRENT, OTHER, "bonding_curve", "stablecoin", 100)
CONTEXT = PairContext(
    "fixture narrative", "", (QuoteCandidate(CURRENT, "stablecoin"), QuoteCandidate(WRAPPED_SOL, "crypto")),
    LiquidityMeasurement("curve_reserves", Decimal("100")), Decimal("50"),
)
META = {"holders": 12, "volume_24h": "23.45", "age_s": 60}


def decision(**updates):
    return {"action": "keep", "reason": "current quote fits", "target_mint": BASE,
            "selected_quote_mint": CURRENT} | updates


def completion(raw=None, *, content=None, finish_reason="stop"):
    if content is None:
        content = json.dumps(raw)
    return httpx.Response(200, json={
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
    })


@pytest.fixture
def setup(monkeypatch):
    client = Mock()
    sleep = Mock()
    monkeypatch.setattr(deepseek.time, "sleep", sleep)
    meta = Mock(return_value=META)
    evaluate = deepseek.DeepSeekEvaluator(api_key="fixture-only", metadata=meta, client=client)
    return evaluate, client, meta, sleep


@pytest.mark.parametrize("action,quote", [("keep", CURRENT), ("relaunch", WRAPPED_SOL), ("reject", CURRENT)])
def test_happy_paths_and_request_shape(setup, action, quote):
    evaluate, client, _, _ = setup
    raw = decision(action=action, selected_quote_mint=quote, confidence=1, risk_flags=["ignored"])
    client.post.return_value = completion(raw)
    assert evaluate(PAIR, CONTEXT) == EvaluatorResult(
        action, raw["reason"], BASE, None if action == "reject" else quote,
    )
    args, kwargs = client.post.call_args
    assert args == ("https://api.deepseek.com/chat/completions",)
    assert kwargs["follow_redirects"] is False
    request = kwargs["json"]
    assert request["model"] == "deepseek-flash"
    assert request["response_format"] == {"type": "json_object"}
    body = json.loads(request["messages"][1]["content"])
    assert body["pair_meta"] == {"liquidity_usd": "100", "holders": "12", "volume_24h": "23.45", "age_s": "60"}
    assert {row["mint"] for row in body["candidates"]} == {CURRENT, WRAPPED_SOL}


@pytest.mark.parametrize("raw,reason", [
    (decision(target_mint=OTHER), "target_mint"),
    (decision(action="relaunch", selected_quote_mint=OTHER), "outside candidate set"),
    (decision(action="relaunch", selected_quote_mint=[]), "outside candidate set"),
])
def test_model_overrides_are_rejected(setup, raw, reason):
    evaluate, client, _, _ = setup
    client.post.return_value = completion(raw)
    result = evaluate(PAIR, CONTEXT)
    assert result.action == "reject" and result.target_mint == BASE and result.selected_quote_mint is None
    assert reason in result.reason


@pytest.mark.parametrize("response", [
    completion(content="not json"),
    completion(content="[]"),
    completion(decision(), finish_reason="length"),
    completion(decision(selected_quote_mint=WRAPPED_SOL)),
    completion(decision(reason="x" * 281)),
    completion({key: value for key, value in decision().items() if key != "selected_quote_mint"}),
    completion(decision(action="ask")),
])
def test_parse_failures_are_typed_without_retry(setup, response):
    evaluate, client, _, sleep = setup
    client.post.return_value = response
    with pytest.raises(deepseek.PairDecisionParseError):
        evaluate(PAIR, CONTEXT)
    client.post.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("field", ["liquidity_usd", "holders", "volume_24h", "age_s"])
def test_missing_metadata_blocks_model_call(setup, field):
    evaluate, client, meta, _ = setup
    ctx = replace(CONTEXT, liquidity=None) if field == "liquidity_usd" else CONTEXT
    meta.return_value = {key: value for key, value in META.items() if key != field}
    with pytest.raises(deepseek.MissingMetadataError, match=field):
        evaluate(PAIR, ctx)
    client.post.assert_not_called()


def test_empty_candidates_block_model_call(setup):
    evaluate, client, meta, _ = setup
    with pytest.raises(deepseek.UnsupportedQuoteError):
        evaluate(PAIR, replace(CONTEXT, candidates=()))
    client.post.assert_not_called()
    meta.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_not_retried(setup, status):
    evaluate, client, _, sleep = setup
    client.post.return_value = httpx.Response(status)
    with pytest.raises(deepseek.DeepSeekAuthError):
        evaluate(PAIR, CONTEXT)
    client.post.assert_called_once()
    sleep.assert_not_called()


def test_429_then_success(setup):
    evaluate, client, _, sleep = setup
    client.post.side_effect = [httpx.Response(429, headers={"Retry-After": "0"}), completion(decision())]
    assert evaluate(PAIR, CONTEXT).action == "keep"
    assert client.post.call_count == 2
    sleep.assert_called_once_with(0.0)


@pytest.mark.parametrize("retry_after,attempts,waits", [("0", 3, 2), ("30", 1, 0)])
def test_rate_limit_is_bounded_and_typed(setup, retry_after, attempts, waits):
    evaluate, client, _, sleep = setup
    client.post.return_value = httpx.Response(429, headers={"Retry-After": retry_after})
    with pytest.raises(deepseek.DeepSeekRateLimited):
        evaluate(PAIR, CONTEXT)
    assert client.post.call_count == attempts
    assert sleep.call_count == waits


@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.ConnectError("offline")])
def test_unavailable_retried_once(setup, failure):
    evaluate, client, _, sleep = setup
    client.post.side_effect = [failure, failure]
    with pytest.raises(deepseek.DeepSeekUnavailable):
        evaluate(PAIR, CONTEXT)
    assert client.post.call_count == 2
    sleep.assert_called_once_with(1.0)


def test_timeout_is_not_retried(setup):
    evaluate, client, _, sleep = setup
    client.post.side_effect = httpx.ReadTimeout("offline")
    with pytest.raises(deepseek.DeepSeekTimeout):
        evaluate(PAIR, CONTEXT)
    client.post.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("status,action,reason", [(200, "keep", "narrative_fit"), (401, "reject", "pair_review_failed")])
def test_factory_injects_into_engine_and_preserves_preview_wall(monkeypatch, status, action, reason):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-only")
    monkeypatch.setenv("DEEPSEEK_MODEL", "fixture-model")
    client = Mock()
    client.post.return_value = completion(decision()) if status == 200 else httpx.Response(status)
    resolver = Mock(resolve_pair=Mock(return_value=PAIR))
    registry = Mock(refresh=Mock(return_value=CONTEXT.candidates))
    preview = deepseek.build_deepseek_preview(
        resolver, registry, lambda _: CONTEXT, metadata=lambda *_: META, client=client,
    )
    policy = load(overrides={"allowlist": ["111"]})
    x, pump, vault, rpc, payments = (Mock() for _ in range(5))
    adapters = Adapters(x=x, pump=pump, vault=vault, rpc=rpc, payments=payments)
    with pytest.raises(BootRefused):
        Engine(Store(), policy, adapters, public=True, pair_preview=preview)
    store = Store()
    engine = Engine(store, policy, adapters, pair_preview=preview)
    engine.boot()
    engine.set_kill(False)
    mention = {"id": "preview-test", "author_id": "111", "verified_type": "blue",
               "followers_count": 60, "text": f"@justmogit_bot mog it {BASE}"}
    assert engine.ingest(mention) == ("PAIR_REVIEWED", action)
    assert json.loads(store.ledger("pair_preview")[0]["body"])["reason"] == reason
    assert client.post.call_args.kwargs["json"]["model"] == "fixture-model"
    assert all(adapter.mock_calls == [] for adapter in (x, pump, vault, rpc, payments))
    assert not store.ledger("launch")
