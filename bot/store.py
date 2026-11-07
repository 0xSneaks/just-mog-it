"""Durable state (sqlite). Money is integer lamports. One writer; every money move runs
inside BEGIN IMMEDIATE so check -> reserve -> hold is atomic (spec §7)."""
import json
import sqlite3
import time
from contextlib import contextmanager

LAMPORTS = 1_000_000_000
LIVE = ("RECEIVED", "DEDUPED", "GATED", "SPELLING_PENDING", "PAYMENT_PENDING", "STAGED", "QUEUED",
        "RESERVED", "ARMED", "SIGNED", "SENT", "UNKNOWN", "CONFIRMED", "REPLIED")
TERMINAL = ("LEDGERED", "REJECTED", "FAILED", "EXPIRED", "CANCELLED", "REPLY_ESCALATED")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs(
  job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_tweet_id TEXT NOT NULL UNIQUE,
  requester_id TEXT NOT NULL, dedupe_key TEXT, state TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, ttl_at REAL NOT NULL,
  rejection_code TEXT, payload TEXT NOT NULL DEFAULT '{{}}',
  mint TEXT, attempt_no INTEGER NOT NULL DEFAULT 0, tx_sig TEXT, reply_tweet_id TEXT,
  approved_by TEXT, payment_tx TEXT, fee_wallet TEXT, reply_failures INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS dedupe_live ON jobs(dedupe_key)
  WHERE dedupe_key IS NOT NULL AND state IN ({",".join(repr(s) for s in LIVE)});
CREATE TABLE IF NOT EXISTS reservations(
  rid INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE, lamports INTEGER NOT NULL,
  state TEXT NOT NULL, day TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, job_id TEXT,
  mint TEXT, lamports INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accruals(
  mint TEXT NOT NULL, party TEXT NOT NULL, party_id TEXT NOT NULL,
  accrued INTEGER NOT NULL DEFAULT 0, paid INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(mint, party, party_id));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, job_id TEXT,
  frm TEXT, too TEXT, note TEXT);
"""

class Store:
    def __init__(self, path=":memory:", clock=time.time):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.clock = clock

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # --- kv: kill switch, bankroll, counters ---------------------------------
    def get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def put(self, k, v, db=None):
        (db or self.db).execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                                (k, json.dumps(v)))

    # --- jobs -----------------------------------------------------------------
    def job(self, job_id):
        r = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(r) if r else None

    def jobs(self, *states):
        q = "SELECT * FROM jobs" + (f" WHERE state IN ({','.join('?' * len(states))})" if states else "")
        return [dict(r) for r in self.db.execute(q + " ORDER BY created_at", states)]

    def payload(self, job_id):
        return json.loads(self.job(job_id)["payload"])

    def set_payload(self, job_id, **kv):
        p = self.payload(job_id); p.update(kv)
        self.db.execute("UPDATE jobs SET payload=? WHERE job_id=?", (json.dumps(p, default=str), job_id))

    def audit(self, job_id, frm, to, note="", db=None):
        (db or self.db).execute("INSERT INTO audit(ts,job_id,frm,too,note) VALUES(?,?,?,?,?)",
                                (self.clock(), job_id, frm, to, note))

    def ledger_row(self, kind, body, job_id=None, mint=None, lamports=0, db=None):
        (db or self.db).execute("INSERT INTO ledger(ts,kind,job_id,mint,lamports,body) VALUES(?,?,?,?,?,?)",
                                (self.clock(), kind, job_id, mint, int(lamports), json.dumps(body, default=str)))

    def ledger(self, kind=None):
        q, a = "SELECT * FROM ledger", ()
        if kind:
            q, a = q + " WHERE kind=?", (kind,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", a)]
