# JustMogIt — what is built (2026-11-07)

There are two separate paths in this repo. Neither has launched a coin. Launch #001 (mogz.fun) was made by hand through an external executor, not by this code.

| Path | What it is | Status |
|---|---|---|
| `mogit/` | **Money core.** Lifetime $30 ledger cap, reserve-before-submit, tx guard (pinned to Meteora DBC), simulate-before-send, and hard gates in `mogit/gates.py`: kill switch (persisted, default ON), max 3 launches/UTC day, 0.025 SOL per-launch cap on simulated spend, SOL-only quote, first buy 0. | BUILT, offline. **No venue adapter.** The venue is pump.fun or Float, undecided (owner, 2026-11-07). mogz.fun is out. |
| `bot/` | **X engine** (mentions → gates → dossier → reservation → fire → reply) plus the **pump.fun pipeline** (PumpPortal create, pump guard). | Logic BUILT offline with fake adapters. The pump.fun pipeline is SPECCED and has never launched. |

DeepSeek pair evaluator (`bot/live/deepseek.py`, `deepseek-flash`) is connected and working. See Actions run 42407864926 (success/PASS) and `docs/DEEPSEEK-TIMEOUT.md`.
**"mog me" on X** (`bot/mogme_queue.py`):
- **Who gets mogged:** only the mention author, using their own PFP.
- **Eligibility:** ≥ 60 followers, a checkmark, and following @justmogit_bot; otherwise a silent skip.
- **Editor:** `IMAGE_EDITOR=stub` (the default; never postable) or `gemini` (needs `GEMINI_API_KEY`). The Gemini prompt is a verbatim copy of justmogit-site `api/mog.js` `vipers`: wraparound shield sport shades in the input's own art style, fitted to the face, colours from its palette. **Traits, NFT-style,** are drawn once per mog from `mog_me.traits` and saved with the item:
- frame lettering: PIT VIPER, or plain glasses
- corner caption: GET MOGGED, MOGGED BY BRAINROT LABS, or none

Colour match: the picture's own palette (median-cut hex colours) goes into the prompt for the frames and lenses, and the caption box and ink use it too. A fit checklist covers lenses over the eyes, the brow line, face width, tilt and turn, and never covering the nose or mouth. `mogme-list` shows trait counts.
- **Before the edit:** avatars under 512 px are upscaled to 1024. Up to 3 owner-approved style references go along with each edit; `mogme-ref ID` adds one.
- **No-change check:** if under 6% of pixels changed, the try counts as a fail. There are at most 2 retries, then a polite text draft. Every try is billed.
- **Owner decisions:** approve and reject both need a short reason, which is saved with the item.
- **Budget:** mog-me has its own $5.00/day, separate from the $0.30 launch-reply budget. That is 72 mogs/day if every one works on the first try, or 34 if every one needs all 3 tries.
- **Posting:** `mogme-post` posts only APPROVED gemini items, and only with the kill switch OFF. Autopost is off.

**Monday switch to Gemini:**
1. Put `GEMINI_API_KEY` in the bot's runtime env.
2. Set `IMAGE_EDITOR=gemini`.
3. Run `python3 -m bot.run mogme-preview` once, then `python3 -m bot.run mogme-list`, and look at the PNG. Nothing posts.

`bot/callout.py` (mogz.fun callouts) is parked with mogz. Do not extend it unless a venue decision revives mogz callouts.

Run: `pip install -r requirements.txt && python3 -m pytest -q`

---

# MogIt bot core — grok-bot/builder (branch grok-bot/bot-core)

Status: INTENT/VERIFIED-local. Core flow + 4 gates + mock tests. **No adapters wired, nothing can sign or send.**

