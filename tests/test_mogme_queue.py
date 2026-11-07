"""'mog me' X flow with an owner approval queue. Mocked X, avatar fetch and Gemini edit only;
no live calls (sockets are not used by any code path here)."""
import io
import json
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from bot import mogme_queue as mq
from bot.engine import Engine
from bot.adapters import Adapters
from bot.policy import load
from bot.store import Store

BOT_ID = "999"
AVATAR = "https://pbs.twimg.com/profile_images/123/abc_normal.jpg"
DAY = 86_400


def png(size=(400, 400), color=(200, 180, 160)):
    b = io.BytesIO(); Image.new("RGB", size, color).save(b, "PNG"); return b.getvalue()


def jpeg(size=(400, 400)):
    b = io.BytesIO(); Image.new("RGB", size, (90, 90, 200)).save(b, "JPEG"); return b.getvalue()


class X:
    bot_user_id = BOT_ID

    def __init__(self):
        self.replies, self.media, self.lookups = [], [], []

    def profile_image_url(self, uid):
        self.lookups.append(uid); return f"https://pbs.twimg.com/profile_images/{uid}/me_normal.png"

    def upload_media(self, data, alt_text=None):
        self.media.append((data, alt_text)); return f"m{len(self.media)}"

    def reply(self, tweet_id, text, media_ids=None, idempotency_key=None):
        self.replies.append((tweet_id, text, media_ids)); return f"r{len(self.replies)}"


class Fetch:
    """Avatar transport stub: url -> (status, headers, body)."""
    def __init__(self, body=None, ctype="image/jpeg", status=200):
        self.body, self.ctype, self.status, self.urls = jpeg() if body is None else body, ctype, status, []

    def __call__(self, url, timeout, max_bytes):
        assert timeout == mq.FETCH_TIMEOUT_S and max_bytes == mq.MAX_AVATAR_BYTES
        self.urls.append(url); return self.status, {"content-type": self.ctype}, self.body


class Editor:
    def __init__(self, refuse=False):
        self.refuse, self.calls = refuse, []

    def edit(self, data, mime):
        self.calls.append((data, mime))
        if self.refuse:
            raise mq.EditRefused("safety_block")
        return png((512, 512), (255, 46, 147))


class Clock:
    def __init__(self, t=20_000 * DAY + 3_600): self.t = t
    def __call__(self): return self.t


def mention(i, author="111", text="@justmogit_bot mog me", **kw):
    return {"id": str(i), "author_id": author, "handle": f"user{author}", "text": text,
            "profile_image_url": AVATAR, **kw}


def flow(tmp=None, x=None, editor=None, fetch=None, clock=None, policy_over=None, kill=True):
    store = Store(clock=clock or Clock())
    store.put("kill", kill)
    p = load(overrides=policy_over or {})
    f = mq.MogMeFlow(store, p, x or X(), editor or Editor(), tmp or tempfile.mkdtemp(),
                     fetch=fetch or Fetch(), clock=clock or Clock())
    return f, store


# ------------------------------------------------------------------ targeting
def test_author_only_target():
    x, fetch, ed = X(), Fetch(), Editor()
    f, _ = flow(x=x, fetch=fetch, editor=ed)
    m = mention(1, author="111", text="@justmogit_bot @victim mog me",
                attachments={"media_keys": ["3_victimphoto"]})
    assert f.handle(m) == ("mog_me_queued", "1")
    assert fetch.urls == ["https://pbs.twimg.com/profile_images/123/abc.jpg"]   # author's avatar, full size
    assert x.lookups == []
    item = f.get("1")
    assert item["author_id"] == "111" and item["reply_to"] == "1"


def test_author_avatar_looked_up_when_missing_and_never_from_other_fields():
    x, fetch = X(), Fetch()
    f, _ = flow(x=x, fetch=fetch)
    m = mention(1, author="222"); m.pop("profile_image_url")
    m["quoted_author_profile_image_url"] = "https://pbs.twimg.com/profile_images/9/other_normal.jpg"
    assert f.handle(m)[0] == "mog_me_queued"
    assert x.lookups == ["222"]
    assert fetch.urls == ["https://pbs.twimg.com/profile_images/222/me.png"]


