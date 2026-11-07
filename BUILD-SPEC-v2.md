# BUILD-SPEC v2 — JustMogIt
Source: DeepSeek web session "JustMogIt spec attack", Turn 4, 2026-11-06 ~21:44–22:06 ET.
Chat: https://chat.deepseek.com/a/chat/s/3d394914-dfe2-4d8e-9328-27508a1d437e
Note: Turn 4 = DeepSeek (author of v1) answering the Turn 3 hostile attack on its own spec: 25 of 26 points conceded (x402 net claim partially), 1 rebutted (confirmed-vs-finalized, documented as accepted risk). Full Turn 4 text: session-2026-11-05/turn4-deepseek-answer.md. v2 supersedes v1; §0 defaults adopted 2026-11-06 preserved.

Standalone implementation document. A builder codes from this without reading PLAN.md, EXECUTION-PLAN.md, or the research files. Where this spec disagrees with those documents, this spec wins. v2 supersedes v1; all §0 defaults adopted 2026-11-06 are preserved unchanged except where explicitly noted.

Reference values: SOL = $97.15 (2026-11-04). Pump.fun creation platform fee = 0. Pump.fun creator fee on bonding curve = 0.30% of every trade (total trade fee 1.25%). X API: owned mention read $0.001, user read $0.010, post read $0.005, plain post $0.015, post containing URL $0.200. PumpPortal rate limit ≈ 25 req/s. Solana slot ≈ 400 ms; confirmed ≈ 1–2 slots. Solana base fee = 5,000 lamports per signature.

## 0. Decision defaults (OWNER-DECISION — flip in config/policy.yaml only, never hardcoded)

STATUS: ADOPTED by Sneaks 2026-11-06 — all §0 defaults below (0.1–0.16) adopted as written, per his "do the rest" in main chat 2026-11-06 ~05:16 ET approving the §0 owner-decision defaults as proposed. Values unchanged from v1 drafting; any future change is an owner flip in config/policy.yaml, not a spec edit. No v2 fix proves a §0 default unsafe; the only §0-adjacent change is implementation of §0.3's collection rail, not the value itself.

0.1 Launches per day cap — 3 — alternatives 1, 5, 10
0.2 Per-launch hard cost cap — 0.025 SOL (creation 0.020 + priority buffer 0.005) — alt 0.03, 0.05
0.3 Upfront launch fee — 0.02 SOL non-refundable, paid by requester before RESERVED — alt 0 (free), 0.03, 0.15 refundable performance bond. v2 implementation note: collection rail in §7; the 0.02 SOL value is unchanged.
0.4 Fee split — Split A: mogger 25% / holder pool 25% / bankroll 50% — alt Split B (50/25/25), pro-rata no tiers
0.5 Trigger policy — Allowlist — alt open, allowlist+quote-only
0.6 Pair default — SOL only — alt SOL+FARTCOIN, SOL+USDC
0.7 Dev buy — 0 SOL — alt 0.05, 0.1
0.8 Reply budget per day — $0.30 — alt $0.50, $1.00
0.9 Kill-switch default — ON at boot until owner toggles OFF — alt OFF
0.10 Content approval — every public post needs per-item owner approval until ledger live — alt auto-post
0.11 X AI-reply-bot approval — REQUIRED before any live reply; Phase-0 gate — no alternative. v2: boot fails closed if approval reference absent.
0.12 Image fallback — pre-approved Pit Viper art template — alt abort, generic logo
0.13 Claim threshold — 0.02 SOL — alt 0.01, 0.05
0.14 Bankroll seed — 5 SOL — alt 3, 10
0.15 Priority fee — p75 from getRecentPrioritizationFees, capped 0.005 SOL — alt fixed 0.001, p90
0.16 "Mog me" mode — disabled until media-upload 403 fixed — alt enabled

## 1. Architecture

