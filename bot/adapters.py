"""Adapter contracts. The engine never constructs real I/O; adapters are injected.
Every default is Unwired: calling it raises, so an unconfigured bot fails CLOSED.

Real adapters live in bot/live/ (⚠️ HIGH-RISK, need Sneaks's yes + independent review):
  x        — X API v2: mentions/stream, user lookup, reply, media upload (OAuth2 + media.write,
             authorized AS @justmogit_bot)
  dossier  — Helius DAS getAsset, pump.fun frontend-api-v3 coins/{mint}, Birdeye, coin snapshot
  pair     — pump.fun create simulation (spec §5)
  pump     — PumpPortal create-tx builder + collectCreatorFee
  guard    — bot/pumpguard.py (pump.fun create/buy/claim decoder; pin against a real
             PumpPortal create tx before live use)
  vault    — signer; keys live only in the vault, never in this repo
  rpc      — dual-RPC send + getSignatureStatuses
  payments — verify the 0.02 SOL fee transfer (job address + memo = job_id)
  faces    — eye landmarks for "mog me" (MediaPipe / Gemini Vision)"""

class AdapterUnwired(RuntimeError):
    pass

class Unwired:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        def _refuse(*a, **k):
            raise AdapterUnwired(f"{self._name}.{attr} not wired — bot fails closed")
        return _refuse

class Adapters:
    NAMES = ("x", "dossier", "pair", "pump", "guard", "vault", "rpc", "payments", "faces")

    def __init__(self, **given):
        unknown = set(given) - set(self.NAMES)
        if unknown:
            raise ValueError(f"unknown adapters: {unknown}")
        for n in self.NAMES:
            setattr(self, n, given.get(n) or Unwired(n))