# ------------------------------------------------------------------ intake filters
def test_dedupe_by_tweet_id():
    ed = Editor(); f, _ = flow(editor=ed)
    assert f.handle(mention(1))[0] == "mog_me_queued"
    assert f.handle(mention(1)) == ("ignored", "duplicate")
    assert len(ed.calls) == 1


def test_retweets_and_own_posts_ignored():
    ed = Editor(); f, _ = flow(editor=ed)
    assert f.handle(mention(1, is_retweet=True)) == ("ignored", "retweet")
    assert f.handle(mention(2, text="RT @someone: @justmogit_bot mog me")) == ("ignored", "retweet")
    assert f.handle(mention(3, author=BOT_ID)) == ("ignored", "own_post")
    assert ed.calls == []


# ------------------------------------------------------------------ limits
def test_cooldown_one_per_user_per_rolling_24h():
    clock = Clock(); ed = Editor(); f, _ = flow(editor=ed, clock=clock)
    assert f.handle(mention(1))[0] == "mog_me_queued"
    clock.t += DAY - 1
    assert f.handle(mention(2)) == ("ignored", "cooldown")
    clock.t += 1
    assert f.handle(mention(3))[0] == "mog_me_queued"
    assert len(ed.calls) == 2


def test_cost_math_and_daily_cap():
    p = load()
    per = mq.cost_per_mog(p)
    assert per == Decimal("0.039") + Decimal("0.015") + Decimal("0.005") + Decimal("0.010")  # 0.069
    assert mq.daily_cap(p) == 4                                   # floor(0.30 / 0.069)
    assert mq.daily_cap(p) * per <= Decimal(str(p.reply_budget_usd_per_day))


def test_daily_cap_blocks_generation():
    ed = Editor(); f, _ = flow(editor=ed)
    results = [f.handle(mention(i, author=str(100 + i)))[0] for i in range(6)]
    assert results == ["mog_me_queued"] * 4 + ["ignored"] * 2
    assert f.handle(mention(9, author="900")) == ("ignored", "daily_cap")
    assert len(ed.calls) == 4


def test_missing_price_fails_closed():
    p = load()
    raw = json.loads(json.dumps(p.raw)); del raw["mog_me"]["costs_usd"]["gemini_image"]
    with pytest.raises(mq.MogMeConfigError):
        mq.cost_per_mog(type(p)(raw))


def test_budget_shared_with_other_replies_blocks_generation():
    ed = Editor(); f, store = flow(editor=ed)
    store.put(f"reply_spend:{f.day()}", "0.25")                    # coin replies already spent today
    assert f.handle(mention(1)) == ("ignored", "budget")
    assert ed.calls == []


