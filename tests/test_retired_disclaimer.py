"""Regression (owner ruling 2026-11-07): "Not the original dev. NFA." is retired.
Fails if "not the original dev" (any case) or a standalone "NFA" appears in any outbound
copy or metadata template. Rescue copy is story only."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import yaml

from bot import callout, metadata, replies
from bot.policy import load

ROOT = Path(__file__).resolve().parents[1]
RETIRED = re.compile(r"not the original dev|(?<![A-Za-z0-9])nfa(?![A-Za-z0-9])", re.I)
CA = "5NRzuqfyd4r7dHHKDpMY9HdGM2BMgSQhcZAbXTcfLoat"
NEW = "3cHj1iCgZb18Q8ZrqbsAtwNGjn6gifQRqnCedHhdmogz"


def _clean(text):
    assert not RETIRED.search(text or ""), text


def test_policy_yaml_templates():
    for key, value in yaml.safe_load((ROOT / "config" / "policy.yaml").read_text()).items():
        if isinstance(value, str):
            _clean(value)


def test_description_template_and_built_metadata():
    p = load()
    _clean(p.description_prefix)
    _clean(metadata.build_description(p, "JOSE", CA, "gm. Not the original dev. NFA. nfa"))


def test_every_reply_template():
    _clean(replies.launched(SimpleNamespace(ticker="JOSE"), NEW))
    _clean(replies.payment_request("job1", NEW, "0.02"))
    _clean(replies.wallet_request())
    for code, template in replies.REJECT.items():
        if template:
            _clean(replies.rejection(code, mint=NEW, cap=3))
    _clean(replies.mog_me_done("someone")); _clean(replies.mog_me_failed()); _clean(replies.alt_text("someone"))


def test_callout_copy():
    _clean(callout.story_text("JOSE", "MUSK4", NEW))


def test_launch_packs_and_metadata_fixtures():
    for path in list((ROOT / "packs").glob("*.json")):
        _clean(json.dumps(json.loads(path.read_text())))

