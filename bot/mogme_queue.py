"""'mog me' on X: the mention author's own PFP, remade with shield shades, via an owner
approval queue. Nothing posts unless the owner approved the item (or autopost is True AND
the kill switch is OFF; both are off by default).

Pipeline (per mention): skip retweets / own posts / duplicates → eligibility (>= 60 followers AND a
checkmark AND follows @justmogit_bot; silent skip otherwise) → 1 per author per rolling 24h
→ mog-me daily budget ($5.00, separate from the launch-reply budget) → target = the mention AUTHOR's avatar only (never
another tagged user, never an attached image) → hardened fetch (https, pbs.twimg.com only,
no redirects, 10 s, 5 MB, jpeg/png/webp by header AND magic bytes) → edit_pfp() via IMAGE_EDITOR=stub|gemini
(default stub; gemini needs GEMINI_API_KEY or boot is refused; avatars under 512 px are upscaled
first; owner-approved style references ride along) → "no change" check, max 2 retries, every
try billed to the budget → optional random tag in the bottom corner → queue item PENDING.
A safety refusal is logged and never retried. A default avatar, a failed fetch, or 3 tries
with no visible change queue a polite text-only draft (still needs approval to post)."""
import random
import base64
import binascii
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont

from . import replies

AVATAR_HOSTS = frozenset({"pbs.twimg.com"})
FETCH_TIMEOUT_S = 10
MAX_AVATAR_BYTES = 5 * 1024 * 1024
AVATAR_MIME = ("image/jpeg", "image/png", "image/webp")
COOLDOWN_S = 24 * 3600
EDIT_TIMEOUT_S = 60
MIN_SIDE = 512                      # avatars smaller than this are upscaled before the edit
UPSCALE_SIDE = 1024
MAX_EDIT_TRIES = 3                  # first try + max 2 retries on "no change"
MAX_REFS = 3
REJECT_REASONS = ("wrong size", "off the eyes", "wrong angle", "style mismatch", "flat overlay", "other")
PENDING, APPROVED, REJECTED, POSTING, POSTED = "PENDING", "APPROVED", "REJECTED", "POSTING", "POSTED"
POLITE_SKIP = "Couldn't grab your profile pic this time. Set one and tag me again for a mog."
POLITE_NO_CHANGE = "Your pic fought the shades and won this round. Tag me again later for a mog."


class MogMeError(Exception):
    pass


class MogMeConfigError(MogMeError):
    pass


class EditRefused(MogMeError):
    pass


