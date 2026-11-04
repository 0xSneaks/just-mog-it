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