# ------------------------------------------------------------------ preview / approval / kill
def test_preview_never_posts():
    x = X(); f, _ = flow(x=x, kill=False)
    assert f.handle(mention(1))[0] == "mog_me_queued"
    item = f.get("1")
    assert item["status"] == mq.PENDING
    assert Path(item["image_path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert f.post_ready() == []                                    # not approved, autopost off
    assert x.replies == [] and x.media == []


def test_kill_switch_blocks_posting_even_when_approved():
    x = X(); f, store = flow(x=x, kill=True)
    f.handle(mention(1)); f.approve("1", "sneaks")
    assert f.post_ready() == []
    assert x.replies == [] and x.media == []
    store.put("kill", None)                                        # missing/non-False = ON
    assert f.post_ready() == []
    assert x.replies == []


def test_approved_item_posts_once_when_kill_off():
    x = X(); f, _ = flow(x=x, kill=False)
    f.handle(mention(1)); f.approve("1", "sneaks")
    assert f.post_ready() == [("1", "r1")]
    assert x.replies == [("1", x.replies[0][1], ["m1"])]
    assert f.get("1")["status"] == mq.POSTED
    assert f.post_ready() == []                                    # never twice


def test_approval_requires_a_name():
    f, _ = flow()
    f.handle(mention(1))
    with pytest.raises(mq.MogMeError):
        f.approve("1", " ")


def test_autopost_needs_flag_and_kill_off():
    for flag, kill, posts in [(True, True, 0), (False, False, 0), ("true", False, 0), (True, False, 1)]:
        x = X()
        over = {"mog_me": {**load().raw["mog_me"], "autopost": flag}}
        f, _ = flow(x=x, kill=kill, policy_over=over)
        f.handle(mention(1))
        assert len(f.post_ready()) == posts, (flag, kill)


def test_policy_defaults_autopost_off():
    assert load().raw["mog_me"]["autopost"] is False


# ------------------------------------------------------------------ fetch hardening
@pytest.mark.parametrize("url,reason", [
    ("http://pbs.twimg.com/profile_images/1/a_normal.jpg", "bad_url"),
    ("https://evil.example/profile_images/1/a_normal.jpg", "bad_url"),
    ("https://pbs.twimg.com.evil.example/a_normal.jpg", "bad_url"),
    ("https://user:pw@pbs.twimg.com/a_normal.jpg", "bad_url"),
    ("https://pbs.twimg.com:8443/a_normal.jpg", "bad_url"),
    ("https://abs.twimg.com/sticky/default_profile_images/default_profile_normal.png", "default_avatar"),
    ("https://pbs.twimg.com/sticky/default_profile_images/default_profile_normal.png", "default_avatar"),
    ("", "no_avatar"),
])
def test_bad_avatar_urls_rejected(url, reason):
    fetch, ed = Fetch(), Editor()
    f, _ = flow(fetch=fetch, editor=ed)
    assert f.handle(mention(1, profile_image_url=url)) == ("mog_me_skipped", reason)
    assert fetch.urls == [] and ed.calls == []


@pytest.mark.parametrize("fetch,reason", [
    (Fetch(status=302), "fetch_failed"),
    (Fetch(status=404), "fetch_failed"),
    (Fetch(ctype="text/html"), "bad_type"),
    (Fetch(ctype="image/svg+xml"), "bad_type"),
    (Fetch(ctype="image/gif"), "bad_type"),
    (Fetch(body=b"<html>not an image</html>"), "bad_type"),
    (Fetch(body=png(), ctype="image/jpeg"), "bad_type"),            # declared type must match bytes
    (Fetch(body=b"\xff\xd8\xff" + b"\0" * (mq.MAX_AVATAR_BYTES + 1)), "too_large"),
    (Fetch(body=b"\xff\xd8\xff\xe0garbage"), "bad_image"),
])
def test_bad_avatar_responses_rejected(fetch, reason):
    ed = Editor(); f, _ = flow(fetch=fetch, editor=ed)
    assert f.handle(mention(1)) == ("mog_me_skipped", reason)
    assert ed.calls == []


def test_fetch_exception_is_polite_skip_with_text_only_draft():
    def boom(url, timeout, max_bytes): raise TimeoutError("slow")
    x = X(); f, _ = flow(x=x, fetch=boom)
    assert f.handle(mention(1)) == ("mog_me_skipped", "fetch_failed")
    item = f.get("1")
    assert item["image_path"] is None and item["status"] == mq.PENDING
    assert "profile pic" in item["reply_text"].lower()


def test_full_size_url():
    assert mq.full_size_url("https://pbs.twimg.com/profile_images/1/a_normal.jpg") == \
        "https://pbs.twimg.com/profile_images/1/a.jpg"
    assert mq.full_size_url("https://pbs.twimg.com/profile_images/1/a_normal") == \
        "https://pbs.twimg.com/profile_images/1/a"


# ------------------------------------------------------------------ Gemini refusal
def test_gemini_refusal_no_post_no_queue_no_retry():
    x, ed = X(), Editor(refuse=True)
    f, store = flow(x=x, editor=ed, kill=False)
    assert f.handle(mention(1)) == ("mog_me_refused", "safety_block")
    assert len(ed.calls) == 1                                      # never retried
    assert f.get("1") is None
    assert f.post_ready() == [] and x.replies == []
    assert store.ledger("mogme_refused")


def test_site_editor_refusal_and_success():
    calls = []

    def post(url, json, timeout, headers):
        calls.append((url, json["style"], headers)); return post.resp
    post.resp = (502, {"error": "no_image_returned"})
    ed = mq.SiteMogEditor("https://example.vercel.app/api/mog", post=post, bypass="fixture-only")
    with pytest.raises(mq.EditRefused):
        ed.edit(jpeg(), "image/jpeg")
    import base64
    post.resp = (200, {"image": "data:image/png;base64," + base64.b64encode(png()).decode()})
    assert ed.edit(jpeg(), "image/jpeg")[:8] == b"\x89PNG\r\n\x1a\n"
    assert calls[0][1] == "vipers" and calls[0][2] == {"x-vercel-protection-bypass": "fixture-only"}
    post.resp = (200, {"image": "data:text/html;base64,PGh0bWw+"})
    with pytest.raises(mq.EditRefused):
        ed.edit(jpeg(), "image/jpeg")
    with pytest.raises(mq.MogMeConfigError):
        mq.SiteMogEditor("http://insecure.example/api/mog", post=post)


def test_get_mogged_tag_added_bottom_corner():
    out = mq.add_tag(png((512, 512), (0, 0, 0)))
    img = Image.open(io.BytesIO(out)).convert("RGB")
    assert img.size == (512, 512)
    assert img.getpixel((500, 500)) != (0, 0, 0)                   # tag drawn bottom-right
    assert img.getpixel((10, 10)) == (0, 0, 0)                     # rest untouched


# ------------------------------------------------------------------ engine wiring
def test_engine_routes_mog_me_to_queue_never_posts():
    x = X(); store = Store(clock=Clock())
    p = load(overrides={"allowlist": ["111"], "mog_me_enabled": True})
    f = mq.MogMeFlow(store, p, x, Editor(), tempfile.mkdtemp(), fetch=Fetch(), clock=Clock())
    e = Engine(store, p, Adapters(x=x), clock=Clock(), mogme=f)
    e.boot()
    assert e.ingest(mention(1)) == ("mog_me_queued", "1")
    assert x.replies == [] and x.media == []


def test_engine_mog_me_disabled_or_unwired():
    store = Store(clock=Clock())
    e = Engine(store, load(), Adapters(x=X()), clock=Clock())
    assert e.ingest(mention(1)) == ("ignored", "mog_me_disabled")
    e2 = Engine(Store(clock=Clock()), load(overrides={"mog_me_enabled": True, "allowlist": ["111"]}),
                Adapters(x=X()), clock=Clock())
    assert e2.ingest(mention(1)) == ("ignored", "mog_me_unwired")


def test_legacy_autonomous_pfp_bot_refuses_to_listen():
    from mogit import bot as legacy
    b = object.__new__(legacy.MogBot)               # no env, no network
    with pytest.raises(SystemExit) as exc:
        b.listen()
    assert "approval" in str(exc.value)


def test_run_mogme_post_refused_while_killed(monkeypatch, tmp_path, capsys):
    from bot import run
    monkeypatch.setenv("JMI_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("JMI_MOG_API_URL", "https://example.invalid/api/mog")
    monkeypatch.setenv("JMI_MOGME_QUEUE", str(tmp_path / "q"))
    monkeypatch.setenv("JMI_X_BOT_USER_ID", BOT_ID)
    posted = []
    import bot.live.x as livex
    monkeypatch.setattr(livex.XClient, "reply", lambda *a, **k: posted.append(a))
    monkeypatch.setattr(livex.XClient, "upload_media", lambda *a, **k: posted.append(a))
    assert run.main(["mogme-post"]) == 0                     # fresh store: kill missing = ON
    assert "kill switch is ON" in capsys.readouterr().out
    assert posted == []