1.1 Two-phase invariant: no money moves, no signature is created, no CA is posted until PRE-STAGE is complete and the job is ARMED. PRE-STAGE = research, validation, dossier assembly. FIRE = signing, broadcast, reply. A job reaching FIRE without a complete Dossier, locked SpellingLock, launchable PairRegistry entry, and held Reservation is a bug.
1.2 Components: Ingress (X filtered-stream consumer + mention poll fallback; writes PingJob at RECEIVED; long-running, stateless) · DedupeGate (source_tweet_id + normalized name/ticker + image pHash; DB-backed, unique partial index) · HardGate (trigger policy, hard-skip, duplicate, cooldown, cap, bankroll; DB-backed) · DossierBuilder (metadata, image, spelling, pair simulation; worker pool, 3 concurrent) · ReservationService (atomic check→reserve→hold; single-writer, DB transaction or file lock) · Signer (one executor, one creator wallet, sequential) · Broadcaster (dual-RPC send + status poll) · ReplyQueue (idempotent X replies, credit-budgeted, 1 concurrent) · LedgerWriter (append-only) · ClaimService (daily creator-fee claim cron) · Monitor (metrics, alerts, kill switch).
1.3 State machine: RECEIVED → DEDUPED → GATED → STAGED → QUEUED → RESERVED → ARMED → SIGNED → SENT → CONFIRMED → REPLIED → LEDGERED. Branches: SPELLING_PENDING (from GATED, TTL 30m), REJECTED (from GATED/STAGED/QUEUED/RESERVED), FAILED (from SIGNED/SENT), UNKNOWN (from SENT), UNKNOWN → CONFIRMED (when old signature is confirmed/finalized), REPLY_ESCALATED (from CONFIRMED after 3 reply failures), EXPIRED (any pre-SENT state on TTL), CANCELLED (owner kill from any state; releases held reservation; releases/spends per §7).

Guards: RECEIVED→DEDUPED dedupe_key computed atomically with unique partial index on live states; no live job with same key. DEDUPED→GATED gate_results computed or rejection_code set. GATED→STAGED dossier_id + spelling_lock_id set, pair launchable, reservation_id null; ambiguous spelling → SPELLING_PENDING instead of STAGED. SPELLING_PENDING → STAGED only on valid confirmation; timeout → EXPIRED; "stop" → CANCELLED permanently. STAGED→QUEUED launches_today < cap re-checked under lock. QUEUED→RESERVED reserve(job, 0.025 SOL) succeeds AND cap re-checked under row lock. RESERVED→ARMED pair.expires_at > now, dossier verified, mint_keypair_ciphertext persisted. ARMED→SIGNED signer signs, attempt_no++; pair re-checked. SIGNED→SENT tx_signature recorded. SENT→CONFIRMED getSignatureStatuses confirmed/finalized within 30s. SENT→UNKNOWN no confirmation in 30s, reservation NOT released. UNKNOWN → CONFIRMED re-query old signature first; if confirmed/finalized, write ledger and reply without rebuild. UNKNOWN→SENT same-mint rebuild, fresh blockhash, re-sign, resend, only after old sig returns null. UNKNOWN→FAILED old sig null AND rebuilt tx fails terminally. CONFIRMED→REPLIED CA reply posted, reply_tweet_id recorded. CONFIRMED→REPLY_ESCALATED after 3 reply failures, owner alert, terminal. REPLIED→LEDGERED ledger row written, terminal. Any pre-SENT →EXPIRED on job_ttl 6h or reservation 10m. Any state →CANCELLED on kill_flag; held reservation released.

## 2. Data model

PingJob: job_id, source_tweet_id, requester_user_id, quoted_tweet_id, state, created_at, updated_at, job_ttl_at, dedupe_key, gate_results, dossier_id, spelling_lock_id, reservation_id, mint_pubkey, mint_keypair_ciphertext, attempts[], rejection_code, rejection_reply_tweet_id, kill_flag, source (x|mcp), agent_wallet, payment_tx_signature (for §0.3 rail).
Attempt: attempt_no, mint_pubkey, blockhash, last_valid_block_height, tx_signature, status (PREPARED|SIGNED|SENT|UNKNOWN|CONFIRMED|FAILED|EXPIRED), sent_at, confirm_deadline_at, rpc_endpoints[], raw_tx_b64, error.
Reservation: reservation_id, job_id, amount_sol (0.025), state (HELD|SPENT|RELEASED|EXPIRED), created_at, expires_at (created_at + 10m), ledger_entry_id.
Dossier: dossier_id, job_id, target {ca, name, ticker, description, image_url, image_sha256, image_phash, socials {twitter, telegram, website}, creator, resolution_method, confidence, candidate_count, verified}, requester {user_id, handle, pfp_url, fee_share_wallet}, spelling_lock_id, pair_id, art {pinata_cid, image_url, inspection_result, fallback_used}, staged_at, expires_at (staged_at + 6h).
SpellingLock: lock_id, job_id, source_text, source_span_start, source_span_end, raw_value, normalized_value, codepoints[], char_count, ticker_upper, ticker_char_count, confirmed_by (onchain|exact_quote|requester_explicit), diff[], homoglyph_check (pass|fail), validation {name_length_ok, ticker_length_ok, control_chars, mixed_scripts}.
PairRegistry: pair_id, venue, quote_mint, quote_symbol, launchable, last_probe_at, probe_method (simulateTransaction), probe_result {ok, compute_units, error}, expires_at (last_probe_at + 60s), authority (simulation).
Ledger: ledger_id, job_id, requester_user_id, source_tweet_id, name, ticker, mint_pubkey, create_signature, cost_sol, reservation_id, reply_tweet_id, fee_accrued_sol, fee_claimed_sol, accrued_mogger_liability_sol, created_at.
DedupeIndex: unique partial index on dedupe_key WHERE state IN live states (RECEIVED…REPLIED); INSERT … ON CONFLICT DO NOTHING returns existing job.

