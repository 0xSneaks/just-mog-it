"""Hard-skip screen (owner non-negotiable): no deaths, violence, political fights, kids,
real private people, or scam/airdrop bait. Word-boundary matching (HOLE-2 lesson: 'elon'
must not hit 'Melon'). Text screen is a floor, not a ceiling: the image inspection gate
(spec §4) covers real-person faces, NSFW and brand logos."""
import re
import unicodedata

CATEGORIES = {
    "death": ["rip", "r i p", "died", "dies", "dead", "death", "killed", "funeral", "obituary",
              "rest in peace", "passed away", "memorial", "suicide", "overdose"],
    "violence": ["shooting", "shooter", "murder", "massacre", "terror", "terrorist", "bomb",
                 "bombing", "stabbing", "assassination", "assassinate", "school shooting", "war crime",
                 "genocide", "rape", "hostage"],
    "politics": ["trump", "biden", "harris", "obama", "maga", "democrat", "republican", "gop",
                 "election", "vote", "putin", "zelensky", "netanyahu", "hamas", "israel", "palestine",
                 "gaza", "ukraine", "russia", "senate", "congress", "president", "liberal", "conservative"],
    "kids": ["kid", "kids", "child", "children", "baby", "babies", "toddler", "minor", "minors",
             "teen", "teens", "schoolgirl", "schoolboy", "daughter", "son", "infant", "underage"],
    "scam": ["airdrop", "giveaway", "free sol", "claim now", "claim your", "connect wallet",
             "drainer", "presale", "pre sale", "whitelist", "guaranteed", "double your", "send sol",
             "dm me", "seed phrase", "private key", "100x guaranteed", "rug proof", "risk free"],
    "private_person": ["my ex", "my neighbor", "my boss", "my coworker", "my teacher", "dox", "doxx",
                       "home address", "phone number"],
}
_PATTERNS = {cat: re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b")
             for cat, words in CATEGORIES.items()}

def _norm(text):
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"[​-‏⁠﻿]", "", t)
    t = re.sub(r"[_\-.]+", " ", t)
    return re.sub(r"\s+", " ", t)

def screen(*texts, inspection=None):
    """Return (ok, category). ok=False means hard skip — never launched, never retried.
    `inspection` is the §4 image JSON when available."""
    blob = " ".join(_norm(t) for t in texts if t)
    for cat, pat in _PATTERNS.items():
        if pat.search(blob):
            return False, cat
    if inspection:
        if inspection.get("is_real_person") and not inspection.get("consent"):
            return False, "private_person"
        if float(inspection.get("nsfw_score", 0)) > 0.20:
            return False, "nsfw"
        if inspection.get("is_brand_logo") and not inspection.get("brand_permission"):
            return False, "brand_logo"
    return True, None
