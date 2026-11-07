"""Mogz callout step. Mocked HTTP and RPC only — the real endpoint is never called."""
from unittest.mock import Mock

import pytest

from bot import callout
from bot.callout import (APPROVED, AWAITING_APPROVAL, CONFIRMED, FAILED, POSTED, REFUSED,
                         CalloutError, CalloutFlow, launch_done)

MINT = "3cHj1iCgZb18Q8ZrqbsAtwNGjn6gifQRqnCedHhdmogz"
WALLET = "2LYQUArYnaYR2YTePK99mUiYU5mFXn1qZqe5b3xGsvHt"
OTHER = "So11111111111111111111111111111111111111112"


def acct(mint, amount):
    return {"account": {"data": {"parsed": {"info": {"mint": mint, "tokenAmount": {"amount": str(amount)}}}}}}


def rpc_with(spl=(), t22=()):
    def rpc(method, params):
        assert method == "getTokenAccountsByOwner"
        assert params[0] == WALLET
        prog = params[1]["programId"]
        return {"value": list(spl if prog == callout.SPL_TOKEN else t22)}
    return Mock(side_effect=rpc)


def http_ok(confirm=True):
    def http(method, url, json=None, timeout=None):
        assert timeout == 10
        if method == "POST":
            assert url == "https://mogz.fun/api/callouts"
            assert set(json) == {"mint", "text", "image", "link"}
            http.sent = json
            return 201, {"ok": True}
        assert (method, url) == ("GET", f"https://mogz.fun/api/coins/{MINT}/callouts")
        return 200, ([{"mint": MINT, "text": http.sent["text"]}] if confirm else [])
    return Mock(side_effect=http)


def approved(flow):
    item = flow.prepare(mint=MINT, ticker="JOSE", old_quote="MUSK4",
                        image="https://ipfs.io/ipfs/x", link=f"https://mogz.fun/coin/{MINT}")
    return flow.approve(item, "sneaks")


def test_default_stops_at_awaiting_approval_without_network():
    http, rpc = Mock(), Mock()
    item = CalloutFlow(WALLET, http=http, rpc=rpc).prepare(mint=MINT, ticker="JOSE", old_quote="MUSK4")
    assert item["state"] == AWAITING_APPROVAL
    http.assert_not_called(); rpc.assert_not_called()
    assert not launch_done(item)


def test_post_without_approval_refused():
    flow = CalloutFlow(WALLET, http=Mock(), rpc=Mock())
    item = flow.prepare(mint=MINT, ticker="JOSE", old_quote="MUSK4")
    with pytest.raises(CalloutError):
        flow.post(item)
    with pytest.raises(CalloutError):
        flow.approve(item, "  ")
    flow.http.assert_not_called(); flow.rpc.assert_not_called()


def test_happy_path_confirmed_only_after_get():
    http = http_ok()
    flow = CalloutFlow(WALLET, http=http, rpc=rpc_with(spl=[acct(OTHER, 5)]))
    item = flow.post(approved(flow))
    assert item["state"] == CONFIRMED and launch_done(item)
    assert [c.args[0] for c in http.call_args_list] == ["POST", "GET"]
    assert flow.rpc.call_count == 2  # SPL Token and Token-2022 both read


def test_unconfirmed_get_means_not_done():
    flow = CalloutFlow(WALLET, http=http_ok(confirm=False), rpc=rpc_with())
    item = flow.post(approved(flow))
    assert item["state"] == POSTED and not launch_done(item)


@pytest.mark.parametrize("spl,t22", [([acct(MINT, 1)], []), ([], [acct(MINT, 7)])])
def test_guard_refuses_when_wallet_holds_mint_spl_or_token2022(spl, t22):
    http = Mock()
    flow = CalloutFlow(WALLET, http=http, rpc=rpc_with(spl, t22))
    item = flow.post(approved(flow))
    assert item["state"] == REFUSED and "holds this mint" in item["refusal"]
    http.assert_not_called()


@pytest.mark.parametrize("rpc", [
    Mock(side_effect=TimeoutError("rpc")),
    Mock(return_value={"unexpected": True}),
    Mock(return_value={"value": [{"account": {"data": "base64-not-parsed"}}]}),
])
def test_guard_refuses_when_balance_read_fails(rpc):
    http = Mock()
    flow = CalloutFlow(WALLET, http=http, rpc=rpc)
    item = flow.post(approved(flow))
    assert item["state"] == REFUSED and "balance guard" in item["refusal"]
    http.assert_not_called()


def test_post_http_error_is_failed_not_done():
    flow = CalloutFlow(WALLET, http=Mock(return_value=(500, {})), rpc=rpc_with())
    item = flow.post(approved(flow))
    assert item["state"] == FAILED and not launch_done(item)


def test_defaults_are_unwired():
    flow = CalloutFlow(WALLET)
    item = flow.post(approved(flow))
    assert item["state"] == REFUSED  # unwired RPC = failed read = refuse


def test_story_copy_and_sanitizing():
    text = callout.story_text("JO‮SE\x00<script>", "MUSK4\n", MINT)
    assert text == f"$JOSEscript was stuck on a MUSK4 pair. JustMogIt relaunched it on a SOL pair. New CA: {MINT}"
    assert len(text) <= 280


@pytest.mark.parametrize("bad", ["http://x.io/a.png", "javascript:alert(1)", "ftp://x"])
def test_non_https_image_or_link_refused(bad):
    flow = CalloutFlow(WALLET)
    with pytest.raises(CalloutError):
        flow.prepare(mint=MINT, ticker="JOSE", old_quote="MUSK4", image=bad)
    with pytest.raises(CalloutError):
        flow.prepare(mint=MINT, ticker="JOSE", old_quote="MUSK4", link=bad)


def test_invalid_mint_refused():
    with pytest.raises(CalloutError):
        CalloutFlow(WALLET).prepare(mint="not a mint", ticker="JOSE", old_quote="MUSK4")