| Gate (claude-code 2026-11-01) | Where | Proof |
|---|---|---|
| 1 lifetime cap vs ledger file before signing; write only after success; missing ledger = refuse | `mogit/ledger.py`, `flow.py` | tests cap/missing/ledger_only_after_success |
| 2 every tx simulated, cost shown before send | `flow._render` | test_sim_fail_no_send, card shows `sim.lamports_delta` |
| 3 program + accounts pinned from real SA/TREMP txs (mainnet RPC read 2026-11-04) | `mogit/guard.py` | real fixtures pass; tamper/wrong mint/2-ix/truncated refused |
| 4 tests mocks only, zero mainnet | `tests/` | socket patched to raise during tests |

Pins: program `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` (Meteora DBC), discriminator `8c55d7b06636684f`, 16 accounts, launch wallet `2LYQUArY…sHvh7`… fixed idx 0,1,2,4,9–15.

Run: `python3 -m pytest -q`

Open: surface (Sneaks decides), real adapters (muse's Mogz flow + vault `execution.sign_mogz`), real ledger file (muse holds it), image verifier. Review → claude-code before any live use.

---

# X bot surface — `bot/` (claude, 2026-11-07, branch `claude/x-bot-surface`)

Status: INTENT/VERIFIED-local. Implements BUILD-SPEC-v2 logic for @justmogit_bot with **fake adapters only**. Nothing reads X, nothing signs, nothing sends. `mogit/` is untouched.

| Piece | File | Spec |
|---|---|---|
| Owner numbers (all of §0 + brief) | `config/policy.yaml`, `bot/policy.py` | §0 — boot refuses bad/missing values |
| Trigger parse ("mog me" vs "just mog it", CA, $TICKER, pump link) | `bot/parse.py` | §3 step 1 |
| Hard-skip screen | `bot/screen.py` | owner guardrail |
| Rugged / wrong-pair eligibility | `bot/rug.py` | owner brief (PROPOSED numbers) |
| Spelling lock | `bot/spelling.py` | §6 |
| Dossier + rescue prefix + never-copied fields | `bot/metadata.py` | §3 |
| Job state machine, gates, reservation, fire, UNKNOWN reconcile, kill, expiry | `bot/engine.py`, `bot/store.py` | §1.3, §7, §8 |
| Fee loop 25/25/50, tiers, holder liability, mogger dust min | `bot/fees.py` | §9, owner brief |
| Replies (plain-text CA, no URLs, $0.30/day budget) | `bot/replies.py` | §7 |
| "mog me" glasses renderer (original art) | `bot/mogme.py` | §0.16 (still OFF) |
| Adapter contracts (all Unwired by default → fail closed) | `bot/adapters.py` | — |

Order: gates → dossier/eligibility → **then** ask for the 0.02 SOL fee → reserve 0.025 → owner approval → arm → guard + simulate → sign → send.

Live adapters (`bot/live/`): X v2 (mentions, reply, chunked media upload), Solana RPC (simulate payer delta, dual send, status, blockhash expiry, PDA), PumpPortal trade-local create + collectCreatorFee with Pinata metadata, DAS + pump.fun + DexScreener dossier, Gemini inspection + eye landmarks, 0.02 SOL payment verifier (memo = job id, no replay), vault signer client. `bot/pumpguard.py` decodes the pump.fun create/buy/claim instructions and refuses anything else. Runner/console: `python3 -m bot.run` (loop --dry-run, status, approve, launch, kill, bankroll).

Run: `python3 -m pytest -q` (the full suite; see the top of this file).

VERIFIED offline only — this build box can't reach X, Solana RPC, pump.fun, PumpPortal, Pinata or Gemini. PDA derivation VERIFIED against two real addresses (Meteora DBC + pump.fun event authorities). Discriminators VERIFIED by sha256 against published values.

Still owed (⚠️ HIGH-RISK, Sneaks's yes + independent review): the vault service itself (keys), one real PumpPortal create tx captured as a fixture to pin `pumpguard.py` (PumpPortal may return create_v2, which the guard refuses until pinned), a dry-run day against live X + RPC, and claude-code review of the money paths.
