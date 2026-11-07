"""Real adapters behind bot/adapters.py contracts. Every network call goes through
http.Http, whose transport is injected in tests (no network). Secrets come from env vars
by NAME only; nothing secret is stored in this repo or in the bot's sqlite store.

VERIFIED: offline against fixtures that follow the public docs. UNVERIFIED live: this
build sandbox cannot reach X, Solana RPC, pump.fun, PumpPortal, Pinata or Gemini."""
