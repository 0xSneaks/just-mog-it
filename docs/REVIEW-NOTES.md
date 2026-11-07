# REVIEW-NOTES: claude-code fix pass, 2026-11-07 (for Codex review)

Branch `claude/admiring-mayer-3n4uj8`, on top of main `b5e18a5` (PR #4 merge).

## Money / signing paths touched

**None.** The money core (`mogit/flow.py`, `mogit/ledger.py`, `mogit/guard.py`) has no changes, and no signing code was added. Caps, the kill-switch default and the $30 lifetime cap are untouched.

## Lines next to money or launch decisions (please review)

| File | Lines | Change | Why it matters |
|---|---|---|---|
| `bot/engine.py` | `boot()`, +3 lines after the bankroll default | Calls `pair_preview.warm_up()` once and stores the result in `pair_preview_warm` | Runs after the kill switch is set ON. A failed warm-up returns False and never blocks boot. |
| `bot/pair_review.py` | `PairPreview.__init__`, `warm_up`, `_evaluate`; `review()` now calls `_evaluate` | Exactly one evaluator retry on `kind == "timeout"`. A second timeout returns reject / `pair_review_failed` / `kind=timeout` | This is the preview-only path (Engine refuses `public=True` with a preview). Reject means skip, with no launch. |
| `bot/callout.py` | `wallet_mint_balance`, `CalloutFlow.post` | Reads the posting wallet's balance of the mint over SPL Token and Token-2022. A balance above 0 or any read error means REFUSED and no POST | Read-only RPC. No keys. The default adapters are unwired and raise, so the flow refuses. |
| `bot/metadata.py` | `RETIRED_DESC`, `build_description` | Strips "not the original dev" and standalone "NFA" from copied target descriptions | This is metadata text only. |

## Not built (blocked)

The Mogz create adapter (Task 1) is not built. The proven create payload (quote = SOL, firstBuyQuote = 0) is not in this repo or in justmogit-site. It has been requested on ai-coms.

## Tests

`python3 -m pytest -q` → 246 passed. The new files are `tests/test_deepseek_timeout_ruling.py`, `tests/test_callout.py`, `tests/test_retired_disclaimer.py` and `TestLaunchMetadataReader` in `tests/test_bot_live.py`.
