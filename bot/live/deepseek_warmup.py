"""Boot warm-up for the DeepSeek pair evaluator (caller side; deepseek.py is untouched).

Timeout ruling 2026-11-07 (docs/DEEPSEEK-TIMEOUT.md): the adapter keeps timeout_s 12.0
and raises on timeout with no retry. The caller warms the route once at boot, then
PairPreview retries exactly once on a timeout; a second timeout is a skip, no launch.

The warm-up sends one GET /models and one minimal non-case chat completion, the same
shape the CI smoke runner proved (run 42407864926). No pair, mention or candidate data
is ever sent here."""
import os

import httpx

from ..pair_review import PairPreview
from . import deepseek
from .http import env

MODELS_URL = "https://api.deepseek.com/models"


def warm_payload(model):
    return {
        "model": model,
        "thinking": {"type": "disabled"},
        "temperature": 0.1,
        "max_tokens": 16,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": "Reply with JSON: {\"ok\": true}"}],
    }


def make_warm_up(*, api_key, model=deepseek.DEFAULT_MODEL, timeout_s=12.0, client=None):
    """Return a callable that warms the provider route once. It returns True on two
    HTTP 200s and False otherwise; it never raises and never retries."""
    get = httpx.get if client is None else client.get
    post = httpx.post if client is None else client.post
    headers = {"Authorization": f"Bearer {api_key}"}

    def warm_up():
        try:
            r1 = get(MODELS_URL, headers=headers, timeout=timeout_s, follow_redirects=False)
            r2 = post(deepseek.URL, headers=headers, json=warm_payload(model),
                      timeout=timeout_s, follow_redirects=False)
        except httpx.HTTPError:
            return False
        return r1.status_code == 200 and r2.status_code == 200

    return warm_up


def build_warmed_deepseek_preview(resolver, registry, context, *, metadata, client=None):
    """Same as deepseek.build_deepseek_preview, plus the boot warm-up hook."""
    api_key = env("DEEPSEEK_API_KEY")
    model = os.environ.get("DEEPSEEK_MODEL", "").strip() or deepseek.DEFAULT_MODEL
    evaluator = deepseek.DeepSeekEvaluator(
        api_key=api_key, metadata=metadata, model=model, client=client,
    )
    return PairPreview(resolver, registry, context, evaluator,
                       warm_up=make_warm_up(api_key=api_key, model=model, client=client))