## 3. Metadata pipeline

Resolution order (CA-first; never pump.fun search first):
Step 1 — CA in trigger/quoted post: base58 32–44 chars parsing as Solana pubkey → DAS getAsset must return a mint with metadata AND pump.fun frontend-api-v3.pump.fun/coins/{mint} must return a coin record before confidence 1.0. If only a pubkey parses, fall through.
Step 2 — no CA, quoted post names coin: exact name/ticker spans; on-chain search requiring ALL of exact normalized name, exact normalized ticker, image pHash distance ≤6 vs source image, creator handle match if available. Name/ticker search uses Birdeye search endpoint (/defi/token_metadata by name/symbol) or Helius DAS search; Solana RPC has no name search. >1 candidate passes → do not launch; reply once with top 3 CAs, ask which.
Step 3 — no CA, no name: OCR quoted image (Gemini Vision), confidence ≥0.9 + readable ticker; multiple candidates → §6 protocol.
API calls (all timeout 5s): trigger post GET /2/tweets/{id} with expansions (post read $0.005, user read $0.010) · Helius DAS getAsset (RPC credit) · pump.fun GET https://frontend-api-v3.pump.fun/coins/{mint} (free; boot health probe mandatory; legacy frontend-api.pump.fun removed) · Birdeye GET /defi/token_metadata (API key tier) and Birdeye search by name/symbol for Step 2 · image fetch (max 10 MB, free) · pHash local 5ms.
Confidence: CA + DAS asset + pump.fun coin record = 1.0 launchable; exact quote + four checks = 0.95 launchable; OCR ≥0.9 = 0.85 requires spelling confirmation; <0.85 reject with reply; metadata malformed → quoted-post text fallback, 0.80, requires spelling confirmation.

## 4. Image pipeline

Source order: (1) target coin's on-chain metadata image URI; (2) quoted post's attached image; (3) requester X pfp ONLY for "mog me", never a coin launch unless requester explicitly says "use my pfp".
Normalization: fetch 5s timeout, 10 MB max; accept PNG/JPG/WebP; reject GIF/SVG/animated; convert PNG, center-crop square, resize 1000×1000, strip EXIF; re-encode hard limit 5 MB; Pinata POST https://uploads.pinata.cloud/v3/files network=public maxFileSize=5_000_000; image = Pinata dedicated gateway URL preferred; https://ipfs.io/ipfs/{cid} fallback only. Store image_sha256, image_phash, pinata_cid.
Inspection gate (mandatory, machine-only): Gemini Vision strict JSON {is_real_person, is_brand_logo, nsfw_score, matches_target_phash, phash_distance, dominant_colors, text_in_image, confidence}. Rejects: nsfw_score > 0.20; is_real_person without explicit consent; is_brand_logo without written permission; phash_distance > 6 only when fallback_used = false; confidence < 0.85. Fallback template (fallback_used = true) is exempt from the pHash-distance check only; all other checks apply.
Fallback ladder: verified target image → Pit Viper template with metadata disclosure "Art generated by JustMogIt; target logo could not be verified." → abort with public reply (never blank/placeholder). Store inspection_result, fallback_used in Dossier.art.

## 5. Pair registry

Probe: at boot and every 60s per entry — build create instruction with throwaway mint + requested quote mint; simulateTransaction sigVerify=false, replaceRecentBlockhash=true. OK → launchable, record compute_units, expires_at = now+60s. Fail → launchable=false, error recorded, removed from fireable set.
Conflict rule: simulation wins; config-pointer disagreement → log PAIR_PROBE_CONFLICT, launchable=false, never fire on conflict.
Fire-time: RESERVED requires launchable && expires_at > now; ARMED→SIGNED re-checks pair.expires_at > now; stale at either transition → one 300ms simulation; fail → REJECTED_PAIR + public reply ("That pair isn't launchable right now; I can fire it on SOL instead — say SOL."). Never silently switch pairs; pre-approved fallback is a Dossier field.

