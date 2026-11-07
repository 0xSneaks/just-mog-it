"""Interim surface: terminal REPL. Real adapters are NOT wired (no Mogz/vault access here) -> refuses at preflight."""
import sys
from .flow import Session
from .ledger import Ledger
class Unwired:
    def __getattr__(self, n):
        raise SystemExit("adapter not wired: needs muse's Mogz flow + vault path (Sneaks's yes)")
if __name__ == "__main__":
    s = Session(sys.argv[1] if len(sys.argv) > 1 else "grok-bot", Ledger("ledger.json"), Unwired(), Unwired(), Unwired(), Unwired())
    for line in sys.stdin: print(s.handle(line))
