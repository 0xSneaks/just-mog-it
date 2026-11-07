<p align="center"><img src="assets/mog-cat-hero.jpg" width="480" alt="JustMogIt — the mog cat"></p>

# JustMogIt

The bot that rescues coins stuck on the wrong pair and relaunches them on a SOL pair.

**Start here:**

- [`BUILD-SPEC-v2.md`](BUILD-SPEC-v2.md) is the current spec.
- [`BOT.md`](BOT.md) covers what the code does and how to run it.
- [`docs/DEEPSEEK-TIMEOUT.md`](docs/DEEPSEEK-TIMEOUT.md) records the DeepSeek timeout ruling.
- The v0 docs (old README, SKILL, SPEC) are archived in [`docs/archive/v0/`](docs/archive/v0/). They are superseded.

## Built vs specced

| Area | Status | Where |
|---|---|---|
| Money core: lifetime $30 cap ledger, reserve-before-submit, tx guard pinned to Meteora DBC, simulate-before-send | **BUILT** | `mogit/` |
| DeepSeek pair evaluator (`deepseek-flash`), live and verified | **BUILT** | `bot/live/deepseek.py` · Actions run [42407864926](https://github.com/0xSneaks/just-mog-it/actions/runs/42407864926) |
| DeepSeek timeout ruling: 12 s adapter, boot warm-up, one caller retry, then skip | **BUILT** | `bot/pair_review.py`, `bot/live/deepseek_warmup.py` |
| Story-only rescue copy; the retired dev/NFA disclaimer is blocked by a test | **BUILT** | `config/policy.yaml`, `tests/test_retired_disclaimer.py` |
| Mogz callout step, post-launch: owner approval per item, refused if the posting wallet holds the mint, done only when the GET confirms | **BUILT** (adapters not wired) | `bot/callout.py` |
| Launch metadata reader (read-only) | **BUILT** | `scripts/read_launch_metadata.py` |
| PFP reply bot v1.0.0 | **BUILT** | `mogit/bot.py`, `mogit/pfp.py` (setup in the [v0 README](docs/archive/v0/README.md)) |
| X-bot job engine, gates and fee split (offline, fake adapters) | **BUILT, not live** | `bot/` |
| Mogz create adapter (preview-default, fail-closed gates) | **NOT BUILT**: waiting on the proven create payload | — |
| pump.fun launch pipeline | **SPECCED** (the bot has never launched on pump.fun) | `BUILD-SPEC-v2.md` |

Fixed money rules, enforced in code: SOL pairs only · dev buy 0 · per-launch cost cap 0.025 SOL · 0.02 SOL upfront fee · max 3 launches/day · kill switch ON at boot · lifetime spend cap $30.

## Launch record

| # | CA | Create tx | Venue | Dev buy | Note |
|---|---|---|---|---|---|
| 001 | `3cHj1iCgZb18Q8ZrqbsAtwNGjn6gifQRqnCedHhdmogz` | `5BBJ1MNdrgVJ89PsrRTNJqTvNUTonXdhJ59embQjgDsMNPVmCkRR217tkKj4oM4YWCsHHS1Z4S24gLoSzGPLRLQp` | mogz.fun | 0 | Launched manually via external executor, not the bot. |

## Tests

```
pip install -r requirements.txt -r requirements-deepseek-preview.txt pyyaml
python3 -m pytest -q
```

## License

MIT (see [`LICENSE`](LICENSE)).
