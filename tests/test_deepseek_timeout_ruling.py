"""Pins the DeepSeek timeout ruling (2026-11-07, docs/DEEPSEEK-TIMEOUT.md).

Adapter (bot/live/deepseek.py, byte-untouched): timeout_s 12.0; raise on timeout, no
retry; 401/403 no retry; 5xx one retry after 1s.
Caller (PairPreview + Engine.boot): warm-up at boot, then exactly ONE retry on a
timeout. A second timeout is a skip (reject), never a launch."""
import hashlib
import inspect
import json
from pathlib import Path
from unittest.mock import Mock, call

import httpx
import pytest

from bot.adapters import Adapters
from bot.engine import Engine
from bot.live import deepseek, deepseek_warmup
from bot.pair_review import EvaluatorResult, PairEvaluatorError, PairPreview
from bot.policy import load
from bot.store import Store
from tests.test_deepseek_evaluator import BASE, CONTEXT, CURRENT, META, PAIR, completion, decision

ADAPTER = Path(__file__).resolve().parents[1] / "bot" / "live" / "deepseek.py"
ADAPTER_SHA256 = "0430730b03694473c5d7ac2aa7b73cc5846073b1e31100714e1fcc6aec9868ef"
KEEP = EvaluatorResult("keep", "fits", BASE, CURRENT)


# ---------------------------------------------------------------- adapter, untouched
def test_adapter_bytes_unchanged():
    assert hashlib.sha256(ADAPTER.read_bytes()).hexdigest() == ADAPTER_SHA256


def test_adapter_timeout_default_is_12s_not_30s():
    default = inspect.signature(deepseek.DeepSeekEvaluator).parameters["timeout_s"].default
    assert default == 12.0


@pytest.fixture
def adapter(monkeypatch):
    client, sleep = Mock(), Mock()
    monkeypatch.setattr(deepseek.time, "sleep", sleep)
    return deepseek.DeepSeekEvaluator(api_key="fixture-only", metadata=Mock(return_value=META),
                                      client=client), client, sleep


def test_adapter_sends_12s_timeout(adapter):
    evaluate, client, _ = adapter
    client.post.return_value = completion(decision())
    evaluate(PAIR, CONTEXT)
    assert client.post.call_args.kwargs["timeout"] == 12.0


def test_adapter_raises_on_timeout_without_retry(adapter):
    evaluate, client, sleep = adapter
    client.post.side_effect = httpx.ReadTimeout("offline")
    with pytest.raises(PairEvaluatorError) as exc:
        evaluate(PAIR, CONTEXT)
    assert exc.value.kind == "timeout"
    assert client.post.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_adapter_auth_errors_not_retried(adapter, status):
    evaluate, client, sleep = adapter
    client.post.return_value = httpx.Response(status)
    with pytest.raises(PairEvaluatorError) as exc:
        evaluate(PAIR, CONTEXT)
    assert exc.value.kind == "auth"
    assert client.post.call_count == 1
    sleep.assert_not_called()


def test_adapter_5xx_one_retry_after_1s(adapter):
    evaluate, client, sleep = adapter
    client.post.side_effect = [httpx.Response(502), completion(decision())]
    assert evaluate(PAIR, CONTEXT).action == "keep"
    assert client.post.call_count == 2
    assert sleep.call_args_list == [call(1.0)]


# ---------------------------------------------------------------- caller: one retry
def _preview(evaluator, warm_up=None):
    resolver = Mock(resolve_pair=Mock(return_value=PAIR))
    registry = Mock(refresh=Mock(return_value=CONTEXT.candidates))
    return PairPreview(resolver, registry, lambda _: CONTEXT, evaluator, warm_up=warm_up)


def _timeout():
    return PairEvaluatorError("timeout", "request timed out")


def test_caller_retries_exactly_once_on_timeout_then_succeeds():
    evaluator = Mock(side_effect=[_timeout(), KEEP])
    decision_, _ = _preview(evaluator).review(BASE)
    assert decision_.action == "keep"
    assert evaluator.call_count == 2


