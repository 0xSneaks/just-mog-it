> **SUPERSEDED (2026-11-07).** v0 document, archived unchanged below this header. Current: [BUILD-SPEC-v2.md](../../../BUILD-SPEC-v2.md) and [BOT.md](../../../BOT.md); front door: [README.md](../../../README.md).

<p align="center"><img src="assets/mog-cat-hero.jpg" width="480" alt="JustMogIt — the mog cat"></p>

# JustMogIt

**Just Mog It.**

MogIt is an agent skill: type `/mogit` and your agent walks a gated flow that ends — only if you confirm the exact terms — with a coin launched on [Mogz.fun](https://mogz.fun), a Solana memecoin launchpad. The skill is a playbook ([`SKILL.md`](SKILL.md)) any agent with shell tools, file access, and image viewing can follow. It prepares and requests; a guard inspects the transaction; the launch wallet signs through a gated vault path; Mogz cosigns the mint.

No auto-execute. No hype. Every launch is confirmed by a human, on pinned terms, against a hard lifetime budget.

## X profile-picture bot v1.0.0

The PFP bot reads mentions, checks that the author is verified and follows more
than 60 accounts, and replies to clear mog requests with a Pit Viper edit of the
author's profile picture. Gemini 2.5 Flash classifies the request and locates
the glasses. The last processed mention is stored in `tmp/since_id.txt`.

### API access and compliance

- **X API access.** Under the legacy tier model, budget for Basic+ (about
  $100/month). The legacy Free tier did not cover the complete mention polling
  and media upload workflow. X now documents
  [pay-per-use API credits](https://docs.x.com/x-api/getting-started/pricing).
  Verify your app's access to mention polling, v1.1 media upload, and post
  creation. Enable Read and Write for the OAuth 1.0a user credentials.
- **Automation disclosure.** Enable the
  [Automated account label](https://help.x.com/en/using-x/automated-account-labels)
  and disclose in the bio that the bot edits and replies to profile-picture
  requests. Get X's prior written approval for AI replies, publish an opt-out
  route, and honor opt-outs before running the listener.
  [X automation rules](https://help.x.com/en/rules-and-policies/x-automation)
- **Gemini access.** Use an API key with access to
  [Gemini 2.5 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash).

### Art assets

Commit these transparent PNGs manually to `assets/`.

- `pit_vipers_frame.png`
- `pit_vipers_left_lens.png`
- `pit_vipers_right_lens.png`
- `high_fidelity_scanlines.png`
- `get_mogged_stamp.png`

The frame and two lens PNGs must use **identical canvas dimensions**, with each
layer aligned in its final position on that canvas. The scanlines are masked
to the glasses. The stamp occupies 30% of the profile picture width.

### Setup

From the repository root, in a shell with Python 3.10 or newer:

```bash
python -m pip install -r requirements.txt
cp .env.example .env
# Fill in all six variables in .env.
set -a
. ./.env
set +a
python -m pytest
python -m mogit.bot
```

The tests create their own image fixtures and run without production art or API
credentials. Run one listener per cursor file. If a reply request fails with an
unknown outcome, check the original thread before restarting the listener.

## How /mogit works

1. **Preflight, before any prompt.** The agent loads the budget ledger and reads the launch wallet's *actual* SOL balance and a real-time cost quote. If the wallet can't fund the launch or the budget can't cover it, the skill refuses on the spot — no prompts for a launch that cannot proceed.
2. **Four prompts.** Name (1–32 chars, no collisions, no banned terms) → ticker (1–10 chars, uppercase alphanumerics, starts with a letter) → description → image. Type `cancel` or `quit` at any prompt to abort cleanly. Each field allows at most 2 retries.
   - **Simple-description rule:** one or two plain sentences. No emoji, no hype, no "to the moon." The description must credit the actual launcher. House style is short — e.g. `Elon Coin. To Mars. Launched on Mogz.`
   - **Media rule:** any image is *opened and visually verified* by the agent before use, and the SHA-256 of the verified bytes is recorded. A file path is never proof of content. No image (`none`) is a valid choice.
3. **The confirm card.** One card pins every term: name, ticker, exact description, image hash, exact cost in SOL and USD, wallet address and balance, budget spent/remaining, and a 90-second quote expiry — all sealed under a `terms_hash` (SHA-256 of the canonical terms). Editing anything after the card voids the confirmation.
4. **`LAUNCH`.** The literal word `LAUNCH` is the only confirmation. Before executing, the agent re-runs preflight and re-verifies the terms hash and quote. Mogz builds the create transaction (single instruction, no bundled dev buy), the guard inspects the bytes, the wallet signs through the vault gate, Mogz cosigns.
5. **Report.** Signature/mint, explorer and coin-page links, the final cost actually charged, and the updated budget. On failure: the reason, and an explicit statement that no budget was spent.

The skill refuses — with the reason stated — on: budget cap hit, underfunded wallet, unverified image, name/ticker collision or banned content, a description that breaks the simple rule, terms edited after the confirm card, an expired quote, or an unavailable/rejecting launch flow. Full rules in [`SKILL.md`](SKILL.md); full design in [`SPEC.md`](SPEC.md).

## Budget rules

- **$30 USD lifetime cap** on all Mogz spend through MogIt. Not per-coin, not per-day — lifetime.
- Baseline at spec date (2026-11-01): **$15.91 spent, $14.09 remaining**.
- The ledger is durable and atomic, and it is a *hard* refusal: if the remaining budget is below a launch's quoted cost, the skill stops. A launch decrements the ledger **only** when it succeeds — failures spend nothing.
- The product's own coin, **MOGIT**, is the skill's first launch (dogfood) and counts against the same budget. No special-casing.

## Hooks

"Hooks" means two things here, and both are built on the same rule: **only real, observed values — no invented numbers, ever.**

### 1. Webhooks — signed launch events

MogIt emits events to user-configured HTTPS endpoints (max 5, HTTPS only, one secret per endpoint):

| Event | Emitted when |
|---|---|
| `prepared` | Confirm card rendered |
| `confirmed` | `LAUNCH` received and terms re-verified |
| `launched` | Transaction succeeded |
| `failed` | Any failure or refusal |

Payloads use a canonical envelope (`event`, `event_id`, `timestamp`, `coin`, `cost`, `wallet`, `result`, `budget`) — see `SPEC.md` §4.1 for the exact shape. Delivery details:

- **Signing:** HMAC-SHA256 with the endpoint's own secret, sent as `X-MogIt-Signature: t=<unix>,v1=<hex>` over the canonical body bytes.
- **Retry:** exponential backoff (1s → 5s → 25s), 3 attempts, then `dead_letter`.
- **Idempotency:** every event carries a unique `event_id`; receivers dedupe on it.
- Payloads contain no secrets and no keys — by construction.

### 2. Share pages — one per launched coin

Each launched coin gets a static share page at `/share/[ticker]/` with:

- the coin's **verified image**, name, and ticker,
- the simple description, exactly as launched,
- a copyable explorer link,
- a **"Launched with MogIt"** attribution badge,
- full Open Graph + Twitter Card meta tags so links unfurl correctly.

The launcher credit in the description doubles as attribution (share cards can carry `?ref=<launcher>`). There is no "total launched" counter, no leaderboard, no invented stats — the only number on a share page is that coin's own real link.

## Repository layout (this scaffold)

```
SKILL.md          # the canonical /mogit playbook
SPEC.md           # the full product spec (design source of truth)
packs/            # staged launch packs awaiting funding/confirmation
README.md
LICENSE           # MIT
```

The full target layout — executor glue, tests, website, and docs — is specified in `SPEC.md` §3 and is built out from this scaffold. The Mogz launch-flow interface (`docs/INTEGRATION.md` in the spec) is pinned from the operator's existing working flow before the executor ships; nothing about Mogz's API is guessed or fabricated here.

## Staged packs

`packs/` holds launch packs staged from real narratives, each awaiting the same two things: a funded launch wallet and a `LAUNCH`:

- `packs/tremp.json` — TREMP (Doland Tremp), from a live FOMO position screenshot.
- `packs/super-agent.json` — SA (Super-Agent), from the DeepSeek "Super-Agent" news narrative.

## Warning

Launching a coin spends real money and is **irreversible**. MogIt is provided as-is, with no warranty of any kind. You are responsible for every launch you confirm.

## License

Code: MIT (see [`LICENSE`](LICENSE)). `SKILL.md` text is additionally licensed CC-BY-4.0.
