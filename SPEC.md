# MogIt — Refined Product Spec

- **Source draft:** Sneaks, 2026-11-01 ("Just mog it. Create a token for it. Then make a GitHub. Then a website with hooks. It will be an agent skill so when you type /mogit — coin is launched or whatever prompt pops up. Rough draft needs work.")
- **Refined by:** OpenCode Go:deepseek-v4-pro (DeepSeek V4 Pro, via the Seeker bench / agora_ask), 2026-11-01
- **Status:** SPEC ONLY — nothing built, no token launched, no repo or site created. Corrections to the model's assumptions are in the final section, "Muse notes."

---

# MogIt — Buildable Product Spec

**Status legend used throughout:** `VERIFIED` = given in the brief / already true. `CONTRACT` = must be pinned from the operator's existing working Mogz flow before code ships. `INTENT` = design choice to build. Nothing below invents facts about Mogz.fun's API, fees, or settings — where Mogz behavior is unknown it is called out as a pin-point, not assumed.

---

## 1. `/mogit` skill UX flow

### 1.1 Invocation

- Trigger: user types `/mogit`, or asks to "launch a coin," "launch a token," or "mog it."
- The skill is a single file: **`SKILL.md`** — YAML frontmatter + numbered procedure + hard rules + refusal cases. An agent loads it and follows it step by step. No model-specific assumptions; it works for any agent that can run shell tools, read/write files, and open images.

```yaml
---
name: mogit
description: Launch a coin on Mogz.fun (Solana memecoin launchpad). Trigger when the user types /mogit or asks to launch a token/coin.
---
```

- **Entry gate (run before any prompt):** load budget ledger and run preflight. If either fails, refuse immediately with the reason. No prompts are shown for a launch that cannot proceed.

### 1.2 Prompt sequence

Each prompt: what it asks → validation → default → bail-out. The user may type `cancel` or `quit` at any prompt to abort cleanly (no money moved, no state written except an optional `aborted` event).