def test_caller_second_timeout_is_skip_no_launch():
    evaluator = Mock(side_effect=[_timeout(), _timeout(), KEEP])
    decision_, context = _preview(evaluator).review(BASE)
    assert (decision_.action, decision_.reason, decision_.kind) == ("reject", "pair_review_failed", "timeout")
    assert context is None
    assert evaluator.call_count == 2  # never a third call


@pytest.mark.parametrize("kind", ["auth", "rate_limited", "unavailable", "parse"])
def test_caller_does_not_retry_other_failures(kind):
    evaluator = Mock(side_effect=[PairEvaluatorError(kind), KEEP])
    decision_, _ = _preview(evaluator).review(BASE)
    assert (decision_.action, decision_.kind) == ("reject", kind)
    assert evaluator.call_count == 1


def test_engine_second_timeout_records_skip_and_never_touches_launch_adapters():
    evaluator = Mock(side_effect=[_timeout(), _timeout()])
    warm = Mock(return_value=True)
    preview = _preview(evaluator, warm_up=warm)
    x, pump, vault, rpc, payments = (Mock() for _ in range(5))
    store = Store()
    engine = Engine(store, load(overrides={"allowlist": ["111"]}),
                    Adapters(x=x, pump=pump, vault=vault, rpc=rpc, payments=payments),
                    pair_preview=preview)
    engine.boot()
    warm.assert_called_once()
    assert store.get("pair_preview_warm") is True
    engine.set_kill(False)
    mention = {"id": "timeout-test", "author_id": "111", "verified_type": "blue",
               "followers_count": 60, "text": f"@justmogit_bot mog it {BASE}"}
    assert engine.ingest(mention) == ("PAIR_REVIEWED", "reject")
    event = json.loads(store.ledger("pair_preview")[0]["body"])
    assert (event["reason"], event["kind"]) == ("pair_review_failed", "timeout")
    assert evaluator.call_count == 2
    assert all(a.mock_calls == [] for a in (x, pump, vault, rpc, payments))
    assert not store.ledger("launch")


# ---------------------------------------------------------------- caller: boot warm-up
def test_warm_up_failure_never_blocks_boot():
    preview = _preview(Mock(), warm_up=Mock(side_effect=RuntimeError("cold")))
    store = Store()
    state = Engine(store, load(), Adapters(), pair_preview=preview).boot()
    assert state["kill"] is True
    assert store.get("pair_preview_warm") is False


def test_warm_up_sends_models_get_and_one_non_case_ping_only():
    seen = []

    def capture(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return completion({"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(capture))
    warm = deepseek_warmup.make_warm_up(api_key="fixture-only", model="deepseek-flash", client=client)
    with client:
        assert warm() is True
    assert [(r.method, str(r.url)) for r in seen] == [
        ("GET", deepseek_warmup.MODELS_URL), ("POST", deepseek.URL)]
    body = json.loads(seen[1].content)
    assert body["model"] == "deepseek-flash"
    assert len(body["messages"]) == 1
    for marker in (BASE.encode(), CURRENT.encode(), b"narrative", b"candidates"):
        assert marker not in seen[1].content


def test_warm_up_timeout_returns_false_without_retry():
    client = Mock()
    client.get.side_effect = httpx.ReadTimeout("offline")
    warm = deepseek_warmup.make_warm_up(api_key="fixture-only", client=client)
    assert warm() is False
    assert client.get.call_count == 1
    client.post.assert_not_called()
    assert client.get.call_args.kwargs["timeout"] == 12.0


def test_warmed_factory_wires_hook(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-only")
    preview = deepseek_warmup.build_warmed_deepseek_preview(
        Mock(), Mock(), lambda _: CONTEXT, metadata=lambda *_: META, client=Mock())
    assert isinstance(preview.evaluator, deepseek.DeepSeekEvaluator)
    assert preview._warm_up is not None
