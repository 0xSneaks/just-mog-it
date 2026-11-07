> **SUPERSEDED (2026-11-07).** v0 document, archived unchanged below this header. Current: [BUILD-SPEC-v2.md](../../../BUILD-SPEC-v2.md) and [BOT.md](../../../BOT.md); front door: [README.md](../../../README.md).

---
name: mogit
description: Launch a coin on Mogz.fun (Solana memecoin launchpad) through a gated, human-confirmed flow. Trigger when the user types /mogit or explicitly asks to launch a coin on Mogz. A bare "launch a coin/token" with no venue named does NOT trigger this skill — ask which venue instead.
---

# MogIt — /mogit

Launch a coin on Mogz.fun by walking a fixed procedure: preflight, four prompts, a pinned-terms confirm card, and an explicit `LAUNCH`. Nothing in this skill auto-executes. Every launch is an irreversible real-money action gated on a human confirming the exact terms.

This file is the canonical playbook. Any agent that can run shell tools, read/write files, and open images can follow it step by step.

## 0. Trigger and scope

- Trigger: the user types `/mogit`, or explicitly asks to launch a coin **on Mogz**.
- Do NOT trigger on a bare "launch a coin / launch a token / mog it" with no venue named. The operator works across multiple venues (StonkFun/LaunchLab, Pump.fun, Mogz); a venue-less launch ask needs a venue choice first.
- v1 launch shape is fixed: a **single-instruction create** with `firstBuyQuote = 0` — no bundled dev buy. The transaction guard refuses the dev-buy bundle shape outright. Any initial buy is a separate step, confirmed separately, never part of the launch transaction. Costs quoted anywhere in this flow are the **create cost**.

## 1. Entry gate — preflight BEFORE any prompt

Run this before showing a single prompt. If preflight fails, refuse immediately with the reason (see §6). No prompts are shown for a launch that cannot proceed.

1. **Load the budget ledger.** Lifetime Mogz spend cap is **$30 USD total**. Verified baseline at spec date (2026-11-01): **$15.91 spent, $14.09 remaining**. Read the operator's real ledger for current figures; never trust a cached or remembered number.
2. **Read the launch wallet's actual SOL balance** from the working Mogz flow / chain — never a cached value.
3. **Get a real-time cost quote** from the working Mogz flow (never an estimate).
4. **Check funding:** if `wallet.balance_sol < cost.sol`, refuse: `wallet underfunded: need X, have Y`. Never promise a launch the wallet cannot fund.
5. **Check budget:** if `budget.remaining_usd < cost.usd`, refuse — budget cap hit (§6, case 1).

Preflight runs again immediately before execution (§4, step 3).

## 2. Prompt sequence

Ask in this order. Hold all values in a single working JSON object. **Nothing is written to the budget ledger until a confirmed launch succeeds.** The user may type `cancel` or `quit` at any prompt to abort cleanly — no money moved, no state written except an optional `aborted` event.

**Retries:** each field gets at most **2 retries**. After that, refuse and name the specific rule that failed.

### Prompt 1 — Name

