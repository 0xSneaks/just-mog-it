"""Spelling lock (spec §6). The name/ticker that launches is exactly the source spelling —
never guessed, never "fixed". Only onchain | exact_quote | requester_explicit may FIRE."""
import re
import unicodedata
from dataclasses import dataclass, field

ZERO_WIDTH = re.compile(r"[​-‏⁠﻿­]")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
CONFIRMERS = ("onchain", "exact_quote", "requester_explicit")

def normalize(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = ZERO_WIDTH.sub("", s)
    return re.sub(r"\s+", " ", s).strip()

def _scripts(s):
    out = set()
    for ch in s:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        out.add(name.split(" ")[0] if name else "UNKNOWN")
    return out

@dataclass
class SpellingLock:
    raw_name: str
    raw_ticker: str
    name: str = ""
    ticker: str = ""
    confirmed_by: str = None
    errors: list = field(default_factory=list)
    diff: list = field(default_factory=list)

    @property
    def fireable(self):
        return self.confirmed_by in CONFIRMERS and not self.errors and not self.diff

def lock(raw_name, raw_ticker, confirmed_by=None, source_name=None, source_ticker=None):
    """Build a lock. If source_* given (on-chain metadata or exact quoted text), the
    normalized values must match exactly or `diff` is set and the lock cannot fire."""
    L = SpellingLock(raw_name, raw_ticker)
    if CONTROL.search(raw_name or "") or CONTROL.search(raw_ticker or ""):
        L.errors.append("control_chars")
    L.name, L.ticker = normalize(raw_name), normalize(raw_ticker)
    if not 1 <= len(L.name) <= 32:
        L.errors.append("name_length")
    if not 1 <= len(L.ticker) <= 10:
        L.errors.append("ticker_length")
    for label, v in (("name", L.name), ("ticker", L.ticker)):
        sc = _scripts(v)
        if len(sc) > 1:
            L.errors.append(f"mixed_scripts_{label}")
    if source_name is not None and normalize(source_name) != L.name:
        L.diff.append(("name", normalize(source_name), L.name))
    if source_ticker is not None and normalize(source_ticker) != L.ticker:
        L.diff.append(("ticker", normalize(source_ticker), L.ticker))
    if confirmed_by is not None and confirmed_by not in CONFIRMERS:
        raise ValueError(f"confirmed_by must be one of {CONFIRMERS}")
    L.confirmed_by = confirmed_by
    return L

def question(L):
    """The one structured ambiguity reply (spec §6)."""
    return (f'I read: name "{L.name}", ticker "{L.ticker}". '
            "Reply YES, or send the exact name and ticker.")
