# Turn 3 — Hostile review of BUILD-SPEC v1 (DeepSeek, self-attack)
Source: live DeepSeek web session "JustMogIt spec attack", Turn 3, 2026-11-06 ~21:44–22:06 ET.
Chat: https://chat.deepseek.com/a/chat/s/3d394914-dfe2-4d8e-9328-27508a1d437e
Prompt: the attack.py Turn 3 prompt verbatim (hostile reviewer framing + BUILD-SPEC v1 full text). Verdict: FIX-FIRST.

---

Hostile review, BUILD-SPEC v1. I read it as a launch-day artifact, not a design essay. The core two-phase invariant is better than most of what crosses my desk, but v1 has a dead endpoint, a test that cannot exercise the money path, a state-machine hole that strands confirmed launches, and enough arithmetic errors to misstate runway by 15×. Details below.

P0 — launch-day blockers

1. §3 — pump.fun metadata endpoint is dead.
The spec calls https://frontend-api.pump.fun/coins/{mint} "free" and makes it the CA-first metadata source. That legacy endpoint is retired; pump.fun killed frontend-api (HTTP 530) and the current base is frontend-api-v3.pump.fun. On launch day, every CA-first job either fails resolution or silently falls back to a worse path.
Fix: use frontend-api-v3.pump.fun/coins/{mint} with a health probe at boot, and make DAS/Birdeye the fallback, not the other way around.

2. §11 — the load test cannot launch anything.
Setup says "0.01 SOL test bankroll." §7 reservations are 0.025 SOL each; §0.2 hard cap is 0.025. 0.01 < 0.025, so the test can never reach RESERVED, never signs, never confirms, never replies. The PASS criterion "exactly ≤3 launches" is technically satisfiable by zero launches, which validates nothing about the launch path.
Fix: test bankroll must be ≥ 0.10 SOL (3 × 0.025 + buffer), and the PASS criterion must require ≥1 confirmed launch, not "≤3."

3. §0.11 / §12 — X AI-reply-bot approval and bot posting authorization are OPEN.
The spec correctly says prior written approval is required for AI reply bots, and that the bot app must be authorized to post as @justmogit_bot. Both are marked OPEN. X's automation rules require prior written approval even for human-reviewed AI replies.
Fix: treat both as hard Phase-0 gates, not "owner action" footnotes. Boot must fail closed if the approval reference and posting authorization are absent.

P1 — will corrupt money, state, or the budget

4. §1.3 / §8 — missing UNKNOWN → CONFIRMED transition.
SENT → CONFIRMED only fires "within 30s." If the tx confirms at 31s, the job goes UNKNOWN. The only exits from UNKNOWN are →SENT (rebuild) and →FAILED. There is no UNKNOWN → CONFIRMED. The rebuild can then fail terminally (mint already exists) and the job is marked FAILED while the original launch actually landed — money spent, no ledger row, no CA reply. This is the worst race in the spec.
Fix: before any rebuild, re-query the old signature; if it is confirmed/finalized, transition UNKNOWN → CONFIRMED and never rebuild. Add the transition to §1.3 and the reconciliation procedure to §8.

5. §7 / §11 — burst X spend exceeds the test's own PASS threshold.
The walkthrough claims "20 cap replies $0.30 + 1 EOD digest $0.015 + launch replies, burst total ≈ $0.315." Launch replies are not free: 3 × $0.015 = $0.045, and the per-launch reads add ~$0.048 more. Real burst spend is roughly $0.408–$0.458, not $0.315. The PASS criterion is ≤$0.35. The walkthrough fails its own test.
Fix: either raise the PASS ceiling to ≥$0.50 and document it, or make CAP_REPLY_BUDGET cover all replies including launch replies (20 total, not 20 cap-replies plus 3 launch replies). Do not ship a budget that cannot fund its own success case.

