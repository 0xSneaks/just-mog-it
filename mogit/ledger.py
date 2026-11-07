"""Gate 1: lifetime cap checked against a ledger FILE before signing.
The ledger is never defaulted: a missing/corrupt ledger is a refusal, not a zero.
FIX 3: a launch reserves its cost as a PENDING entry before submit; the reservation
counts against the cap until it is settled (send confirmed) or released (send
definitively failed), so a landed-but-unrecorded launch cannot leak the cap.
C2: a send merely REPORTED failed is not definitive — the entry is marked
unconfirmed and keeps counting; only a definitive pre-send rejection or a
reconcile step (expired blockhash + signature not found) releases it.
FIX 4: check -> reserve -> settle runs under an exclusive flock on a sidecar lock
file, so two sessions can never both pass the cap check for the same money."""
import fcntl, json, os, tempfile, threading, uuid
from decimal import Decimal

CAP_USD = Decimal("30")

class LedgerError(Exception):
    pass

class _FileLock:
    """Exclusive flock guard for one Ledger. Re-entrant for the owning thread only,
    so flow can hold the lock across check -> reserve -> submit -> settle while the
    ledger's own methods re-acquire it harmlessly on the same thread."""
    def __init__(self, ledger):
        self.ledger = ledger
    def __enter__(self):
        L = self.ledger
        if L._lock_owner == threading.get_ident():
            L._lock_depth += 1
            return L
        fd = os.open(L.path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        L._lock_fd, L._lock_owner, L._lock_depth = fd, threading.get_ident(), 1
        return L
    def __exit__(self, *exc):
        L = self.ledger
        L._lock_depth -= 1
        if L._lock_depth == 0:
            fcntl.flock(L._lock_fd, fcntl.LOCK_UN)
            os.close(L._lock_fd)
            L._lock_fd = L._lock_owner = None
        return False

class Ledger:
    def __init__(self, path):
        self.path = path
        self._lock_fd = None
        self._lock_owner = None
        self._lock_depth = 0

    def locked(self):
        """Hold the exclusive ledger lock (FIX 4). Blocks until any other session's
        confirm sequence finishes; never lets two confirms interleave."""
        return _FileLock(self)

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
        pending = data.get("pending", [])
        reserved = sum((Decimal(str(p["cost_usd"])) for p in pending), Decimal("0"))
        return {"spent_usd": spent, "cap_usd": cap, "reserved_usd": reserved,
                "remaining_usd": cap - spent - reserved,
                "launches": data.get("launches", []), "pending": pending,
                "kill": data.get("kill")}  # raw; only exactly False means OFF (see kill_on)

    def _save(self, st, spent_usd, launches, pending):
        data = {"cap_usd": str(st["cap_usd"]), "spent_usd": str(spent_usd),
                "launches": launches, "pending": pending}
        if st.get("kill") is not None:
            data["kill"] = st["kill"]  # persist the kill switch across every write
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".ledger.")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def kill_on(self):
        """Kill switch, persisted in the ledger file. ON unless the stored value is exactly
        False: a missing key, any other value, or an unreadable ledger all mean ON."""
        try:
            return self.load()["kill"] is not False
        except LedgerError:
            return True

    def set_kill(self, on):
        with self.locked():
            st = self.load()
            st["kill"] = bool(on)
            self._save(st, st["spent_usd"], st["launches"], st["pending"])
            if self.kill_on() != bool(on):
                raise LedgerError("ledger read-back mismatch on set_kill")
            return bool(on)

    def boot(self):
        """Kill switch ON at boot (owner §0). The owner turns it OFF explicitly."""
        return self.set_kill(True)

    def check(self, cost_usd):
        st = self.load()
        if Decimal(str(cost_usd)) > st["remaining_usd"]:
            raise LedgerError(f"budget cap hit: need ${cost_usd}, remaining ${st['remaining_usd']}")
        return st

    def reserve(self, cost_usd, record):
        """FIX 3: write a PENDING reservation BEFORE submit. It counts against the cap
        (see load()) until settle() or release(). Returns the reservation id."""
        with self.locked():
            st = self.check(cost_usd)
            rid = uuid.uuid4().hex
            entry = dict(record)
            entry.update({"id": rid, "cost_usd": str(cost_usd), "status": "pending"})
            self._save(st, st["spent_usd"], st["launches"], st["pending"] + [entry])
            if not any(p["id"] == rid for p in self.load()["pending"]):
                raise LedgerError("ledger read-back mismatch on reserve")
            return rid

    def settle(self, rid, record):
        """FIX 3: convert a pending reservation into recorded spend once the send is
        confirmed. Raising here loses nothing: the caller keeps the pending entry,
        which keeps counting against the cap."""
        with self.locked():
            st = self.load()
            mine = [p for p in st["pending"] if p["id"] == rid]
            if not mine:
                raise LedgerError(f"no pending reservation {rid} to settle")
            new_spent = st["spent_usd"] + Decimal(str(mine[0]["cost_usd"]))
            rest = [p for p in st["pending"] if p["id"] != rid]
            self._save(st, new_spent, st["launches"] + [record], rest)
            if self.load()["spent_usd"] != new_spent:
                raise LedgerError("ledger read-back mismatch on settle")
            return new_spent

    def mark_unconfirmed(self, rid):
        """C2: mark a pending reservation unconfirmed — the send was reported failed
        but may still land on-chain, so it keeps counting against the cap until
        settle() or a reconcile-driven release()."""
        with self.locked():
            st = self.load()
            mine = [p for p in st["pending"] if p["id"] == rid]
            if not mine:
                raise LedgerError(f"no pending reservation {rid} to mark unconfirmed")
            mine[0]["status"] = "unconfirmed"
            self._save(st, st["spent_usd"], st["launches"], st["pending"])
            if not any(p["id"] == rid and p["status"] == "unconfirmed" for p in self.load()["pending"]):
                raise LedgerError("ledger read-back mismatch on mark_unconfirmed")
            return True

    def release(self, rid):
        """Drop a pending reservation without recording spend (send definitively
        failed / blockhash expired and signature not found)."""
        with self.locked():
            st = self.load()
            rest = [p for p in st["pending"] if p["id"] != rid]
            if len(rest) == len(st["pending"]):
                return False
            self._save(st, st["spent_usd"], st["launches"], rest)
            return True

    def commit(self, cost_usd, record):
        """Direct spend write with no reservation (kept for non-flow callers).
        Atomic write, then read-back verify."""
        with self.locked():
            st = self.check(cost_usd)
            new_spent = st["spent_usd"] + Decimal(str(cost_usd))
            self._save(st, new_spent, st["launches"] + [record], st["pending"])
            if self.load()["spent_usd"] != new_spent:
                raise LedgerError("ledger read-back mismatch")
            return new_spent
