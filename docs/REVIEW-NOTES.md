# REVIEW-NOTES: claude-code fix pass, 2026-11-07 (for Codex review)

Branch `claude/admiring-mayer-3n4uj8`, on top of main `b5e18a5` (PR #4 merge).

## Money / signing paths touched

Pass 1 (commits `b2bf74b`…`865a1ae`) touched no money path.

Pass 2 (consolidated directive, commit `0c50f9a`) **adds gates to the money core**. Every line below needs Codex review:

| File | Lines | Change |
|---|---|---|
| `mogit/gates.py` | new file | `SOL_MINT`, `PER_LAUNCH_CAP_LAMPORTS = 25_000_000`, `MAX_LAUNCHES_PER_DAY = 3`. `launches_today` counts settled + pending entries stamped today (UTC); an entry with no numeric `ts` counts as today (fail closed). `check_quote`: `quote_mint` must equal wSOL, `first_buy_lamports` must be the int 0, and quote SOL must be ≤ 0.025. `check_spend`: simulated spend must be ≤ 25,000,000 lamports. `check_preflight`: kill switch, then the daily limit, then `check_quote`. |
| `mogit/flow.py` | `_preflight` | Calls `gates.check_preflight` after `ledger.check`. This runs at `/mogit`, before build in `_render`, and again under the ledger lock in `_confirm`, before sign. |
| `mogit/flow.py` | `_render` | `gates.check_spend(spend)` runs before the FIX 2 sim-vs-quote check. |
| `mogit/flow.py` | `_confirm` | Re-checks `gates.check_spend` on the card's `sim.lamports_delta`, under the lock, before sign. |
| `mogit/flow.py` | `_confirm` | The reserve and settle records now carry `ts` from the session clock. |
| `mogit/ledger.py` | `load`, `_save` | Carry the raw `kill` value through every write, so it is never dropped. |
| `mogit/ledger.py` | `kill_on`, `set_kill`, `boot` | ON unless the stored value is exactly `False`. An unreadable ledger also means ON. `set_kill` writes under the lock and reads back. `boot()` sets ON. |

Test fixtures changed with it: the three fake quotes now declare `quote_mint` (wSOL) and `first_buy_lamports: 0`, and the test ledgers carry `"kill": false`. `test_sim_spend_over_quote_refused` now uses 22,000,000 lamports, so FIX 2 (not the new cap) is what refuses it.

Open question: legacy ledger entries without `ts` count as today, so a real ledger holding 3 or more old launches blocks every launch until they are backfilled with `ts`. This is deliberate (fail closed). Muse holds the real ledger.

`mogit/cli.py` does not call `Ledger.boot()`. Its adapters are unwired, so it refuses at preflight anyway.

## Lines next to money or launch decisions (please review)

| File | Lines | Change | Why it matters |
|---|---|---|---|
| `bot/engine.py` | `boot()`, +3 lines after the bankroll default | Calls `pair_preview.warm_up()` once and stores the result in `pair_preview_warm` | Runs after the kill switch is set ON. A failed warm-up returns False and never blocks boot. |
| `bot/pair_review.py` | `PairPreview.__init__`, `warm_up`, `_evaluate`; `review()` now calls `_evaluate` | Exactly one evaluator retry on `kind == "timeout"`. A second timeout returns reject / `pair_review_failed` / `kind=timeout` | This is the preview-only path (Engine refuses `public=True` with a preview). Reject means skip, with no launch. |
| `bot/callout.py` | `wallet_mint_balance`, `CalloutFlow.post` | Reads the posting wallet's balance of the mint over SPL Token and Token-2022. A balance above 0 or any read error means REFUSED and no POST | Read-only RPC. No keys. The default adapters are unwired and raise, so the flow refuses. |
| `bot/metadata.py` | `RETIRED_DESC`, `build_description` | Strips "not the original dev" and standalone "NFA" from copied target descriptions | This is metadata text only. |

## "mog me" X flow (pass 3, not a money path)

| File | Change |
|---|---|
| `bot/mogme_queue.py` | New. Intake filters (retweet, own post, dedupe), 24 h author cooldown, daily cap from prices (fail closed if a price is missing), shared $/day budget, author-only avatar, hardened fetch, `SiteMogEditor` (no retry), GET MOGGED tag, file queue, `post_ready`. `post_ready` posts only when the kill switch is exactly `False` and the item is APPROVED (or autopost is exactly `True`), and marks the item POSTING before any X call. |
| `bot/engine.py` | `mog_me` no longer renders, uploads or replies. It delegates to the queue (`mogme=` kwarg). With no queue wired it returns `mog_me_unwired`. |
| `bot/live/x.py` | `mentions()` adds `is_retweet` and the author's `profile_image_url`; new `profile_image_url(uid)`. |
| `bot/run.py` | New `mogme-preview`, `mogme-list`, `mogme-approve`, `mogme-reject` and `mogme-post` commands. `loop` wires the queue when `JMI_MOG_API_URL` is set. |
| `bot/mogme_queue.py` (pass 4) | `ineligible()` checks ≥ 60 followers + verified_type ∈ {blue, business, government}. A missing field means skip. `_follows_bot` needs `connection_status` to contain `followed_by`: taken from the mention expansion, else one lookup booked at $0.010; a failure means not following. `edit_pfp()` interface: `StubEditor` (default) and `GeminiEditor` (verbatim vipers prompt). `build_editor` refuses gemini without a key and refuses unknown values. Stub items are never posted. The earlier `/api/mog` caller (`SiteMogEditor`) is removed. |
| `mogit/bot.py` | `listen()` raises SystemExit. It used to reply on X with no approval, kill switch or budget. |
| `config/policy.yaml`, `bot/policy.py` | New required `mog_me` block: `autopost: false`, `style`, and UNVERIFIED prices. |

## Not built (blocked)

Mogz create adapter: CANCELLED (owner ruling 2026-11-07, mogz is out). No venue adapter until the pump.fun-or-Float decision.

## Tests

`python3 -m pytest -q` → 266 passed (pass 2; clean venv with `pip install -r requirements.txt`). The new files are `tests/test_deepseek_timeout_ruling.py`, `tests/test_callout.py`, `tests/test_retired_disclaimer.py` and `TestLaunchMetadataReader` in `tests/test_bot_live.py`.