## 6. Spelling lock

Span extraction with offsets (auditable; paraphrase-only extractors banned). Normalization: Unicode NFKC, strip zero-width, collapse whitespace, trim; never "fix" spelling; never capitalize ticker unless source does. Validation: ticker 1–10 chars, name 1–32; mixed-script (homoglyph) → reject + explicit confirmation required; control chars → reject.
confirmed_by — ONLY these allow FIRE: onchain (source CA metadata matches exactly) · exact_quote (quoted text contains exact name+ticker, span diff == []) · requester_explicit (YES to structured candidate, or exact spelling sent). confirmed_by null OR diff != [] → cannot enter RESERVED. No guess-and-fix path.
Ambiguity protocol: GATED → SPELLING_PENDING — one structured reply ('I read: name "Them Greens", ticker "GREENS". Reply YES, or send the exact name and ticker.'), TTL 30 min, no launch until confirmed; "stop" aborts immediately and permanently → CANCELLED. Phase B — window opens only with a valid lock.

## 7. Burst protocol

Admission order (all before any dossier prep): ingress → PingJob RECEIVED; dedupe by source_tweet_id then dedupe_key via atomic INSERT … ON CONFLICT; hard gates in order trigger policy → hard-skip → duplicate/copycat → cooldown → cap (row lock) → bankroll → §0.3 payment verification. Pass-all → STAGED. Reservation under single DB tx/file lock; signing serialized one executor; broadcast + reply queue.
§0.3 payment rail: requester must send 0.02 SOL to the job-derived address with memo = job_id; a PAYMENT_PENDING check verifies the transfer before RESERVED. If no payment is received within the TTL, REJECTED_PAYMENT. The bankroll no longer silently fronts the fee.
Cap: DB row with daily counter, checked at admission AND re-checked QUEUED→RESERVED under row lock; launches_today >= 3 → REJECTED_CAP.
Reservation: one transaction — INSERT Reservation (0.025 SOL, +10m expiry) + decrement available_balance + set job RESERVED; available < 0.025 → REJECTED_BANKROLL; release on REJECTED/FAILED/EXPIRED/CANCELLED; spend on CONFIRMED; UNKNOWN never releases.
Reply budget: CAP_REPLY_BUDGET $0.30/day covers all replies including launch replies and cap-reject replies; 20 replies × $0.015. Pinned "at capacity today" post + EOD digest for the rest. Reply idempotency key job_id + ":reply:1"; retry +5s/+30s; after 3 failures → REPLY_ESCALATED + owner alert.
50-ping walkthrough (corrected arithmetic): 50 mentions in 10s → 50 RECEIVED rows (reads $0.05, deduped per UTC day) → dedupe collapses 10 → 40 unique (10 people quoting same target = 1 launch, 10 requester records) → hard-skip −5, duplicate −3, cooldown −2 → 30 → cap admits first 3 by timestamp, 27 REJECTED_CAP → reply cost: 20 replies × $0.015 = $0.30 total, including the 3 launch CA replies and cap-reject replies; EOD digest $0.015 if budget remains; burst total ≈ $0.315–$0.45 depending on read/payment mix. Dossier prep for 3 in parallel 3–8s → reservations 3 × 0.025 = 0.075 SOL held; with 0.05 available only 2 reserved → signing 3 × ~80ms → broadcast skipPreflight after simulation, maxRetries 0, rebroadcast every 2s to 2 RPCs + raw-send, poll getSignatureStatuses every 400ms → confirmation P50 400ms / P99 2s → CA reply per launch → ledger row per launch and per rejection → kill switch mid-burst: QUEUED/RESERVED → CANCELLED, held reservation released; fee claiming unaffected.

## 8. Transaction outbox