6. §10 / §13 — 2-second mention polling is at the rate limit and economically fatal.
X's /2/users/:id/mentions limit is 450 requests/15 min per app; 2-second polling is 450/15 min exactly, leaving zero headroom for retries or other calls. If the filtered stream drops for a day, 43,200 polls at $0.001/resource is $43.20/day against a $2.83 pot. Even if empty polls are free, the rate limit guarantees 429s the moment anything else touches the endpoint.
Fix: poll no faster than 60s (X's own tooling defaults to 60–240s), only when the stream is down, and budget the polls explicitly. The stream is the primary path; polling is a cold failover, not a 2-second loop.

7. §9 — 5 SOL runway is off by ~15×.
The spec correctly computes 0.0203 SOL/launch → 1 SOL = 49 launches → 5 SOL ≈ 245 launches. At 3/day, 245 launches is ~82 days. The spec then says "5 SOL ≈ 5.5 days, 10 SOL ≈ 11 days unrecycled." Those figures imply ~0.3 SOL/launch, which contradicts the stated 0.0203. The "1 SOL ≈ 5 launches" inconsistency is acknowledged, but the runway numbers still use the wrong burn rate.
Fix: publish one burn rate. If dev buy = 0, 5 SOL ≈ 82 days at 3/day. If you intend 0.3 SOL/launch (0.1 dev buy + buffer), say so and update every table.

8. §9 — DexScreener volume requirement is 2× too low.
"$299 needs ≈ $99,667 volume ≈ 1,026 SOL at 0.30% × 50% share." Bankroll share is 0.30% × 50% = 0.15%, not 0.30%. $299 / 0.0015 = $199,333, which at $97.15/SOL is ~2,052 SOL, not 1,026. The error makes the milestone look twice as achievable.
Fix: use 0.0015 in the denominator; update both the dollar and SOL figures.

9. §0.3 — the 0.02 SOL upfront requester fee has no collection mechanism.
The spec says it is "paid by requester before RESERVED," but no component collects it. X has no native SOL payment; there is no invoice, escrow, or on-chain transfer step in the state machine. In practice the bankroll funds the 0.025 reservation and the requester never pays, so the economic model is wrong and the "non-refundable" term is unenforceable.
Fix: either remove the upfront fee and model the bankroll cost honestly, or specify a payment rail (e.g., requester sends 0.02 SOL to a derived address with the job ID as memo, verified before RESERVED) and add a PAYMENT_PENDING state.

10. §6 — the spelling ambiguity protocol has no state.
Phase A says "one structured reply … TTL 30 min, no launch until confirmed." But §1.3 requires GATED → STAGED only when spelling_lock_id is set, and confirmed_by null cannot enter RESERVED. An ambiguous job cannot legally sit in GATED waiting for a reply; there is no AWAITING_SPELLING state, so the job either expires at GATED or is rejected.
Fix: add SPELLING_PENDING between GATED and STAGED with a 30-minute TTL; transition to STAGED on valid confirmation, EXPIRED on timeout, CANCELLED on "stop."

11. §4 — the fallback template fails the mandatory inspection gate.
The inspection gate rejects phash_distance > 6 for all images. The Pit Viper fallback is a generic template, not the target image; its pHash distance vs the target will almost always exceed 6. The fallback ladder therefore cannot execute: the template is rejected by the very gate that is supposed to approve it.
Fix: exempt fallback_used = true from the phash_distance check (keep NSFW/person/logo checks), or compute pHash only when the image claims to be the target.

12. §3 Step 1 — a 32–44 char base58 string is not proof of a pump.fun coin.
Confidence 1.0 is assigned as soon as the string parses as a Solana pubkey. A wallet address, a random program ID, or a non-pump mint all parse. The spec then calls "on-chain metadata" without requiring a DAS asset or a pump.fun coin record.
Fix: require DAS getAsset to return a mint with metadata and the pump.fun coin endpoint to return a coin before assigning 1.0; otherwise fall through to Step 2 or reject.

13. §7 — dedupe is check-then-insert, not atomic.
The guard says "dedupe_key computed, no live job with same key," but two simultaneous mentions with the same key can both read "none" and both insert. DB-backed does not mean race-free.
Fix: unique partial index on dedupe_key for live states; INSERT … ON CONFLICT and treat conflict as DEDUPED.

14. §11 — "devnet or mainnet" is false equivalence.
pump.fun is a mainnet program. Devnet has no pump.fun bonding curve or create instruction. A devnet "pass" validates nothing about the venue.
Fix: mainnet-only, with a funded throwaway wallet and explicit kill-switch authority.

P2 — real but not launch-stopping

15. §1.3 / §7 — kill-switch naming and release rule disagree. §1.3 says CANCELLED from any state; §7 says REJECTED_KILL. Pick one and define reservation release for it. Currently CANCELLED has no release rule.

16. §1.3 / §7 — cap re-check location disagrees. §1.3 checks at STAGED → QUEUED; §7 says "checked at admission AND re-checked QUEUED → RESERVED under lock." Two different gates. Fix the state machine to match the row-lock design.

17. §8 — reply failure after CONFIRMED has no terminal state. CONFIRMED → REPLIED is the only path; if X rejects the reply permanently, the job sits forever. Add a reply TTL and ESCALATED/FAILED with owner alert.

18. §5 — pair expiry can race between ARMED and SIGNED. The pair is checked at RESERVED → ARMED, but expires 60s after probe and is not re-checked at ARMED → SIGNED. A slow signer queue can sign on an expired pair. Re-check at both transitions, or pin pair validity to the reservation.

19. §9 — base fee is per signature, not per tx. 0.000005 SOL is the base fee per signature. A pump.fun create tx typically has creator + mint signatures; use 0.00001 or compute dynamically.

20. §9 — "at 0.0052 SOL claiming costs up to 20%" is 26%. 0.0052 / 0.02 = 26%. Minor, but the threshold logic is supposed to protect the bankroll.

21. §9 — x402 "100/mo ≈ $50–100 net" does not follow from $2.50–$3.00/call. 100 × $2.50 = $250 gross. If net is $50–$100, state the cost stack; otherwise remove the net claim.

22. §3 — on-chain search by exact name/ticker is not a Solana RPC method. It requires an indexer (Birdeye search, Helius DAS search). The spec lists Birdeye by address only. Name the search provider.

23. §4 — ipfs.io public gateway is a poor production host. It is slow and rate-limited; pump.fun consumers may time out. Use a Pinata dedicated gateway or a CDN-backed gateway.

24. §10 — "NEVER in window: 5MB IPFS upload" contradicts §4. If the source is a quoted image or pfp, §4 requires an upload in the dossier stage, which is inside the ping→reply window. Either pre-upload all fallback assets and restrict in-window uploads, or remove the blanket ban.

25. §12 — TEST 3 "OPEN" will confuse a builder. The context is right: Test 3/Bingus completed its cycle; the OPEN marker is definitional. The spec should say "not a defect — definitional only" in the table, not leave a builder wondering what broke.

26. §9 — "reply on confirmed, not finalized" is an acceptable latency tradeoff. Confirmed rollback is possible only under extreme network restart, so this is defensible; just document that a confirmed-but-rolled-back launch would leave a CA reply for a reverted mint.

Sections that are sound

§1.1 two-phase invariant — sound; the best part of the spec.

§2 data model — sound as a field inventory, but missing the SPELLING_PENDING and UNKNOWN→CONFIRMED states above.

§3 CA-first resolution order — sound in principle; the dead endpoint and weak pubkey check are the problems, not the order.

§4 normalization — sound except for the fallback/inspection contradiction.

§5 conflict rule ("simulation wins, never fire on conflict") — sound.

§6 spelling lock core (no guess-and-fix, confirmed_by allowlist) — sound except for the missing ambiguity state.

§8 send policy (simulate, skipPreflight, maxRetries 0, manual rebroadcast, same-mint rebuild) — sound except for the UNKNOWN transition.

§9 break-even and P&L arithmetic for Split A/B at 13.5/27.1 SOL — correct given the stated fee split.

§10 latency arithmetic (4.9s P50 / 8.2s P99) — correct; the 2s polling fallback is the problem.

§11 FAIL criteria — sound as failure definitions; the setup makes them unreachable.

§12 fee-claim dust and Holder Rewards OPEN notes — sound acknowledgments.

Verdict: FIX-FIRST

Not BUILD: a dead metadata endpoint, a load test that cannot spend money, and an UNKNOWN race that can strand a confirmed launch are not ship-ready. Not REWORK: the two-phase invariant, single-signer model, same-mint persistence, and ledger-first architecture are the right bones. The fixes are targeted, not a redesign.

Top three that matter most:

§3 — replace frontend-api.pump.fun with frontend-api-v3.pump.fun and boot-probe it. Without this, CA-first resolution fails on day one.

§11 — fund the load test with ≥0.10 SOL and require at least one confirmed launch. A 0.01 SOL bankroll cannot reach RESERVED; the test validates nothing.

§1.3/§8 — add UNKNOWN → CONFIRMED and re-query the old signature before any rebuild. This is the difference between "we launched and didn't know it" and "we launched twice / failed a live coin."