class SkipMog(MogMeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ------------------------------------------------------------------ cost math
def _usd(value, name):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise MogMeConfigError(f"price {name} invalid") from None
    if not d.is_finite() or d < 0:
        raise MogMeConfigError(f"price {name} invalid")
    return d


def cost_per_mog(policy):
    """Worst-case USD per mog: Gemini image edit + one plain reply + reading the mention +
    one user lookup. Any missing price raises (fail closed)."""
    try:
        c = policy.raw["mog_me"]["costs_usd"]
        parts = (_usd(c["gemini_image"], "gemini_image"), _usd(policy.raw["x_prices_usd"]["plain_post"], "plain_post"),
                 _usd(c["mention_read"], "mention_read"), _usd(c["user_read"], "user_read"))
    except (KeyError, TypeError):
        raise MogMeConfigError("mog_me cost prices missing") from None
    return sum(parts, Decimal("0"))


def mogme_budget(policy):
    """Mog-me's own $/day (owner 2026-11-07: $5.00), separate from the launch-reply budget."""
    try:
        return _usd(policy.raw["mog_me"]["daily_budget_usd"], "daily_budget_usd")
    except (KeyError, TypeError):
        raise MogMeConfigError("mog_me.daily_budget_usd missing") from None


def worst_case_per_mog(policy):
    """Every try is billed: up to MAX_EDIT_TRIES Gemini calls per mog."""
    g = _usd(policy.raw["mog_me"]["costs_usd"]["gemini_image"], "gemini_image")
    return cost_per_mog(policy) + g * (MAX_EDIT_TRIES - 1)


def daily_cap(policy):
    """Mogs per day if every one lands on the first try. Spend is still checked before each try."""
    per = cost_per_mog(policy)
    return int(mogme_budget(policy) // per) if per > 0 else 0


# ------------------------------------------------------------------ avatar fetch
def full_size_url(url):
    return re.sub(r"_normal(?=(\.[A-Za-z0-9]+)?$)", "", url)


def check_avatar_url(url):
    if not url or not isinstance(url, str):
        raise SkipMog("no_avatar")
    if "/default_profile_images/" in url:
        raise SkipMog("default_avatar")
    p = urlparse(url)
    if (p.scheme != "https" or p.hostname not in AVATAR_HOSTS or p.username or p.password
            or p.port not in (None, 443)):
        raise SkipMog("bad_url")
    return url


def _sniff(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def fetch_avatar(url, get):
    """Returns (bytes, mime). Raises SkipMog on any problem."""
    try:
        status, headers, body = get(url, timeout=FETCH_TIMEOUT_S, max_bytes=MAX_AVATAR_BYTES)
    except Exception:
        raise SkipMog("fetch_failed") from None
    if status != 200:                                    # includes 3xx: redirects are never followed
        raise SkipMog("fetch_failed")
    ctype = str({k.lower(): v for k, v in (headers or {}).items()}.get("content-type", "")).split(";")[0].strip().lower()
    if ctype not in AVATAR_MIME:
        raise SkipMog("bad_type")
    if len(body) > MAX_AVATAR_BYTES:
        raise SkipMog("too_large")
    if _sniff(body) != ctype:
        raise SkipMog("bad_type")
    try:
        with Image.open(io.BytesIO(body)) as im:
            im.verify()
    except Exception:
        raise SkipMog("bad_image") from None
    return body, ctype


def httpx_get(url, timeout, max_bytes):
    """Real transport: no redirects, stops reading one byte past the cap."""
    import httpx
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=False) as r:
        body = b""
        for chunk in r.iter_bytes():
            body += chunk
            if len(body) > max_bytes:
                break
        return r.status_code, dict(r.headers), body


# ------------------------------------------------------------------ image editors
VERIFIED_TYPES = frozenset({"blue", "business", "government"})
MIN_FOLLOWERS = 60
GEMINI_MODEL = "gemini-2.5-flash-image"
GEMINI_TIMEOUT_S = 55
# Verbatim copy of PROMPTS.vipers in justmogit-site api/mog.js (branch pfp-changer @ 771a800,
# shades ruling 2026-11-07). Keep the two in sync: the site endpoint is the reference.
VIPERS_PROMPT = (
    "Edit this profile picture: add wraparound shield sport sunglasses to the "
    "face. Draw the sunglasses in exactly the same art style as the input image "
    "(a photo stays a photo, a cartoon stays a cartoon, pixel art stays pixel "
    "art, 3D stays 3D). Fit them to the face: sized to the head, centered on the "
    "eyes, following the head's angle and tilt, with the arms going back behind "
    "the ears or hair. Take the frame and lens colours from the picture's own "
    "palette. No text, letters, logos or brand names anywhere on the frames or "
    "lenses. Keep the face, identity, pose, clothing and background otherwise "
    "unchanged. Square 1:1 profile picture.")
REFS_PROMPT = (  # verbatim in justmogit-site api/mog.js (REFS_PROMPT)
    "The images after this text are owner-approved examples of the target look. Copy only how their shades are shaped, fitted to the face and drawn in each picture's own art style. Do NOT copy any text, letters, words, logos or brand names from them (ignore any lettering on their frames), and never copy their faces, characters, clothing, jewellery, captions or backgrounds.")


class StubEditor:
    """No network. Returns one fixed test image. Items it produces can never be posted."""
    name = "stub"

    def edit_pfp(self, image_bytes):
        im = Image.new("RGB", (512, 512), (20, 20, 20))
        d = ImageDraw.Draw(im)
        for k in range(0, 512, 32):
            d.line((k, 0, 0, k), fill=(255, 46, 147), width=4)
        d.rounded_rectangle((96, 200, 416, 280), radius=30, fill=(0, 214, 201))
        d.text((200, 228), "STUB PREVIEW", fill=(0, 0, 0))
        out = io.BytesIO(); im.save(out, "PNG"); return out.getvalue()


class GeminiEditor:
    """Same request as justmogit-site api/mog.js: generateContent with the vipers prompt and the
    image inline. No retry. A safety block or a response with no image raises EditRefused."""
    name = "gemini"

    def __init__(self, api_key, model=GEMINI_MODEL, post=None, refs=()):
        if not api_key or not str(api_key).strip():
            raise MogMeConfigError("GEMINI_API_KEY missing")
        self._key, self.model = str(api_key).strip(), model
        self._post = post or self._httpx_post
        self.refs = [r for r in refs if _sniff(r)][:MAX_REFS]

    @staticmethod
    def _httpx_post(url, json, timeout, headers):
        import httpx
        r = httpx.post(url, json=json, timeout=timeout, headers=headers, follow_redirects=False)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def edit_pfp(self, image_bytes):
        mime = _sniff(image_bytes)
        if mime is None:
            raise EditRefused("bad_input_image")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        parts = [{"text": VIPERS_PROMPT}, _inline(image_bytes, mime)]
        if self.refs:
            parts += [{"text": REFS_PROMPT}] + [_inline(r, _sniff(r)) for r in self.refs]
        body = {"contents": [{"parts": parts}]}
        try:
            status, out = self._post(url, json=body, timeout=GEMINI_TIMEOUT_S,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self._key})
        except Exception as e:
            raise EditRefused(f"edit_unavailable:{type(e).__name__}") from None
        if status != 200 or not isinstance(out, dict):
            raise EditRefused(f"edit_failed:{status}")
        block = (out.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise EditRefused(f"safety_block:{block}")
        try:
            parts = out["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            reason = ((out.get("candidates") or [{}])[0] or {}).get("finishReason", "no_image_returned")
            raise EditRefused(f"no_image_returned:{reason}") from None
        for part in parts if isinstance(parts, list) else []:
            inline = (part or {}).get("inlineData") or (part or {}).get("inline_data")
            if inline and inline.get("data"):
                try:
                    img = base64.b64decode(inline["data"])
                except (binascii.Error, ValueError):
                    break
                if _sniff(img):
                    return img
                break
        raise EditRefused("no_image_returned")


def _inline(data, mime):
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}


def load_refs(folder):
    """Up to MAX_REFS owner-approved reference mogs (png/jpeg/webp, <= 5 MB), newest first."""
    if not folder or not Path(folder).is_dir():
        return []
    files = sorted((p for p in Path(folder).iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files:
        if p.stat().st_size <= MAX_AVATAR_BYTES:
            b = p.read_bytes()
            if _sniff(b):
                out.append(b)
        if len(out) == MAX_REFS:
            break
    return out


def build_editor(env=None):
    """IMAGE_EDITOR=stub (default) | gemini. gemini without GEMINI_API_KEY refuses (fail closed);
    an unknown value refuses. Never falls back silently."""
    env = os.environ if env is None else env
    kind = (env.get("IMAGE_EDITOR") or "stub").strip().lower()
    if kind == "stub":
        return StubEditor()
    if kind == "gemini":
        return GeminiEditor(env.get("GEMINI_API_KEY", ""), model=(env.get("GEMINI_IMAGE_MODEL") or GEMINI_MODEL).strip(),
                            refs=load_refs(env.get("JMI_MOGME_REFS", "mogme_refs")))
    raise MogMeConfigError(f"IMAGE_EDITOR must be stub or gemini, got {kind!r}")


_TAG_OK = re.compile(r"[A-Z0-9][A-Z0-9 !'.-]{0,31}")
_TAG_BAD = re.compile(r"https?|www|\.(com|fun|io|xyz|net|org|app|it)\b|\$|%|\b\d+(\.\d+)?\s*(sol|usd|x)\b"
                      r"|pit ?viper|oakley|ray-?ban|nike|adidas", re.I)


def validate_tags(tags):
    """Owner tag lines: short caps text only. No URLs, prices, tickers or brands (fail closed)."""
    if not isinstance(tags, list):
        raise MogMeConfigError("mog_me.tags must be a list")
    for t in tags:
        if not isinstance(t, str) or not _TAG_OK.fullmatch(t) or _TAG_BAD.search(t):
            raise MogMeConfigError(f"tag not allowed: {t!r}")
    return list(tags)


def pick_tag(tags, rng):
    """No tag, or one line from the list, chosen at random with equal odds."""
    return rng.choice([None] + list(tags))


def add_tag(data, text):
    """Small tag in the bottom-right corner (at most ~6% of the height, kept off the centered
    face). text=None returns the image unchanged."""
    if not text:
        return data
    with Image.open(io.BytesIO(data)) as im:
        canvas = im.convert("RGBA")
    w, h = canvas.size
    size = max(10, min(w // 28, h // 24))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:                                    # Pillow < 10.1
        font = ImageFont.load_default()
    d = ImageDraw.Draw(canvas)
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    pad = max(4, size // 3)
    bw, bh = (r - l) + 2 * pad, (b - t) + 2 * pad
    x0, y0 = w - bw - pad, h - bh - pad
    d.rounded_rectangle((x0, y0, w - pad, h - pad), radius=pad, fill=(10, 10, 10, 230),
                        outline=(255, 46, 147, 255), width=max(1, size // 10))
    d.text((x0 + pad - l, y0 + pad - t), text, font=font, fill=(0, 255, 230, 255))
    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG")
    return out.getvalue()


def upscale(data):
    """Avatars under MIN_SIDE px are upscaled (LANCZOS) so the shorter side is UPSCALE_SIDE,
    re-encoded as PNG. Larger images pass through untouched."""
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        if min(w, h) >= MIN_SIDE:
            return data
        k = UPSCALE_SIDE / min(w, h)
        big = im.convert("RGB").resize((round(w * k), round(h * k)), Image.Resampling.LANCZOS)
    out = io.BytesIO(); big.save(out, "PNG"); return out.getvalue()


def changed_fraction(before, after, side=256, threshold=24):
    """Share of pixels that visibly changed (grayscale, both resized to side x side)."""
    with Image.open(io.BytesIO(before)) as a, Image.open(io.BytesIO(after)) as b:
        ga = a.convert("L").resize((side, side), Image.Resampling.BILINEAR)
        gb = b.convert("L").resize((side, side), Image.Resampling.BILINEAR)
    pa, pb = ga.tobytes(), gb.tobytes()
    return sum(1 for x, y in zip(pa, pb) if abs(x - y) > threshold) / float(side * side)


def ineligible(m):
    """Hard gate, fail closed. Reads followers_count + verified_type from the user fields that
    came with THIS mention read (never cached across requests). Returns a reason or None."""
    followers, vtype = m.get("followers_count"), m.get("verified_type")
    if isinstance(followers, bool) or not isinstance(followers, int) or not isinstance(vtype, str):
        return "missing_fields"
    if followers < MIN_FOLLOWERS:
        return "followers_lt_60"
    if vtype.lower() not in VERIFIED_TYPES:
        return "not_verified"
    return None


# ------------------------------------------------------------------ the flow
class MogMeFlow:
    def __init__(self, store, policy, x, editor, queue_dir, fetch=httpx_get, clock=time.time, rng=None):
        self.s, self.p, self.x, self.editor, self.fetch, self.clock = store, policy, x, editor, fetch, clock
        self.dir = Path(queue_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rng = rng or random.SystemRandom()
        cfg = policy.raw.get("mog_me") or {}
        self.tags = validate_tags(cfg.get("tags", []))      # bad tag config refuses boot
        mogme_budget(policy); cost_per_mog(policy)           # missing prices refuse boot
        try:
            self.min_changed = float(cfg["min_changed_fraction"])
        except (KeyError, TypeError, ValueError):
            raise MogMeConfigError("mog_me.min_changed_fraction missing") from None

    def spent(self, day=None):
        return Decimal(self.s.get(f"mogme_spend:{day or self.day()}", "0"))

    def _book(self, usd, day=None):
        k = f"mogme_spend:{day or self.day()}"
        self.s.put(k, str(Decimal(self.s.get(k, "0")) + usd))

    def _room(self, usd, day=None):
        return self.spent(day) + usd <= mogme_budget(self.p)

    def day(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")

    def killed(self):
        return self.s.get("kill", True) is not False

    # -------------------------------------------------- intake → preview
    def handle(self, m):
        mid, uid = str(m.get("id", "")), str(m.get("author_id", ""))
        if not mid or not uid:
            return ("ignored", "bad_mention")
        if m.get("is_retweet") or str(m.get("text", "")).lstrip().startswith("RT @"):
            return ("ignored", "retweet")
        if uid == str(getattr(self.x, "bot_user_id", "")):
            return ("ignored", "own_post")
        if self.s.get(f"mogme_seen:{mid}"):
            return ("ignored", "duplicate")
        self.s.put(f"mogme_seen:{mid}", True)
        why = ineligible(m)                              # free: fields come with the mention read
        if why:
            return self._silent(mid, uid, why)
        now, day = self.clock(), self.day()
        last = self.s.get(f"mogme_last:{uid}")
        if isinstance(last, (int, float)) and now - last < COOLDOWN_S:
            return ("ignored", "cooldown")
        try:
            per, cap = cost_per_mog(self.p), daily_cap(self.p)
        except MogMeConfigError:
            return ("ignored", "config")
        if int(self.s.get(f"mogme_count:{day}", 0)) >= cap:
            return ("ignored", "daily_cap")
        if not self._room(per, day):                     # mog-me's own budget, never the launch replies'
            return ("ignored", "budget")
        if not self._follows_bot(m, uid, day):           # last gate: may cost one user read
            return self._silent(mid, uid, "not_following")
        handle = str(m.get("handle") or "you")
        try:                                             # AUTHOR's avatar only
            url = m.get("profile_image_url")
            if url is None:
                url = self.x.profile_image_url(uid)
            url = check_avatar_url(full_size_url(url) if isinstance(url, str) else url)
            raw, mime = fetch_avatar(url, self.fetch)
        except SkipMog as e:
            self._save(mid, uid, handle, None, POLITE_SKIP, note=e.reason)
            self.s.put(f"mogme_last:{uid}", now)
            return ("mog_me_skipped", e.reason)
        except Exception:
            self._save(mid, uid, handle, None, POLITE_SKIP, note="fetch_failed")
            self.s.put(f"mogme_last:{uid}", now)
            return ("mog_me_skipped", "fetch_failed")
        # generation spends money: book cap and cooldown first, and every try before calling
        self.s.put(f"mogme_count:{day}", int(self.s.get(f"mogme_count:{day}", 0)) + 1)
        self.s.put(f"mogme_last:{uid}", now)
        try:
            src = upscale(raw)
        except Exception:
            self._save(mid, uid, handle, None, POLITE_SKIP, note="bad_image")
            return ("mog_me_skipped", "bad_image")
        gem = _usd(self.p.raw["mog_me"]["costs_usd"]["gemini_image"], "gemini_image")
        tries, fractions = 0, []
        while tries < MAX_EDIT_TRIES:
            if not self._room(gem, day):
                break
            self._book(gem, day)
            tries += 1
            try:
                edited = self.editor.edit_pfp(src)
            except EditRefused as e:                     # safety/failed: logged, never retried
                self.s.ledger_row("mogme_refused", {"mention_id": mid, "author_id": uid,
                                                    "reason": str(e), "tries": tries})
                return ("mog_me_refused", str(e))
            try:
                frac = changed_fraction(src, edited)
            except Exception:
                frac = 0.0
            fractions.append(round(frac, 4))
            if frac >= self.min_changed:
                tag = pick_tag(self.tags, self.rng)
                self._save(mid, uid, handle, add_tag(edited, tag), replies.mog_me_done(handle),
                           extra={"tries": tries, "changed": fractions, "tag": tag})
                return ("mog_me_queued", mid)
        self.s.ledger_row("mogme_no_change", {"mention_id": mid, "author_id": uid, "tries": tries,
                                              "changed": fractions})
        self._save(mid, uid, handle, None, POLITE_NO_CHANGE, note="no_change",
                   extra={"tries": tries, "changed": fractions})
        return ("mog_me_skipped", "no_change" if tries == MAX_EDIT_TRIES else "budget")

    def _silent(self, mid, uid, reason):
        """Ineligible: no reply, no image, no edit. Logged only."""
        self.s.ledger_row("mogme_ineligible", {"mention_id": mid, "author_id": uid, "reason": reason})
        return ("ignored", reason)

    def _follows_bot(self, m, uid, day):
        """connection_status (relative to the bot's user token) must contain "followed_by".
        Taken from the mention's author expansion when present (no extra call); otherwise one
        GET /2/users/:id?user.fields=connection_status, booked as a user read. Any failure = no."""
        cs = m.get("connection_status")
        if cs is None:
            try:
                cs = self.x.connection_status(uid)
            except Exception:
                cs = None
            finally:
                self._book(_usd(self.p.raw["mog_me"]["costs_usd"]["user_read"], "user_read"), day)
        return isinstance(cs, (list, tuple)) and "followed_by" in cs

    # -------------------------------------------------- queue
    def _path(self, mid):
        if not re.fullmatch(r"[0-9]{1,25}", mid):
            raise MogMeError("bad mention id")
        return self.dir / f"{mid}.json"

    def _write(self, item):
        p = self._path(item["mention_id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(item, indent=1))
        os.replace(tmp, p)

    def _save(self, mid, uid, handle, png, text, note=None, extra=None):
        img_path = None
        if png is not None:
            img_path = str(self.dir / f"{mid}.png")
            Path(img_path).write_bytes(png)
        self._write({"mention_id": mid, "reply_to": mid, "author_id": uid, "handle": handle,
                     "reply_text": text, "image_path": img_path,
                     "image_sha256": hashlib.sha256(png).hexdigest() if png else None,
                     "note": note, "status": PENDING, "created_at": self.clock(),
                     "editor": getattr(self.editor, "name", "unknown") if png is not None else None,
                     "approved_by": None, "decision_reason": None, "reply_id": None, **(extra or {})})

    def get(self, mid):
        try:
            return json.loads(self._path(str(mid)).read_text())
        except (OSError, ValueError, MogMeError):
            return None

    def items(self, status=None):
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                it = json.loads(p.read_text())
            except ValueError:
                continue
            if status is None or it.get("status") == status:
                out.append(it)
        return out

    def approve(self, mid, by, reason):
        """reason: a short note (1-80 chars, one line), saved with the item."""
        r = " ".join(str(reason or "").split())
        if not 1 <= len(r) <= 80:
            raise MogMeError("approve needs a short reason (1-80 chars)")
        return self._decide(mid, by, APPROVED, r)

    def reject(self, mid, by, reason):
        """reason: one of REJECT_REASONS, optionally "other: <short note>"."""
        r = " ".join(str(reason or "").split()).lower()
        head = r.split(":", 1)[0].strip()
        if head not in REJECT_REASONS or len(r) > 80 or (":" in r and head != "other"):
            raise MogMeError("reject reason must be one of: " + " / ".join(REJECT_REASONS))
        return self._decide(mid, by, REJECTED, r)

    def _decide(self, mid, by, status, reason):
        if not str(by or "").strip():
            raise MogMeError("owner approval needs a name")
        it = self.get(mid)
        if it is None or it["status"] != PENDING:
            raise MogMeError(f"{mid} is not PENDING")
        it.update(status=status, approved_by=str(by).strip(), decision_reason=reason)
        self._write(it)
        return it

    def add_ref(self, mid, folder):
        """Owner action: copy one APPROVED gemini mog into the style-reference folder."""
        it = self.get(mid)
        if not it or it["status"] not in (APPROVED, POSTED) or it.get("editor") != "gemini" or not it["image_path"]:
            raise MogMeError(f"{mid} is not an approved gemini mog")
        data = Path(it["image_path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != it["image_sha256"]:
            raise MogMeError("image changed after preview")
        Path(folder).mkdir(parents=True, exist_ok=True)
        dst = Path(folder) / f"ref_{mid}.png"
        dst.write_bytes(data)
        return str(dst)

    # -------------------------------------------------- posting (gated)
    def post_ready(self):
        """Post APPROVED items (or PENDING ones when autopost is exactly True). Never while the
        kill switch is ON. An item is marked POSTING before any X call, so a crash mid-post
        can never double-post; POSTING items need a manual look."""
        if self.killed():
            return []
        autopost = self.p.raw.get("mog_me", {}).get("autopost") is True
        done = []
        for it in self.items():
            if not (it["status"] == APPROVED or (autopost and it["status"] == PENDING)):
                continue
            if it.get("image_path") and it.get("editor") != "gemini":
                it["status"], it["note"] = REJECTED, "stub/unknown editor image never posts"
                self._write(it)
                continue
            reply_cost = _usd(self.p.raw["x_prices_usd"]["plain_post"], "plain_post")
            if not self._room(reply_cost):                # mog-me budget, not the launch replies'
                break
            self._book(reply_cost)
            it["status"] = POSTING
            self._write(it)
            media = None
            if it["image_path"]:
                data = Path(it["image_path"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != it["image_sha256"]:
                    it["status"], it["note"] = REJECTED, "image changed after preview"
                    self._write(it)
                    continue
                media = [self.x.upload_media(data, alt_text=replies.alt_text(it["handle"]))]
            rid = self.x.reply(it["reply_to"], it["reply_text"], media_ids=media,
                               idempotency_key=f"mogme:{it['mention_id']}")
            it.update(status=POSTED, reply_id=rid)
            self._write(it)
            done.append((it["mention_id"], rid))
        return done
