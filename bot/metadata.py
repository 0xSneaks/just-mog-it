"""Dossier assembly (spec §3) — what the relaunch copies, and what it never copies.

Copied: name, ticker (exact, via spelling lock), image (verified, §4), description (with
the mandatory rescue prefix), socials (https only).
NEVER copied: creator/dev wallet, mint, metadata URI, fee recipients, "official"/"verified"
claims, non-https or script links. The relaunch is a labeled rescue copy, not the original."""
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .spelling import lock

SOCIAL_KEYS = ("twitter", "telegram", "website")
BANNED_DESC = re.compile(r"\b(official|verified|the real|team token|airdrop)\b", re.I)
RETIRED_DESC = re.compile(r"\bnot the original dev\b\.?|\bNFA\b\.?", re.I)  # owner ruling 2026-11-07: story-only copy

class DossierError(Exception):
    pass

@dataclass
class Dossier:
    ca: str
    name: str
    ticker: str
    description: str
    image_url: str
    socials: dict
    confidence: float
    confirmed_by: str
    resolution_method: str
    spelling: object = None
    eligibility_reasons: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

def _safe_social(url):
    if not url or not isinstance(url, str):
        return None
    p = urlparse(url.strip())
    if p.scheme != "https" or not p.netloc:
        return None
    return url.strip()

def build_description(policy, ticker, ca, original):
    prefix = policy.description_prefix.replace("{ticker}", ticker).replace("{ca}", ca)
    original = RETIRED_DESC.sub("", BANNED_DESC.sub("", (original or ""))).strip()
    room = int(policy.description_max_chars) - len(prefix) - 1
    if room < 0:
        raise DossierError("description prefix alone exceeds description_max_chars")
    body = original[:room].rstrip()
    return (prefix + (" " + body if body else "")).strip()

def resolve(policy, *, ca, das_asset=None, pump_coin=None, quote_text_match=None, ocr=None):
    """Pick the source of truth and its confidence. Inputs are adapter results (dicts).
    Returns a Dossier or raises DossierError (reject with reply)."""
    if das_asset and das_asset.get("mint") == ca and (pump_coin is None or pump_coin.get("mint") == ca):
        # on-chain metadata is authoritative; the pump.fun record (when the original lived there)
        # adds description/socials. Non-pump originals (the wrong-venue case) resolve from DAS.
        src, conf, method, by = {**das_asset, **(pump_coin or {})}, 1.0, "ca_onchain", "onchain"
    elif quote_text_match and quote_text_match.get("exact"):
        src, conf, method, by = quote_text_match, 0.95, "exact_quote", "exact_quote"
    elif ocr and float(ocr.get("confidence", 0)) >= 0.9:
        src, conf, method, by = ocr, 0.85, "ocr", None        # needs requester_explicit
    else:
        raise DossierError("could not resolve the coin with enough confidence")
    name, ticker = src.get("name"), src.get("symbol") or src.get("ticker")
    if not name or not ticker:
        raise DossierError("source has no name/ticker")
    sp = lock(name, ticker, confirmed_by=by,
              source_name=(das_asset or {}).get("name") if by == "onchain" else None,
              source_ticker=(das_asset or {}).get("symbol") if by == "onchain" else None)
    socials = {k: v for k in SOCIAL_KEYS if (v := _safe_social(src.get(k)))}
    return Dossier(
        ca=ca, name=sp.name, ticker=sp.ticker,
        description=build_description(policy, sp.ticker, ca, src.get("description")),
        image_url=src.get("image_uri") or src.get("image"),
        socials=socials, confidence=conf, confirmed_by=by, resolution_method=method,
        spelling=sp, provenance={"source": method, "das": bool(das_asset), "pump": bool(pump_coin)})
