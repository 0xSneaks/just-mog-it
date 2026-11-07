"""Mention parsing: what did the user ask for? Pure functions, no I/O.

Commands (owner spec):
  "mog me"       -> PFP glasses (no coin, no money)
  "just mog it"  -> rescue-relaunch of the quoted/linked coin
Coin indicators (CA, $TICKER, quoted post) always win over "mog me" so a coin
request can never be misread as a picture request or vice versa."""
import re
import unicodedata
from dataclasses import dataclass, field

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(B58)}
CA_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
TICKER_RE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9]{0,9})\b")
MENTION_RE = re.compile(r"@\w{1,15}")
URL_RE = re.compile(r"https?://\S+")
PUMP_LINK_RE = re.compile(r"pump\.fun/(?:coin/)?([1-9A-HJ-NP-Za-km-z]{32,44})")

MOG_ME_RE = re.compile(r"^(?:pls |please |yo |hey )?mog ?me(?: pls| please| bro| fr| now)?$")
MOG_IT_RE = re.compile(r"\b(?:just mog it|mog it|mog this)\b")
YES_RE = re.compile(r"^(?:yes|y|yep|yeah|confirm)$")
STOP_RE = re.compile(r"^(?:stop|cancel|no)$")

def b58decode(s):
    n = 0
    for c in s:
        if c not in _B58_IDX:
            raise ValueError("not base58")
        n = n * 58 + _B58_IDX[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw

def is_pubkey(s):
    """A Solana pubkey is base58 that decodes to exactly 32 bytes."""
    try:
        return len(b58decode(s)) == 32
    except ValueError:
        return False

def _clean(text):
    t = unicodedata.normalize("NFKC", text)
    t = URL_RE.sub(" ", t)
    t = MENTION_RE.sub(" ", t)
    t = re.sub(r"[^\w$ ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()

@dataclass
class Trigger:
    kind: str                       # "mog_me" | "mog_coin" | "yes" | "stop" | "none"
    cas: list = field(default_factory=list)
    tickers: list = field(default_factory=list)
    quoted_tweet_id: str = None

def parse(text, quoted_tweet_id=None):
    links = PUMP_LINK_RE.findall(text)
    cas = []
    for c in links + CA_RE.findall(URL_RE.sub(" ", text)):
        if is_pubkey(c) and c not in cas:
            cas.append(c)
    tickers = [t.upper() for t in TICKER_RE.findall(text)]
    clean = _clean(text)
    coinish = bool(cas or tickers or quoted_tweet_id)
    if YES_RE.match(clean):
        return Trigger("yes", cas, tickers, quoted_tweet_id)
    if STOP_RE.match(clean):
        return Trigger("stop", cas, tickers, quoted_tweet_id)
    if MOG_IT_RE.search(clean) and coinish:
        return Trigger("mog_coin", cas, tickers, quoted_tweet_id)
    if MOG_ME_RE.match(clean) and not coinish:
        return Trigger("mog_me")
    return Trigger("none", cas, tickers, quoted_tweet_id)