Send policy: simulate first; send skipPreflight, maxRetries 0; manual rebroadcast every 2s to two RPCs + raw-send until blockhash expiry (~30–90s); poll getSignatureStatuses every 400ms; reply on confirmed, not finalized. Accepted risk: confirmed-but-rolled-back launch would leave a CA reply for a reverted mint; this is a deliberate latency tradeoff, documented, not changed.
Mint persistence: mint keypair generated at STAGED, encrypted, stored as mint_keypair_ciphertext before ARMED; crash-safe same-mint rebuild; never a new mint on retry.
UNKNOWN reconciliation: SENT unconfirmed 30s → UNKNOWN; never release reservation, never new mint; re-query old signature first; if old sig confirmed/finalized → UNKNOWN → CONFIRMED, write ledger, post CA reply, no rebuild. If old sig null and blockhash expired → rebuild same create tx, same mint keypair, fresh blockhash, re-sign, resend, attempt_no++; reservation moves only when old sig returns null AND rebuilt tx confirms/fails terminally.
Funding-send retry: dual-RPC send skipPreflight false; no confirm in 10s → fresh-blockhash rebuild + resend; still unknown → no third send until getSignatureStatuses null for both signatures; log both signatures.
PumpPortal 429: backoff 1s/2s/4s, max 3 retries; only reserved jobs are prepped (no 50-job retry storm). Blockhash expired pre-send: rebuild same mint, attempt_no++.

## 9. Money

Per-launch cost: creation platform fee 0; on-chain creation 0.020–0.025 SOL; priority 0.0002–0.005 (p75, cap 0.005); base fee 0.00001 SOL for two signatures (5,000 lamports × 2); X API $0.031 = 0.000319 SOL; total hard cost ≈ 0.0203 SOL ≈ $1.97. PumpPortal creation 0; dev buy 0.5% Local / 1% Lightning (dev buy default 0).
Fee split (Split A, §0.4): creator fee 0.30% of every trade; mogger 25% / holder pool 25% / bankroll 50%; bankroll revenue 0.0015 × V; break-even V* 13.5 SOL (A) / 27.1 SOL (B); buffered (0.0228 all-in) 15.2 / 30.4; round 14 / 28. P&L Split A: V=5 → net −0.0128 SOL (−$1.24); V=14 → +0.0007; V=30 → +0.0247 (+$2.40); V=85 → +0.1072 (+$10.41). Split B halves bankroll fees.
Claims: collectCreatorFee claims all, no minimum, anytime; claim tx ≈ 0.00001–0.001 SOL; keep 0.01 SOL gas buffer in creator wallet (bankroll-funded); claim only when claimable > max(0.02 SOL, 20 × claim tx cost) — at 0.0052 SOL claiming costs 26%, wait; daily check, weekly min sweep > 0.02 SOL. SharingConfig: each recipient claims own bucket; bot cannot force-claim the mogger's slice. Tiered payouts = claim-and-forward (bot claims bankroll bucket, snapshots mogger holdings, forwards cut) — a liability; ledger tracks accrued_mogger_liability per coin. Holder Rewards mode is mutually exclusive with splits; distributor undesigned (§12 OPEN).
X credits: per launch $0.031; URL post $0.200 (plain-text CA is a hard cost rule); 3/day launches $0.093 + 2 content posts $0.030 ≈ $0.123/day; $2.83 ≈ 23 days; burst rejection replies can cut it to ~17 days; CAP_REPLY_BUDGET protects runway. Burst cost corrected in §7: $0.315–$0.45, not $0.315.
Bankroll: one burn rate — 0.0203 SOL/launch → 1 SOL = 49 launches → 5 SOL ≈ 245 launches ≈ 82 days at 3/day unrecycled. 10 SOL ≈ 164 days. Dev buy 0 → 5 SOL ≈ 245 launches. Recommended seed 3–5 SOL + 0.05 SOL claim gas buffer. A 0.1 SOL dev buy changes the burn rate and must update every table; it is not the default.
DexScreener: $299 Enhanced Token Info needs ≈ $199,333 volume ≈ 2,052 SOL at 0.30% × 50% share (0.15%); post-graduation only, never from shared bankroll.
x402: CDP facilitator first 1,000 tx/mo free then $0.001/tx; price mog_launch ≈ $2.50–$3.00/call; 100/mo gross ≈ $250–$300; net cost stack undesigned — do not claim net figures until the stack is specified; not a primary revenue line.

## 10. Latency budget

Warm path: X filtered stream P50 4,000ms / P99 5,000ms (mention poll fallback 60s, only when stream is down; 2s polling removed — it sat at the 450/15min per-app limit) · parse+dedupe+gates 30/80ms · dossier lookup 5/20ms · pair simulation 200/400ms · build+sign 50/150ms · broadcast→confirmed 400/2,000ms · CA reply 200/500ms · total ≈ 4.9s P50, 8.2s P99. Target ≤30s P99 warm. Cold path: + dossier resolution 3–5s + IPFS 1–3s, ≤60s P99.
NEVER in window: pair feasibility research; image generation from scratch; human clarification loops; SOL top-up; mogz config-pointer reads; LLM planning >1s; 5 MB IPFS upload — except the verified target image or quoted image under 5 MB; all fallback and template assets are pre-uploaded before launch day.

