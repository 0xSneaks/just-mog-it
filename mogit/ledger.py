"""Gate 1: lifetime cap checked against a ledger FILE before signing; written only after success.
The ledger is never defaulted: a missing/corrupt ledger is a refusal, not a zero."""
import json, os, tempfile
from decimal import Decimal

CAP_USD = Decimal("30")

class LedgerError(Exception):
    pass

class Ledger:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            spent = Decimal(str(data["spent_usd"]))
            cap = Decimal(str(data.get("cap_usd", CAP_USD)))
        except (OSError, ValueError, KeyError) as e:
            raise LedgerError(f"ledger unreadable at {self.path}: {e}")
        if cap > CAP_USD:
            raise LedgerError("ledger cap above the $30 lifetime cap")
        return {"spent_usd": spent, "cap_usd": cap, "remaining_usd": cap - spent,
                "launches": data.get("launches", [])}

    def check(self, cost_usd):
        st = self.load()
        if Decimal(str(cost_usd)) > st["remaining_usd"]:
            raise LedgerError(f"budget cap hit: need ${cost_usd}, remaining ${st['remaining_usd']}")
        return st

    def commit(self, cost_usd, record):
        """Call ONLY after a confirmed, successful launch. Atomic write, then read-back verify."""
        st = self.check(cost_usd)
        new_spent = st["spent_usd"] + Decimal(str(cost_usd))
        data = {"cap_usd": str(st["cap_usd"]), "spent_usd": str(new_spent),
                "launches": st["launches"] + [record]}
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".ledger.")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)
        if self.load()["spent_usd"] != new_spent:
            raise LedgerError("ledger read-back mismatch")
        return new_spent
