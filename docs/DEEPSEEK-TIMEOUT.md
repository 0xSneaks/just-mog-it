# DeepSeek timeout ruling (2026-11-07)

Status: **live and working.** `DeepSeekEvaluator` (`bot/live/deepseek.py`, model `deepseek-flash`) is the pair evaluator.

## Ruling

| Layer | Behaviour | Pinned by |
|---|---|---|
| Adapter `bot/live/deepseek.py` | Left byte-for-byte unchanged (sha256 `0430730b…68ef`). `timeout_s = 12.0`. Timeout → raise `timeout`, no retry. 401/403 → raise `auth`, no retry. 5xx → one retry after 1 s. | `tests/test_deepseek_timeout_ruling.py` |
| Caller `bot/pair_review.py` `PairPreview` | Exactly **one** retry when the evaluator raises `timeout`. A second timeout returns `reject / pair_review_failed / kind=timeout`. That means skip, with no launch. No other failure kind is retried by the caller. | same |
| Caller `bot/engine.py` `Engine.boot` | Calls `PairPreview.warm_up()` once at boot. A failed warm-up never blocks boot, and the kill switch stays ON. Result stored as `pair_preview_warm`. | same |
| Warm-up `bot/live/deepseek_warmup.py` | One `GET /models` plus one minimal non-case chat completion. No pair, mention or candidate data. 12 s timeout, no retry. | same |

The proposed change to `timeout_s = 30.0` is **rejected**.

## Why

- The observed timeouts were cold-route only. The runner notes three 12 s read timeouts on first use, after which latency fell to about 2 s. A warm-up plus one retry handles that cold start. A 30 s timeout would only make every real stall cost 30 s per attempt.
- Evidence after the warm-up: Actions run [42407864926](https://github.com/0xSneaks/just-mog-it/actions/runs/42407864926), `conclusion: success`. The warm-up took 1.65 s. Cases ran in 1.19–1.70 s, each on its first POST, and no 429 was observed. Nothing in it contradicts a 12 s timeout. The sanitized output is in [`evidence/deepseek-smoke-run-42407864926.json`](evidence/deepseek-smoke-run-42407864926.json).
- Worst case: 12 s + 12 s = 24 s, and then the job is skipped. The bot never waits longer than that on DeepSeek.
- Retrying on 429 stays inside the adapter, which already has bounded backoff. Nothing here induces a 429.