| # | Prompt | Asks | Validation | Default | Bail-out |
|---|--------|------|-----------|---------|----------|
| 1 | **Name** | Coin display name | 1–32 chars; non-blank; passes banned-term check (see 1.4); not colliding with an existing Mogz coin (CONTRACT: query Mogz's lookup if it exposes one; otherwise best-effort substring check against a local list) | none | `cancel` → abort |
| 2 | **Ticker** | Symbol | 1–10 chars, uppercase A–Z + 0–9 only; must start with a letter (VERIFIED-sensible; confirm against Mogz rules, CONTRACT); not colliding with an existing ticker | uppercased first 4–6 alnum chars of name (shown for edit, never auto-accepted) | `cancel` → abort; empty + user declines default → refuse |
| 3 | **Description** | Coin description | **Simple rule enforced:** one or two plain sentences, no embellishment (no emoji, no hype, no "to the moon"), must credit the actual launcher by name/handle | template: `"<Name> is a coin launched through MogIt. Launched by <launcher>."` (editable) | `cancel` → abort |
| 4 | **Image** | Path to token image, or `none` | **Media rule enforced:** the agent must *open and visually verify* the image (see 1.4). A path/filename is never proof. Unverifiable → refuse | `none` (skip image) | `cancel` → abort; bad path → re-ask once, then refuse |

- **Validation retries:** each field gets at most **2 retries**, then the skill refuses with the specific rule that failed. This prevents infinite loops and wasted budget.
- **State:** field values are held in a single in-memory/working JSON object; nothing is written to the ledger until a confirmed launch (see 1.3).

### 1.3 The confirm card

After the four prompts, the skill renders **one** confirm card. This is the only point where money can move, and it is gated on an explicit human confirmation of **pinned, immutable terms**.

Confirm card fields (exact):

| Field | Source / meaning |
|-------|------------------|
| `coin.name` | finalized name |
| `coin.ticker` | finalized ticker |
| `coin.description` | finalized description (exactly as it will be submitted) |
| `coin.image_sha256` | SHA-256 of the verified image file, or `null` if `none` |
| `cost.sol` | exact launch cost in SOL, quoted from the working Mogz flow (CONTRACT: real-time quote, not an estimate) |
| `cost.usd` | same cost in USD at the pinned quote rate + timestamp |
| `wallet.address` | launch wallet public address |
| `wallet.balance_sol` | actual SOL balance from preflight (re-checked at confirm time — see 1.4) |
| `budget.spent_usd` | `$15.91` + cost of any confirmed launches this session (VERIFIED baseline) |
| `budget.remaining_usd` | `$14.09` − cost of this launch (VERIFIED baseline `$14.09` remaining) |
| `quote.expires_at` | quote validity deadline (INTENT: 90 s; the skill re-quotes if expired) |
| `terms_hash` | SHA-256 over the canonical serialization of all of the above (the **pinned terms**) |
| `verb` | literal `LAUNCH` — the only accepted confirm string |

**Confirm protocol:**
1. Render card. Capture `terms_hash`.
2. Wait for explicit human confirmation = the word `LAUNCH` (or an unambiguous UI button). Anything else = no action.
3. **Before executing:** re-run preflight (wallet balance) and re-verify `terms_hash` has not changed. If either fails → refuse and regenerate the card.
4. Execute through the existing working Mogz flow: **Mogz server builds the transaction → a guard inspects the bytes → the user's wallet signs → Mogz cosigns the mint.** The skill only *prepares and requests*; it never signs, and it never holds the key (see §5.4).
5. On success → commit to the budget ledger and emit `launched`. On failure → emit `failed`, do **not** decrement the ledger.

### 1.4 Preflight, media rule, and refusals

**Preflight (mandatory, runs at entry and again before confirm):**
- Check the launch wallet's **actual** SOL balance (CONTRACT: read from the working flow / wallet, not a cached number).
- `balance_sol < cost.sol` → stop. Refuse with "wallet underfunded: need X, have Y." The skill must **never** promise a launch it cannot fund.

**Media rule (mandatory):**
- Agent opens the image with its image tool and *visually confirms* the content matches the intent (is actually the token's image, not a random screenshot, not disallowed content).
- Record `image_sha256` of the *verified* bytes; store the hash, not just the path.
- Cannot open, or content is wrong/disallowed → refuse (or allow user to pick a different file, once).

**Refusal cases (skill returns without launching, and states the reason):**

1. **Budget cap hit** — `budget.remaining_usd < cost.usd`. Hard stop; cap is a lifetime ceiling (`$30` total, `$14.09` remaining as of spec date).
2. **Wallet underfunded** — `balance_sol < cost.sol` at preflight or confirm-time recheck.
3. **Unverified image** — image not opened, not visually verified, or content mismatch. Path/filename is never sufficient proof.
4. **Name/ticker collision or banned content** — collides with an existing coin, or matches the banned-terms list (CONTRACT: seed from Mogz's own rules; maintain a local supplemental list).
5. **Description violates the simple rule** — embellishment, hype, missing launcher credit, or >2 sentences.
6. **User edits terms after confirm card** — any change to a field after `terms_hash` is captured invalidates the confirm. Rebuild the card and require a fresh `LAUNCH`.
7. **Quote expired** — `now > quote.expires_at`. Re-quote, show updated cost.
8. **Launch flow unavailable** — Mogz flow errors, times out, or the guard rejects the transaction bytes. Emit `failed`; do not retry blindly.

### 1.5 Post-launch report

Emitted on `launched` (and a reduced form on `failed`):

- `tx_signature` and/or `mint` address (whatever Mogz returns — CONTRACT)
- Explorer link and coin page URL (if Mogz provides one)
- Final cost actually charged (SOL + USD)
- Updated budget: `spent_usd`, `remaining_usd`
- Timestamp, wallet address used, `image_sha256`, `terms_hash`, `event_id`
- On `failed`: error code/reason and explicit statement that **no budget was spent**.

---

## 2. The MOGIT token itself

The product's own coin is launched **through the skill**, making it the dogfood proof.

- **Name:** `MogIt` · **Ticker:** `MOGIT`. Rationale: exact product name, unambiguous, within the 1–10 uppercase-alnum ticker rule, no collision risk with the brand itself (INTENT — confirm against Mogz availability at launch time).
- **Description (follows the simple rule, credits the launcher):**
  > "MogIt is the agent skill that launches coins through /mogit. Launched by [actual launcher name/handle]."
- **Launch settings worth choosing (CONTRACT — select only if Mogz exposes them; do not assume they exist):**
  - Fixed supply (INTENT: `1,000,000,000` — a conventional memecoin choice, not a Mogz default)
  - Revoke/disable mint authority if offered (no future minting)
  - Fair launch: no presale, no team allocation
  - Burn or lock LP if offered
  - Simple description + verified image, exactly as the skill mandates
- **Dogfooding:** the MOGIT launch is the skill's **first** recorded launch. It exercises every step — prompts, image verification, preflight, confirm card, budget ledger, transaction guard, post-launch report. Its transaction becomes the canonical example in `docs/INTEGRATION.md`, and its share card (see §4.2) is the first live one. The launch **counts against the same `$14.09` budget** — no special-casing.

---

## 3. GitHub repo structure

- **Repo name:** `mogit` (if taken: `mogit-skill`). Visibility: **public**, because the deliverable is a portable skill playbook; **no secrets ever committed**.
- **License:** `MIT` for code; `SKILL.md` additionally `CC-BY-4.0` (skill text is content, not code).

```
mogit/
├── SKILL.md                        # canonical skill playbook (frontmatter + procedure + rules)
├── skills/mogit/SKILL.md           # install copy for folder-based skill loaders (duplicate, kept in sync)
├── executor/
│   ├── mogit_cli.py                # glue to the operator's working Mogz launch flow (CONTRACT interface)
│   ├── budget.py                   # durable, atomic budget ledger; enforces $30 lifetime cap
│   ├── preflight.py                # reads launch wallet's actual SOL balance
│   ├── verify_image.py             # opens/visual-verifies image, returns sha256
│   ├── events.py                   # webhook emitter: sign (HMAC), retry, idempotency
│   └── config.example.toml         # placeholders only — no real keys, no real endpoints
├── web/
│   ├── landing/index.html          # "what /mogit is + how it works" (no invented numbers)
│   ├── share/[ticker]/index.html   # share-card template per launched coin (OG meta tags)
│   └── hooks/receive               # optional webhook receiver for local testing
├── tests/
│   ├── test_budget.py              # cap enforcement, atomicity, no double-decrement
│   ├── test_preflight.py           # underfunded refusal
│   ├── test_prompts.py             # field validation, retry limits
│   ├── test_refusals.py            # all §1.4 refusal cases
│   ├── test_media.py               # image verification + hash
│   └── fixtures/                   # sample images, canned budget states
├── docs/
│   ├── SPEC.md                     # this document
│   ├── INTEGRATION.md              # Mogz launch-flow interface contract (to pin — see below)
│   └── SECURITY.md                 # key custody, webhook signing, budget integrity
├── .gitignore                      # .env, keys, ledger, config.toml (secrets) — always ignored
├── LICENSE
└── README.md
```

- **`docs/INTEGRATION.md`** is the critical contract. It must be filled from the operator's **existing working flow** (5 coins already launched) before `executor/` ships: exact entrypoints for *quote cost*, *build transaction*, *guard byte-inspection*, *wallet signature request*, *cosign mint*, and *result readback*. Until pinned, those are `CONTRACT` stubs, not fabricated endpoints.

**README contents:** what MogIt is (one paragraph); install (drop `SKILL.md` into the loader, or run `executor/`); usage (`/mogit`); prerequisites (working Mogz account/flow + funded wallet); the hard rules (budget cap `$30`/`$14.09` remaining, media rule, simple-description rule, never auto-execute); refusal cases; security notes; license; explicit no-warranty and "irreversible real-money" warning.

---

## 4. Website with "hooks"

Covers both readings honestly. No fake stats, no invented numbers, ever.

### 4.1 Webhooks (event delivery)

Events emitted to **user-configured** HTTPS endpoints (INTENT: max 5 endpoints, all `https://` only, one secret per endpoint):

| Event | Emitted when | Key payload fields |
|-------|-------------|--------------------|
| `prepared` | confirm card rendered | `coin.*`, `cost.*`, `quote.expires_at`, `terms_hash` |
| `confirmed` | explicit `LAUNCH` received + terms re-verified | `terms_hash`, `event_id` |
| `launched` | transaction succeeded | `tx_signature`, `mint`, `cost.*`, `budget.*` |
| `failed` | any failure/refusal | `error.code`, `error.reason`, `budget.*` (unchanged) |

Payload shape (canonical envelope):

```json
{
  "event": "launched",
  "event_id": "evt_<uuid>",
  "timestamp": "2026-11-01T18:42:04Z",
  "coin": {
    "name": "MogIt", "ticker": "MOGIT",
    "description_sha256": "<sha256>", "image_sha256": "<sha256|null>"
  },
  "cost": { "sol": "0.0X", "usd": "X.XX", "quote_at": "..." },
  "wallet": { "address": "..." },
  "result": { "tx_signature": "...", "mint": "..." },
  "budget": { "spent_usd": "15.91", "remaining_usd": "14.09" }
}
```

- **Signing:** HMAC-SHA256 per endpoint with its own secret; header `X-MogIt-Signature: t=<unix>,v1=<hex>` over the canonical body bytes.
- **Retry:** exponential backoff (1s → 5s → 25s), 3 attempts, then mark `dead_letter`. Idempotent via `event_id` (receivers dedupe).
- **Honesty:** events carry **only real, observed values** — real cost, real balance, real signatures. No derived "volume" or "rank."

### 4.2 Viral hooks (growth mechanics, honest)

- **Landing page:** "MogIt — launch a coin by typing /mogit." A literal 5-step visual: name → ticker → description → image → confirm card → launch. A single real "how it works" flow, no inflated claims. A "what it costs" line pointing to the live quote, not a fixed number.
- **Share cards:** one static page per launched coin at `/share/[ticker]/`, with the coin's **verified image**, name/ticker, the simple description, a copyable explorer link, and a **"Launched with MogIt"** attribution badge. Full Open Graph + Twitter Card meta tags so links unfurl correctly. The only number shown is the real per-coin link, never a "total launched" counter.
- **Attribution (referral-lite):** the `launcher` credit (already required by the simple-description rule) doubles as the attribution signal — a `?ref=<launcher>` param on share cards. No leaderboard, no invented "users," no fake referral payouts. The mechanic is: *credited authorship*, not growth hacking.

---

## 5. Risks & guardrails

| Risk | Mitigation baked into design |
|------|------------------------------|
| **Financial — irreversible spend** | Confirm card pins `terms_hash` + exact cost; explicit `LAUNCH` only; no auto-execute ever; quote expiry forces re-quote; post-launch report reconciles actual vs. quoted cost. |
| **Financial — budget overrun** | `budget.py` enforces the `$30` lifetime cap (`$14.09` remaining) as a **hard refusal** (§1.4 #1); ledger is durable and atomic (single-writer, write-then-read-verify); a launch never decrements the ledger unless `launched` succeeds. |
| **Financial — double-spend / double-submit** | Idempotency key per launch attempt; confirm requires fresh `terms_hash` match; re-preflight immediately before execute catches a wallet that drained between confirm and sign. |
| **Financial — underfunded promise** | Mandatory preflight reads **actual** SOL balance at entry *and* at confirm; refuses before promising anything (§1.4 #2). |
| **Platform ToS** | No automated/mass launching; rate limit (INTENT: max 1 launch per N minutes, configurable); banned-content check seeded from Mogz's own rules; comply with Mogz's flow exactly — the skill *prepares*, the user's wallet *signs*, Mogz *cosigns*; no circumventing Mogz limits; no claim of Mogz affiliation without permission. |
| **Abuse / spam** | Field validation + 2-retry cap; collision + banned-term checks; simple-description rule structurally limits hype/spam; every launch gated by explicit human confirm; webhook endpoints capped and rate-limited. |
| **Key custody** | The skill **never** holds, logs, or transmits the private key; it requests a signature from the user's wallet. The "guard inspects the bytes" step sits between Mogz's server and the signature — so the transaction is reviewed before signing. Secrets (webhook keys, wallet config) live in `.env`/`config.toml`, always gitignored, never committed; `SECURITY.md` documents rotation. |
| **Data / privacy** | Only hash + verified image + simple description are stored/shared; no keys, no signatures logged; webhook payloads contain no secrets by construction. |

---

**Open pin-points to close before the first real `/mogit` run (all CONTRACT, none assumed):**
1. Mogz's exact quote + build + cosign + result-readback interface (→ `docs/INTEGRATION.md`).
2. Mogz's ticker/name collision and banned-content rules (seed the checks in §1.4).
3. Mogz's available launch settings for §2 (supply, mint-revoke, LP options).
4. Confirm the current `$14.09` remaining figure against the operator's real ledger before shipping `budget.py` defaults.

---

## Muse notes — corrections to the model's assumptions (2026-11-01)

The spec above is the model's output, lightly formatted. These points correct it against the actual setup; where they conflict, this section wins:

1. **Key custody is wrong in §1.3/§5.4.** The model assumes the skill "never signs" and requests a signature from the user's wallet each launch. The real Mogz flow is an agent-custodied wallet: keys sit in the bot's vault and signing happens only through a gated execution path (`execution.sign_mogz` — the transaction bytes are guard-inspected first, the wallet signs partially, Mogz's server cosigns the mint). The confirm card is still mandatory, but it gates the vault's signing path; there is no per-launch user-wallet prompt. §5's custody row should read: keys never leave the vault, signing only via the gate, nothing signed that the guard hasn't inspected.
2. **Trigger is too broad in §1.1.** "Launch a coin/token" generally must NOT route to MogIt: the main setup is multi-venue (StonkFun/LaunchLab, Pump.fun, Mogz) and a bare launch ask needs venue choice. The skill triggers on `/mogit` and explicit Mogz mentions only.
3. **v1 launch shape (§1.3/§2).** The guarded Mogz flow today is a single-instruction create with firstBuyQuote = 0 — no bundled dev buy; the guard refuses the dev-buy bundle shape outright. Costs quoted on the confirm card are the create cost; any initial buy is a separate, separately-confirmed step, not part of the launch transaction.
4. **Description style (§1.2/§2).** The standing house style is shorter than the model's template — e.g. "Elon Coin. To Mars. Launched on Mogz." One or two plain sentences, launcher credited, no product self-description. MOGIT's own description should follow that form (final wording is Sneaks's call at launch time).
5. **Skill file layout (§3).** Muse custom skills live in exactly one place: `~/workspace/custom-skills/<name>/SKILL.md`. The repo's duplicate `skills/mogit/SKILL.md` "kept in sync" copy is unnecessary — one canonical file; the GitHub repo can mirror it at release time.
6. **Wallet reality check for the first dogfood launch.** §1.4's preflight is not theoretical: at spec time the Mogz launch wallet holds ~0.002 SOL, so a real `/mogit` run today would (correctly) refuse as underfunded until the planned ~0.05 SOL top-up lands. The MOGIT coin launch is budget-checked against the same $30 lifetime cap ($15.91 spent) — and still needs Sneaks's per-launch go, like every token launch.
