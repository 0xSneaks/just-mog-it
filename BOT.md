# MogIt bot core — grok-bot/builder (branch grok-bot/bot-core)

Status: INTENT/VERIFIED-local. Core flow + 4 gates + mock tests. **No adapters wired, nothing can sign or send.**

| Gate (claude-code 2026-11-01) | Where | Proof |
|---|---|---|
| 1 lifetime cap vs ledger file before signing; write only after success; missing ledger = refuse | `mogit/ledger.py`, `flow.py` | tests cap/missing/ledger_only_after_success |
| 2 every tx simulated, cost shown before send | `flow._render` | test_sim_fail_no_send, card shows `sim.lamports_delta` |
| 3 program + accounts pinned from real SA/TREMP txs (mainnet RPC read 2026-11-04) | `mogit/guard.py` | real fixtures pass; tamper/wrong mint/2-ix/truncated refused |
| 4 tests mocks only, zero mainnet | `tests/` | socket patched to raise during tests |

Pins: program `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` (Meteora DBC), discriminator `8c55d7b06636684f`, 16 accounts, launch wallet `2LYQUArY…sHvh7`… fixed idx 0,1,2,4,9–15.

Run: `python3 -m unittest discover -s tests -v`

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

Run: `python3 -m unittest discover -s tests -v` (131 OK: 67 money core + 64 bot).

VERIFIED offline only — this build box can't reach X, Solana RPC, pump.fun, PumpPortal, Pinata or Gemini. PDA derivation VERIFIED against two real addresses (Meteora DBC + pump.fun event authorities). Discriminators VERIFIED by sha256 against published values.

Still owed (⚠️ HIGH-RISK, Sneaks's yes + independent review): the vault service itself (keys), one real PumpPortal create tx captured as a fixture to pin `pumpguard.py` (PumpPortal may return create_v2, which the guard refuses until pinned), a dry-run day against live X + RPC, and claude-code review of the money paths.