- Asks: the coin's display name.
- Validation: 1–32 characters; non-blank; passes the banned-term check (§6, case 4); does not collide with an existing Mogz coin (query Mogz's lookup if it exposes one; otherwise best-effort substring check against a local list).
- Default: none.
- Bail-out: `cancel` → abort.

### Prompt 2 — Ticker

- Asks: the coin's symbol.
- Validation: 1–10 characters; uppercase A–Z and 0–9 only; must start with a letter; does not collide with an existing ticker.
- Default: the uppercased first 4–6 alphanumeric characters of the name — **shown for edit, never auto-accepted**.
- Bail-out: `cancel` → abort; empty input plus the user declining the default → refuse.

### Prompt 3 — Description (simple rule enforced)

- Asks: the coin's description.
- **The simple rule:** one or two plain sentences. No embellishment — no emoji, no hype, no "to the moon." The description **must credit the actual launcher** by name/handle. No product self-description.
- House style is short. Example of the form: `Elon Coin. To Mars. Launched on Mogz.`
- Template (editable): `"<Name> is a coin launched through MogIt. Launched by <launcher>."` — prefer the shorter house form when the user provides or accepts it.
- Bail-out: `cancel` → abort. A description that violates the simple rule and isn't fixed within the retry limit → refuse (§6, case 5).

### Prompt 4 — Image (media rule enforced)

- Asks: a path to the token image, or `none`.
- Default: `none` (skip image).
- **The media rule:** open the image with an image tool and *visually confirm* the content is actually the token's intended image — not a random screenshot, not disallowed content. **A path or filename is never proof of content.** Record the SHA-256 of the *verified* bytes (`image_sha256`); store the hash, not just the path.
- Bail-out: `cancel` → abort; bad path → re-ask once, then refuse. Cannot open the image, or the content is wrong/disallowed → refuse (§6, case 3), or let the user pick a different file once.

## 3. The confirm card — pinned terms

After the four prompts, render **one** confirm card. This is the only point where money can move. All fields are pinned and immutable once the card is rendered:

| Field | Meaning |
|---|---|
| `coin.name` | finalized name |
| `coin.ticker` | finalized ticker |
| `coin.description` | finalized description, exactly as it will be submitted |
| `coin.image_sha256` | SHA-256 of the verified image file, or `null` if `none` |
| `cost.sol` | exact launch (create) cost in SOL, from the real-time quote |
| `cost.usd` | same cost in USD, at the pinned quote rate, with timestamp |
| `wallet.address` | launch wallet public address |
| `wallet.balance_sol` | actual SOL balance from preflight (re-checked at confirm time) |
| `budget.spent_usd` | lifetime spent per the ledger |
| `budget.remaining_usd` | lifetime remaining per the ledger, minus this launch's cost |
| `quote.expires_at` | quote deadline — **now + 90 seconds**; an expired quote is re-quoted |
| `terms_hash` | SHA-256 over the canonical serialization of all fields above |
| `verb` | literal `LAUNCH` — the only accepted confirm string |

Emit the `prepared` webhook event when the card is rendered.

## 4. Confirm protocol

1. Render the card. Capture `terms_hash`.
2. Wait for explicit human confirmation: the word **`LAUNCH`** (or an unambiguous UI confirm button). Anything else = no action.
3. **Before executing:** re-run preflight (actual wallet balance) and re-verify `terms_hash` is unchanged. If either fails → refuse and regenerate the card.
4. Check the quote: if `now > quote.expires_at`, re-quote, show the updated cost, and get a fresh `LAUNCH` (§6, case 7).
5. If the user edits **any** field after the card was rendered, the confirm is void. Rebuild the card, capture a new `terms_hash`, and require a fresh `LAUNCH` (§6, case 6).
6. **Execute through the operator's working Mogz flow and its gated signing path:**
   - Mogz's server builds the create transaction (single instruction, `firstBuyQuote = 0`).
   - **A guard inspects the transaction bytes before anything is signed.** Nothing is signed that the guard has not inspected.
   - The launch wallet signs **only through the vault's gated execution path** (`execution.sign_mogz`). Keys never leave the vault; the skill never holds, logs, or transmits a private key, and there is no per-launch user-wallet prompt — the confirm card is what gates the vault's signing path.
   - Mogz's server cosigns the mint.
7. On success → commit the spend to the budget ledger and emit `launched`. On failure → emit `failed` and **do not** decrement the ledger. Never retry a failed launch blindly.

## 5. Post-launch report

On `launched`, report:

- `tx_signature` and/or `mint` address (whatever Mogz returns)
- Explorer link and coin page URL (if Mogz provides them)
- Final cost actually charged (SOL + USD)
- Updated budget: `spent_usd`, `remaining_usd`
- Timestamp, wallet address used, `image_sha256`, `terms_hash`, `event_id`

On `failed`, report the error code/reason and state explicitly that **no budget was spent**.

## 6. Refusal cases

Return without launching, and state the reason, when any of these hold:

1. **Budget cap hit** — `budget.remaining_usd < cost.usd`. Hard stop; the $30 cap is a lifetime ceiling.
2. **Wallet underfunded** — `balance_sol < cost.sol` at preflight or at the confirm-time recheck.
3. **Unverified image** — image not opened, not visually verified, or content mismatch. A path/filename is never sufficient proof.
4. **Name/ticker collision or banned content** — collides with an existing coin, or matches the banned-terms list (seeded from Mogz's own rules, plus a local supplemental list).
5. **Description violates the simple rule** — embellishment, hype, missing launcher credit, or more than two sentences.
6. **User edits terms after the confirm card** — any field change after `terms_hash` is captured invalidates the confirm; rebuild the card and require a fresh `LAUNCH`.
7. **Quote expired** — `now > quote.expires_at`; re-quote and show the updated cost.
8. **Launch flow unavailable** — the Mogz flow errors or times out, or the guard rejects the transaction bytes. Emit `failed`; do not retry blindly.

## 7. Hard rules (never bend)

- **Budget:** $30 USD lifetime cap across all Mogz launches through this skill. The ledger is durable and atomic; a launch decrements it only on `launched`.
- **Human gate:** explicit `LAUNCH` on pinned terms, every time. No auto-execute, ever. One launch per confirmation.
- **Media rule:** every image is opened and visually verified; the hash of the verified bytes is what gets recorded and reported.
- **Simple-description rule:** plain, short, launcher credited. It is structural anti-hype, not a style suggestion.
- **Key custody:** keys live in the vault and never leave it. Signing happens only through the gated path, only after guard inspection. No secrets in this repo, in logs, or in webhook payloads — ever.
- **Honesty:** reports and events carry only real, observed values — real cost, real balance, real signatures.

---

*Full design: [SPEC.md](SPEC.md). This skill text is additionally licensed CC-BY-4.0 (see README); code in this project is MIT.*
