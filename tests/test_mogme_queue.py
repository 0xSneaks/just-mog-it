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

    follow_lookups = None

    def connection_status(self, uid):
        self.follow_lookups = (self.follow_lookups or []) + [uid]
        if isinstance(self.follow_answer, Exception):
            raise self.follow_answer
        return self.follow_answer

    follow_answer = ["followed_by"]

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
    name = "gemini"                                  # stands in for the real editor in tests

    def __init__(self, refuse=False):
        self.refuse, self.calls = refuse, []

    def edit_pfp(self, data, traits=None):
        self.calls.append(data)
        if self.refuse:
            raise mq.EditRefused("safety_block")
        return png((512, 512), (250, 250, 250))       # clearly changed vs the blue avatar


class Clock:
    def __init__(self, t=20_000 * DAY + 3_600): self.t = t
    def __call__(self): return self.t


def mention(i, author="111", text="@justmogit_bot mog me", **kw):
    return {"id": str(i), "author_id": author, "handle": f"user{author}", "text": text,
            "profile_image_url": AVATAR, "followers_count": 60, "verified_type": "blue",
            "connection_status": ["followed_by"], **kw}


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
    assert per == Decimal("0.039") + Decimal("0.015") + Decimal("0.005") + Decimal("0.010") + 2 * Decimal("0.002")  # 0.073
    assert mq.mogme_budget(p) == Decimal("5.00")                  # owner 2026-11-07, separate budget
    assert mq.daily_cap(p) == 68                                  # floor(5.00 / 0.073), all first-try
    assert mq.worst_case_per_mog(p) == Decimal("0.155")           # 3 edits + 4 pose calls + reply + 2 reads
    assert int(Decimal("5.00") // mq.worst_case_per_mog(p)) == 32  # every mog needing all 3 tries


def small_budget(usd="0.292"):
    return {"mog_me": {**load().raw["mog_me"], "daily_budget_usd": usd}}


def test_daily_cap_blocks_generation():
    ed = Editor(); f, _ = flow(editor=ed, policy_over=small_budget())          # cap 4
    results = [f.handle(mention(i, author=str(100 + i)))[0] for i in range(6)]
    assert results == ["mog_me_queued"] * 4 + ["ignored"] * 2
    assert f.handle(mention(9, author="900")) == ("ignored", "daily_cap")
    assert len(ed.calls) == 4


def test_missing_price_fails_closed():
    p = load()
    raw = json.loads(json.dumps(p.raw)); del raw["mog_me"]["costs_usd"]["gemini_image"]
    with pytest.raises(mq.MogMeConfigError):
        mq.cost_per_mog(type(p)(raw))


def test_mogme_budget_is_separate_from_launch_replies():
    ed = Editor(); f, store = flow(editor=ed)
    store.put(f"reply_spend:{f.day()}", "0.30")                    # launch replies used their whole budget
    assert f.handle(mention(1))[0] == "mog_me_queued"              # mog-me unaffected...
    assert store.get(f"reply_spend:{f.day()}") == "0.30"           # ...and never touches it
    store.put(f"mogme_spend:{f.day()}", "4.95")
    assert f.handle(mention(2, author="222")) == ("ignored", "budget")
    assert len(ed.calls) == 1


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
    f.handle(mention(1)); f.approve("1", "sneaks", "good fit")
    assert f.post_ready() == []
    assert x.replies == [] and x.media == []
    store.put("kill", None)                                        # missing/non-False = ON
    assert f.post_ready() == []
    assert x.replies == []


def test_approved_item_posts_once_when_kill_off():
    x = X(); f, _ = flow(x=x, kill=False)
    f.handle(mention(1)); f.approve("1", "sneaks", "good fit")
    assert f.post_ready() == [("1", "r1")]
    assert x.replies == [("1", x.replies[0][1], ["m1"])]
    assert f.get("1")["status"] == mq.POSTED
    assert f.post_ready() == []                                    # never twice


def test_approval_requires_a_name():
    f, _ = flow()
    f.handle(mention(1))
    with pytest.raises(mq.MogMeError):
        f.approve("1", " ", "good fit")


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


def test_gemini_editor_request_and_refusals_mocked():
    import base64
    calls = []

    def post(url, json, timeout, headers):
        calls.append((url, json, headers)); return post.resp
    ed = mq.GeminiEditor("fixture-only", post=post)
    post.resp = (200, {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png()).decode()}}]}}]})
    assert ed.edit_pfp(jpeg())[:8] == b"\x89PNG\r\n\x1a\n"
    url, body, headers = calls[0]
    assert url.endswith("/models/gemini-2.5-flash-image:generateContent")
    assert body["contents"][0]["parts"][0]["text"] == mq.compose_prompt(None)
    assert body["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/jpeg"
    assert headers["x-goog-api-key"] == "fixture-only"
    for resp in [(200, {"promptFeedback": {"blockReason": "SAFETY"}}),
                 (200, {"candidates": [{"finishReason": "IMAGE_SAFETY"}]}),
                 (200, {"candidates": [{"content": {"parts": [{"text": "I can't edit that"}]}}]}),
                 (400, {"error": {"message": "x"}}), (500, {})]:
        post.resp = resp
        with pytest.raises(mq.EditRefused):
            ed.edit_pfp(jpeg())
    assert len(calls) == 6                            # one call per edit, never retried


def test_stub_editor_fixed_image_no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    a, b = mq.StubEditor().edit_pfp(jpeg()), mq.StubEditor().edit_pfp(png())
    assert a == b and a[:8] == b"\x89PNG\r\n\x1a\n"


def test_build_editor_selection_fail_closed():
    assert isinstance(mq.build_editor({}), mq.StubEditor)                       # default = stub
    assert isinstance(mq.build_editor({"IMAGE_EDITOR": "stub", "GEMINI_API_KEY": "k"}), mq.StubEditor)
    assert isinstance(mq.build_editor({"IMAGE_EDITOR": "gemini", "GEMINI_API_KEY": "fixture-only"}), mq.GeminiEditor)
    for env in ({"IMAGE_EDITOR": "gemini"}, {"IMAGE_EDITOR": "gemini", "GEMINI_API_KEY": "  "},
                {"IMAGE_EDITOR": "dalle", "GEMINI_API_KEY": "k"}):
        with pytest.raises(mq.MogMeConfigError):
            mq.build_editor(env)


def test_stub_items_queue_but_never_post():
    x = X(); store = Store(clock=Clock()); store.put("kill", False)
    f = mq.MogMeFlow(store, load(), x, mq.StubEditor(), tempfile.mkdtemp(), fetch=Fetch(), clock=Clock())
    assert f.handle(mention(1)) == ("mog_me_queued", "1")
    assert f.get("1")["editor"] == "stub"
    f.approve("1", "sneaks", "good fit")
    assert f.post_ready() == []
    assert x.replies == [] and x.media == []
    assert f.get("1")["status"] == mq.REJECTED


# ------------------------------------------------------------------ eligibility (silent, fail closed)
@pytest.mark.parametrize("fields,result", [
    ({"followers_count": 59, "verified_type": "blue"}, ("ignored", "followers_lt_60")),
    ({"followers_count": 60, "verified_type": "blue"}, ("mog_me_queued", "1")),
    ({"followers_count": 60, "verified_type": "business"}, ("mog_me_queued", "1")),
    ({"followers_count": 60, "verified_type": "government"}, ("mog_me_queued", "1")),
    ({"followers_count": 60, "verified_type": "none"}, ("ignored", "not_verified")),
    ({"followers_count": 60, "verified_type": None}, ("ignored", "missing_fields")),
    ({"followers_count": None, "verified_type": "blue"}, ("ignored", "missing_fields")),
    ({"followers_count": "60", "verified_type": "blue"}, ("ignored", "missing_fields")),
    ({"followers_count": True, "verified_type": "blue"}, ("ignored", "missing_fields")),
])
def test_eligibility_gate(fields, result):
    x, fetch, ed = X(), Fetch(), Editor()
    f, store = flow(x=x, fetch=fetch, editor=ed)
    m = mention(1); m.update(fields)
    assert f.handle(m) == result
    if result[0] == "ignored":
        assert fetch.urls == [] and ed.calls == [] and f.get("1") is None and x.replies == []
        assert json.loads(store.ledger("mogme_ineligible")[0]["body"])["reason"] == result[1]


def test_missing_eligibility_keys_skip():
    ed = Editor(); f, _ = flow(editor=ed)
    m = mention(1); del m["followers_count"]; del m["verified_type"]
    assert f.handle(m) == ("ignored", "missing_fields") and ed.calls == []


@pytest.mark.parametrize("cs,answer,result", [
    (["followed_by"], None, ("mog_me_queued", "1")),               # from the mention expansion, no call
    (["following"], None, ("ignored", "not_following")),
    ([], None, ("ignored", "not_following")),
    ("MISSING", ["followed_by", "following"], ("mog_me_queued", "1")),  # one lookup
    ("MISSING", [], ("ignored", "not_following")),
    ("MISSING", RuntimeError("403"), ("ignored", "not_following")),   # lookup error = not following
    ("MISSING", None, ("ignored", "not_following")),
])
def test_follow_gate(cs, answer, result):
    x, ed = X(), Editor(); x.follow_answer = answer
    f, store = flow(x=x, editor=ed)
    m = mention(1)
    if cs == "MISSING":
        del m["connection_status"]
    else:
        m["connection_status"] = cs
    assert f.handle(m) == result
    assert x.follow_lookups == (["111"] if cs == "MISSING" else None)
    if result[0] == "ignored":
        assert ed.calls == [] and f.get("1") is None
    if cs == "MISSING":                                            # the lookup is booked against the budget
        booked = Decimal(store.get(f"mogme_spend:{f.day()}"))
        assert booked == Decimal("0.010") + (Decimal("0.039") if result[0] == "mog_me_queued" else 0)


def test_follow_lookup_only_after_free_gates():
    x = X(); f, _ = flow(x=x)
    m = mention(1, followers_count=10); del m["connection_status"]
    assert f.handle(m) == ("ignored", "followers_lt_60")
    assert x.follow_lookups is None                                # no paid call for an ineligible author


def test_get_mogged_tag_added_bottom_corner():
    out = mq.add_tag(png((512, 512), (0, 0, 0)), "GET MOGGED")
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
    monkeypatch.delenv("IMAGE_EDITOR", raising=False)
    monkeypatch.setenv("JMI_MOGME_QUEUE", str(tmp_path / "q"))
    monkeypatch.setenv("JMI_X_BOT_USER_ID", BOT_ID)
    posted = []
    import bot.live.x as livex
    monkeypatch.setattr(livex.XClient, "reply", lambda *a, **k: posted.append(a))
    monkeypatch.setattr(livex.XClient, "upload_media", lambda *a, **k: posted.append(a))
    assert run.main(["mogme-post"]) == 0                     # fresh store: kill missing = ON
    assert "kill switch is ON" in capsys.readouterr().out
    assert posted == []


def test_run_refuses_boot_when_gemini_selected_without_key(monkeypatch, tmp_path, capsys):
    from bot import run
    monkeypatch.setenv("JMI_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("JMI_MOGME_QUEUE", str(tmp_path / "q"))
    monkeypatch.setenv("JMI_X_BOT_USER_ID", BOT_ID)
    monkeypatch.setenv("IMAGE_EDITOR", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert run.main(["mogme-list"]) == 2
    assert "GEMINI_API_KEY missing" in capsys.readouterr().out


# ================================================================== owner fixes 2026-11-07 16:30
class SameEditor:
    """Returns its input untouched: the 'no change' failure."""
    name = "gemini"

    def __init__(self, succeed_on=None):
        self.calls, self.succeed_on, self.traits_seen = [], succeed_on, []

    def edit_pfp(self, data, traits=None):
        self.calls.append(data); self.traits_seen.append(dict(traits or {}))
        if self.succeed_on and len(self.calls) >= self.succeed_on:
            return png((512, 512), (250, 250, 250))
        return data


def test_prompt_rules_no_brand_text():
    t = mq.VIPERS_PROMPT
    for phrase in ("wraparound shield sport sunglasses", "same art style as the input",
                   "sized to the head", "centered on the eyes", "angle and tilt",
                   "behind the ears or hair", "own palette", "No text, letters, logos or brand names"):
        assert phrase in t, phrase
    assert "pit viper" not in t.lower()


def test_gemini_sends_up_to_three_style_refs_after_the_input():
    import base64
    sent = []

    def post(url, json, timeout, headers):
        sent.append(json); return 200, {"candidates": [{"content": {"parts": [
            {"inlineData": {"data": base64.b64encode(png()).decode()}}]}}]}
    refs = [png((64, 64), (i * 40, 0, 0)) for i in range(5)] + [b"not an image"]
    ed = mq.GeminiEditor("fixture-only", post=post, refs=refs)
    ed.edit_pfp(jpeg())
    parts = sent[0]["contents"][0]["parts"]
    assert parts[0]["text"] == mq.compose_prompt(None) and parts[1]["inline_data"]["mime_type"] == "image/jpeg"
    assert parts[2]["text"] == mq.REFS_PROMPT
    assert len(parts) == 3 + 3                                     # max 3 refs, junk dropped
    ed0 = mq.GeminiEditor("fixture-only", post=post)
    ed0.edit_pfp(jpeg())
    assert len(sent[1]["contents"][0]["parts"]) == 2               # no refs = prompt + input only


def test_load_refs_and_add_ref_owner_action(tmp_path):
    folder = tmp_path / "refs"; folder.mkdir()
    (folder / "junk.txt").write_text("hi")
    for i in range(4):
        (folder / f"r{i}.png").write_bytes(png((32, 32), (i, i, i)))
    assert len(mq.load_refs(str(folder))) == 3
    assert mq.load_refs(str(tmp_path / "missing")) == []
    f, _ = flow()
    f.handle(mention(1))
    with pytest.raises(mq.MogMeError):
        f.add_ref("1", str(tmp_path / "out"))                       # PENDING: not approved yet
    f.approve("1", "sneaks", "great fit")
    dst = f.add_ref("1", str(tmp_path / "out"))
    assert Path(dst).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    stub_f, _ = flow(editor=mq.StubEditor())
    stub_f.handle(mention(2, author="222")); stub_f.approve("2", "sneaks", "test")
    with pytest.raises(mq.MogMeError):
        stub_f.add_ref("2", str(tmp_path / "out"))                  # stub images never become refs


def test_small_avatars_upscaled_before_edit():
    ed = Editor(); f, _ = flow(editor=ed, fetch=Fetch(body=jpeg((200, 200))))
    assert f.handle(mention(1))[0] == "mog_me_queued"
    sent = Image.open(io.BytesIO(ed.calls[0]))
    assert min(sent.size) == mq.UPSCALE_SIDE
    ed2 = Editor(); f2, _ = flow(editor=ed2, fetch=Fetch(body=jpeg((600, 600))))
    f2.handle(mention(1))
    assert Image.open(io.BytesIO(ed2.calls[0])).size == (600, 600)  # big enough: untouched


def test_no_change_retries_twice_then_polite_text_every_try_billed():
    ed = SameEditor(); f, store = flow(editor=ed)
    assert f.handle(mention(1)) == ("mog_me_skipped", "no_change")
    assert len(ed.calls) == mq.MAX_EDIT_TRIES == 3
    item = f.get("1")
    assert item["image_path"] is None and item["note"] == "no_change" and item["tries"] == 3
    assert item["reply_text"] == mq.POLITE_NO_CHANGE
    assert Decimal(store.get(f"mogme_spend:{f.day()}")) == Decimal("0.039") * 3
    assert store.ledger("mogme_no_change")


def test_no_change_then_success_on_retry():
    ed = SameEditor(succeed_on=2); f, store = flow(editor=ed)
    assert f.handle(mention(1)) == ("mog_me_queued", "1")
    assert len(ed.calls) == 2 and f.get("1")["tries"] == 2
    assert Decimal(store.get(f"mogme_spend:{f.day()}")) == Decimal("0.078")


def test_flat_overlay_counts_as_no_change():
    def overlay(data):
        im = Image.open(io.BytesIO(data)).convert("RGB")
        d = __import__("PIL.ImageDraw", fromlist=["x"]).Draw(im)
        w, h = im.size
        d.rectangle((w * 0.30, h * 0.40, w * 0.45, h * 0.47), fill=(255, 0, 255))   # two small eye patches
        d.rectangle((w * 0.55, h * 0.40, w * 0.70, h * 0.47), fill=(255, 0, 255))
        out = io.BytesIO(); im.save(out, "PNG"); return out.getvalue()
    src = png((512, 512), (90, 90, 200))
    assert mq.changed_fraction(src, overlay(src)) < 0.06
    assert mq.changed_fraction(src, png((512, 512), (250, 250, 250))) > 0.9


def test_retry_stops_when_budget_runs_out():
    ed = SameEditor()
    f, store = flow(editor=ed, policy_over=small_budget("0.15"))   # room for 0.069 precheck, 3 tries = 0.117
    store.put(f"mogme_spend:{f.day()}", "0.07")                     # 0.08 left: two tries max
    assert f.handle(mention(1)) == ("mog_me_skipped", "budget")
    assert len(ed.calls) == 2


@pytest.mark.parametrize("reason,ok", [
    ("wrong size", True), ("off the eyes", True), ("wrong angle", True), ("style mismatch", True),
    ("flat overlay", True), ("other", True), ("other: cropped the hat", True), ("Wrong Size", True),
    ("", False), ("ugly", False), ("wrong size: too big", False), ("other: " + "x" * 90, False),
])
def test_reject_reason_from_list(reason, ok):
    f, _ = flow(); f.handle(mention(1))
    if ok:
        assert f.reject("1", "sneaks", reason)["decision_reason"] == " ".join(reason.split()).lower()
        assert f.get("1")["status"] == mq.REJECTED
    else:
        with pytest.raises(mq.MogMeError):
            f.reject("1", "sneaks", reason)
        assert f.get("1")["status"] == mq.PENDING


def test_approve_needs_short_reason_saved():
    f, _ = flow(); f.handle(mention(1))
    for bad in ("", "   ", "x" * 81):
        with pytest.raises(mq.MogMeError):
            f.approve("1", "sneaks", bad)
    assert f.approve("1", "sneaks", "fits the  eyes")["decision_reason"] == "fits the eyes"


@pytest.mark.parametrize("tag", ["visit justmog.it", "https://x.com", "BUY $MOG", "100X", "5 SOL", "10%",
                                 "PIT VIPER", "OAKLEY MOG", "get mogged", "", "A" * 33, 7])
def test_bad_captions_refuse_boot(tag):
    with pytest.raises(mq.MogMeConfigError):
        mq.validate_tags([tag])
    traits = {**TRAITS, "caption": [{"value": "GET MOGGED", "weight": 1}, {"value": tag, "weight": 1}]}
    with pytest.raises(mq.MogMeConfigError):
        flow(policy_over={"mog_me": {**load().raw["mog_me"], "traits": traits}})


TRAITS = load().raw["mog_me"]["traits"]


@pytest.mark.parametrize("bad", [
    None, {}, {"frame_text": TRAITS["frame_text"]},
    {**TRAITS, "extra": []},
    {**TRAITS, "frame_text": []},
    {**TRAITS, "frame_text": [{"value": "SUPREME", "weight": 1}]},              # not owner-approved
    {**TRAITS, "frame_text": [{"value": "PIT VIPER", "weight": -1}]},
    {**TRAITS, "frame_text": [{"value": "PIT VIPER", "weight": 0}]},             # sums to 0
    {**TRAITS, "caption": [{"value": "GET MOGGED", "weight": 1.5}]},
    {**TRAITS, "caption": [{"value": "GET MOGGED"}]},
])
def test_bad_trait_config_refuses_boot(bad):
    with pytest.raises(mq.MogMeConfigError):
        mq.validate_traits(bad)


def test_policy_traits_and_weighted_draw():
    t = mq.validate_traits(TRAITS)
    assert t == {"frame_text": [("PIT VIPER", 1), (None, 1)],
                 "caption": [("GET MOGGED", 1), ("MOGGED BY BRAINROT LABS", 1), (None, 1)]}
    import random
    rng = random.Random(3)
    draws = [mq.draw_traits(t, rng) for _ in range(300)]
    assert {d["frame_text"] for d in draws} == {"PIT VIPER", None}
    assert {d["caption"] for d in draws} == {"GET MOGGED", "MOGGED BY BRAINROT LABS", None}
    plain = sum(1 for d in draws if d["frame_text"] is None and d["caption"] is None)
    assert 20 < plain < 80                                          # ~1/6 fully plain glasses
    only_pv = mq.validate_traits({**TRAITS, "frame_text": [{"value": "PIT VIPER", "weight": 1},
                                                           {"value": None, "weight": 0}]})
    assert all(mq.draw_traits(only_pv, rng)["frame_text"] == "PIT VIPER" for _ in range(20))


class Fixed:
    """First draw returns the given traits; any later draw flips them, so a redraw shows up."""
    def __init__(self, frame, caption): self.v, self.n = {"frame": frame, "caption": caption}, 0
    def choices(self, seq, weights=None):
        self.n += 1
        want = self.v["frame"] if "PIT VIPER" in seq else self.v["caption"]
        if self.n > 2:
            want = next(x for x in seq if x != want)
        return [want]


def test_traits_drawn_once_per_mog_recorded_and_sent_to_editor():
    for frame, caption in ((None, None), ("PIT VIPER", "GET MOGGED"), (None, "MOGGED BY BRAINROT LABS")):
        ed = SameEditor(succeed_on=2)                                     # one retry: traits must not change
        store = Store(clock=Clock()); store.put("kill", True)
        f = mq.MogMeFlow(store, load(), X(), ed, tempfile.mkdtemp(), fetch=Fetch(), clock=Clock(),
                         rng=Fixed(frame, caption))
        assert f.handle(mention(1))[0] == "mog_me_queued"
        tr = f.get("1")["traits"]
        assert (tr["frame_text"], tr["caption"]) == (frame, caption)
        assert len(tr["palette"]) >= 1 and all(c.startswith("#") and len(c) == 7 for c in tr["palette"])
        sent = [{k: v for k, v in t.items() if k != "placement"} for t in ed.traits_seen]
        assert sent[0] == sent[1] == tr and tr["pose"] is None
        raw = Path(f.get("1")["image_path"]).read_bytes()
        assert (raw == png((512, 512), (250, 250, 250))) is (caption is None)


def test_prompt_carries_frame_text_palette_and_fit_check():
    pal = ["#5a5ac8", "#ff2e93", "#f0f0f0"]
    plain = mq.compose_prompt({"frame_text": None, "palette": pal})
    assert plain.startswith(mq.VIPERS_PROMPT) and "#5a5ac8, #ff2e93, #f0f0f0" in plain
    assert mq.PLACEMENT_PROMPT in plain and "PIT VIPER" not in plain
    pv = mq.compose_prompt({"frame_text": "PIT VIPER", "palette": pal})
    assert pv.startswith(mq.VIPERS_LOGO_PROMPT) and mq.NO_TEXT_SENTENCE not in pv
    gm = mq.compose_prompt({"frame_text": "GET MOGGED", "palette": []})
    assert "Put the GET MOGGED lettering" in gm and "Colour match" not in gm
    assert mq.refs_prompt({"frame_text": None}) == mq.REFS_PROMPT
    assert mq.refs_prompt({"frame_text": "PIT VIPER"}) == mq.REFS_LOGO_PROMPT
    assert "the GET MOGGED lettering" in mq.refs_prompt({"frame_text": "GET MOGGED"})


def test_gemini_request_uses_the_mogs_traits():
    import base64
    sent = []

    def post(url, json, timeout, headers):
        sent.append(json["contents"][0]["parts"]); return 200, {"candidates": [{"content": {"parts": [
            {"inlineData": {"data": base64.b64encode(png()).decode()}}]}}]}
    ed = mq.GeminiEditor("fixture-only", post=post, refs=[png((64, 64))])
    tr = {"frame_text": "PIT VIPER", "caption": None, "palette": ["#112233"]}
    ed.edit_pfp(jpeg(), traits=tr)
    assert sent[0][0]["text"] == mq.compose_prompt(tr) and sent[0][2]["text"] == mq.REFS_LOGO_PROMPT


def test_palette_of_picks_the_pictures_own_colours():
    im = Image.new("RGB", (200, 200), (20, 120, 40))
    Image.Image.paste(im, (230, 30, 30), (0, 0, 200, 60))
    b = io.BytesIO(); im.save(b, "PNG")
    pal = [tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) for h in mq.palette_of(b.getvalue())]
    assert len(pal) == 2
    assert any(abs(c[0] - 20) + abs(c[1] - 120) + abs(c[2] - 40) < 30 for c in pal)
    assert any(abs(c[0] - 230) + abs(c[1] - 30) + abs(c[2] - 30) < 30 for c in pal)


def test_caption_coloured_from_palette():
    out = mq.add_tag(png((512, 512), (0, 0, 0)), "GET MOGGED", ["#1e5028", "#f0e650"])
    img = Image.open(io.BytesIO(out)).convert("RGB")
    corner = {img.getpixel((x, y)) for x in range(380, 510, 3) for y in range(470, 505, 3)}
    from collections import Counter
    fill = Counter(img.getpixel((x, y)) for x in range(380, 510, 2) for y in range(470, 505, 2)).most_common(1)[0][0]
    assert fill == (30, 80, 40)                                         # box = darkest picture colour
    assert any(abs(c[0] - 240) + abs(c[1] - 230) + abs(c[2] - 80) < 60 for c in corner)   # ink = contrast colour


def test_tag_stays_in_bottom_corner_off_center():
    out = mq.add_tag(png((512, 512), (0, 0, 0)), "MOGGED BY BRAINROT LABS")
    img = Image.open(io.BytesIO(out)).convert("RGB")
    for y in range(0, 512 - 512 // 12):                                   # nothing above the bottom ~8%
        for x in range(0, 512, 8):
            assert img.getpixel((x, y)) == (0, 0, 0)
    assert mq.add_tag(png(), None) == png()


def test_repo_style_refs_load_and_prompt_forbids_copying_text():
    refs = mq.load_refs(str(Path(__file__).resolve().parents[1] / "mogme_refs"))
    assert len(refs) == 2 and all(mq._sniff(r) == "image/jpeg" for r in refs)
    t = mq.REFS_PROMPT
    assert "Do NOT copy any text, letters, words, logos or brand names" in t
    assert "never copy their faces" in t


@pytest.mark.parametrize("budget,ceiling,ok", [("5.00", "10.00", True), ("10.00", "10.00", True),
                                               ("10.01", "10.00", False), ("12.00", "12.00", False),
                                               ("5.00", None, False)])
def test_budget_ceiling_ten_dollars(budget, ceiling, ok):
    cfg = {**load().raw["mog_me"], "daily_budget_usd": budget}
    if ceiling is None:
        cfg.pop("daily_budget_max_usd")
    else:
        cfg["daily_budget_max_usd"] = ceiling
    p = load(overrides={"mog_me": cfg})
    if ok:
        assert mq.mogme_budget(p) == Decimal(budget)
    else:
        with pytest.raises(mq.MogMeConfigError):
            mq.mogme_budget(p)
        with pytest.raises(mq.MogMeConfigError):
            flow(policy_over={"mog_me": cfg})


def test_policy_ships_five_with_ten_ceiling_and_logs_budget_hits():
    raw = load().raw["mog_me"]
    assert (raw["daily_budget_usd"], raw["daily_budget_max_usd"]) == ("5.00", "10.00")
    f, store = flow()
    store.put(f"mogme_spend:{f.day()}", "4.99")
    assert f.handle(mention(1)) == ("ignored", "budget")
    assert store.ledger("mogme_budget_hit")


def test_only_named_approvers_can_decide():
    assert load().raw["mog_me"]["approvers"] == ["sneaks", "muse"]
    f, _ = flow(); f.handle(mention(1)); f.handle(mention(2, author="222"))
    for who in ("random", "codex", "claude-code"):
        with pytest.raises(mq.MogMeError):
            f.approve("1", who, "looks good")
    assert f.approve("1", "Muse", "clean fit")["approved_by"] == "Muse"
    assert f.reject("2", "sneaks", "off the eyes")["status"] == mq.REJECTED
    for bad in ([], None, [""], "muse"):
        with pytest.raises(mq.MogMeConfigError):
            flow(policy_over={"mog_me": {**load().raw["mog_me"], "approvers": bad}})