## 11. Load test — 50-ping burst

Setup: mainnet only (pump.fun is not a devnet program) with 0.10 SOL test bankroll (≥3 × 0.025 reservation + buffer), cap 3, 50 synthetic mentions (10 duplicates, 5 hard-skip, 3 duplicate coins, 2 cooldown violations, 30 valid), X test balance $2.83, CAP_REPLY_BUDGET $0.30.
Steps: inject 50 mentions in 10s via stream simulator; measure rows created, dedupe collapse, gate counts; measure time to first RESERVED / CONFIRMED / CA reply; measure X spend and credit balance; kill-switch mid-burst test (QUEUED/RESERVED → CANCELLED, claiming still works); UNKNOWN test (force RPC drop 35s → UNKNOWN, old-sig re-query, same-mint rebuild only if old sig null, no duplicate mint); duplicate-mint test (same CA twice → second REJECTED_DUPLICATE with existing CA).
PASS: exactly ≤3 launches and ≥1 confirmed launch; 0 duplicate mints; 0 duplicate CA replies; 0 launches without SpellingLock; 0 launches on stale pair; reservations held for all UNKNOWN jobs; burst X spend ≤ $0.50; P99 ping→CA ≤ 30s warm. FAIL: any duplicate mint, unlocked-spelling launch, reservation released on UNKNOWN, cap breach, X spend > $0.50, P99 > 30s, job stuck in SENT > 90s without UNKNOWN transition.

## 12. Failure table

- TEST 2 silent create → §8 UNKNOWN reconciliation + §1.3 UNKNOWN → CONFIRMED — CLOSED
- TEST 3 → not a defect — definitional only. Test 3/Bingus completed its full cycle (launch, posts, exit on command); there was no failure to close. The OPEN marker in v1 confused builders; v2 annotates it as definitional.
- TEST 4 name guess → §6 + §1.1 — CLOSED
- TEST 4 pair research in window → §5 + §10 — CLOSED
- TEST 4 research in window generally → §1.1 + §10 — CLOSED
- Dropped 0.7 SOL return → §8 funding-send retry — CLOSED
- X AI-reply-bot approval not submitted → §0.11 — OPEN, boot-blocking Phase-0 gate; boot fails closed without approval reference
- Bot app authorization / media 403 → §0.16 + boot auth check — OPEN (owner authorizes app to post as @justmogit_bot)
- Fee-claim dust → §9 claims — CLOSED (bankroll-funded buffer; 26% threshold corrected)
- Holder Rewards vs splits → §9 note — OPEN (distributor undesigned; one mode per coin)
- "1 SOL ≈ 5 launches" contradiction → §9 single burn rate 0.0203 SOL/launch, 5 SOL ≈ 82 days at 3/day — CLOSED (owner confirms dev buy = 0 per §0.7 or updates the table)
- Dex $299 as routine milestone → §9 corrected to $199,333 / 2,052 SOL — CLOSED (policy)
- Merch in execution plan → removed, no merch component — CLOSED (policy: kill until Phase 3 ledger live)
- Governance contradiction → §0.10 — CLOSED (policy, owner flips in one line)
- X credits contradiction (DEPLETED vs $2.83) → §9 + boot balance check — OPEN (owner confirms current credit balance before Phase 0)
- Legacy pump.fun endpoint → §3 frontend-api-v3 + boot health probe — CLOSED
- UNKNOWN confirmed-but-stranded → §1.3 UNKNOWN → CONFIRMED + §8 old-sig re-query — CLOSED
- Spelling ambiguity dead-end → §1.3 SPELLING_PENDING + §6 — CLOSED
- Fallback template rejected by inspection → §4 fallback_used pHash exemption — CLOSED
- Dedupe race → §2 unique partial index + §7 atomic insert — CLOSED
- Pair expiry race ARMED→SIGNED → §5 re-check — CLOSED
- Upfront fee uncollected → §7 payment rail — CLOSED
- Reply failure stranded → §1.3 REPLY_ESCALATED — CLOSED
- Base fee understated → §9 two signatures — CLOSED
