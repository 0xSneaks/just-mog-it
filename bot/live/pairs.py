"""Read-only Pump account resolution. Layouts pinned to pump-public-docs 639b87f."""
import base64
from dataclasses import dataclass

from mogit.guard import b58

from ..parse import b58decode, is_pubkey
from ..pumpguard import CREATE_V2, PUMP
from .solana import find_pda

PUMP_SWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WRAPPED_SOL = "So11111111111111111111111111111111111111112"
SOL_SENTINEL = "11111111111111111111111111111111"
GLOBAL = bytes.fromhex("a7e8e8b1c86c727f")
QUOTE_CONTROL = bytes.fromhex("38f423eec1d5a2c9")
CURVE = bytes.fromhex("17b7f83760d8ac60")
POOL = bytes.fromhex("f19a6d0411b16dbc")
FINALIZED = {"commitment": "finalized", "encoding": "base64"}


class PairDataError(ValueError):
    pass


def normalize_sol(mint):
    return WRAPPED_SOL if mint == SOL_SENTINEL else mint


def _account(account, owner, *, discriminator=b"", minimum=0):
    if not account or account.get("owner") != owner or account.get("executable"):
        raise PairDataError("wrong or missing account owner")
    try:
        encoded, encoding = account["data"]
        if encoding != "base64":
            raise PairDataError("expected base64 account data")
        raw = base64.b64decode(encoded, validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise PairDataError("invalid account data") from exc
    if len(raw) < minimum or not raw.startswith(discriminator):
        raise PairDataError("invalid account layout")
    return raw


def _u64(raw, offset):
    return int.from_bytes(raw[offset:offset + 8], "little")


@dataclass(frozen=True)
class QuoteMint:
    mint: str
    source: str
    initial_virtual_quote_reserves: int


@dataclass(frozen=True)
class ResolvedPair:
    base_mint: str
    quote_mint: str
    pool: str
    venue: str
    quote_class: str
    real_quote_reserves: int | None


class QuoteRegistry:
    def __init__(self, rpc):
        self.rpc = rpc

    def _fetch_batch(self):
        addresses = [find_pda([seed], PUMP)[0] for seed in (b"global", b"quote-control")]
        return self.rpc.call("getMultipleAccounts", [addresses, FINALIZED])

    def refresh(self):
        """Return a fresh snapshot. SOL, Global, then QuoteControl, deduped in that order."""
        result = self._fetch_batch()
        global_account, control_account = result["value"]
        global_raw = _account(global_account, PUMP, discriminator=GLOBAL, minimum=1045)
        if global_raw[8] != 1:
            raise PairDataError("uninitialized global account")
        entries = {WRAPPED_SOL: QuoteMint(WRAPPED_SOL, "sol", _u64(global_raw, 81))}
        for offset in range(1013, 1045, 32):
            mint = b58(global_raw[offset:offset + 32])
            if mint != SOL_SENTINEL:
                entries.setdefault(mint, QuoteMint(mint, "global", _u64(global_raw, 1005)))
        if control_account is not None:
            control = _account(control_account, PUMP, discriminator=QUOTE_CONTROL, minimum=108)
            count = int.from_bytes(control[104:108], "little")
            if 108 + count * 40 > len(control):
                raise PairDataError("truncated QuoteControl mint vector")
            for offset in range(108, 108 + count * 40, 40):
                mint = b58(control[offset:offset + 32])
                if mint == SOL_SENTINEL:
                    raise PairDataError("unexpected zero quote mint")
                entries.setdefault(mint, QuoteMint(mint, "quoteControl", _u64(control, offset + 32)))
        return tuple(entries.values())


class PumpPairs:
    def __init__(self, rpc):
        self.rpc = rpc

    def _get(self, address):
        return self.rpc.call("getAccountInfo", [address, FINALIZED])["value"]

    def _mint(self, address):
        if not is_pubkey(address):
            raise PairDataError("invalid mint address")
        mint_account = self._get(address)
        program = (mint_account or {}).get("owner")
        if program not in (SPL_TOKEN, TOKEN_2022):
            raise PairDataError("unsupported base mint program")
        mint = _account(mint_account, program, minimum=82)
        # An extended Token-2022 mint has account type Mint (1) at offset 165.
        if (len(mint) != 82 and
                (program != TOKEN_2022 or len(mint) < 166 or mint[165] != 1)) or mint[45] != 1:
            raise PairDataError("account is not an initialized mint")
        return program, mint[44]

    def quote_decimals(self, address):
        return self._mint(address)[1]

    def resolve_pair(self, base_mint):
        self._mint(base_mint)
        base = b58decode(base_mint)
        curve_address = find_pda([b"bonding-curve", base], PUMP)[0]
        curve = _account(self._get(curve_address), PUMP, discriminator=CURVE, minimum=81)
        if curve[48] not in (0, 1) or 83 < len(curve) < 115:
            raise PairDataError("invalid curve state")
        quote_mint = normalize_sol(b58(curve[83:115])) if len(curve) >= 115 else WRAPPED_SOL
        quote_class = "crypto" if quote_mint == WRAPPED_SOL else "unknown"
        if curve[48] == 0:
            return ResolvedPair(base_mint, quote_mint, curve_address, "bonding_curve",
                                quote_class, _u64(curve, 32))
        authority = find_pda([b"pool-authority", base], PUMP)[0]
        pool_address = find_pda(
            [b"pool", b"\0\0", b58decode(authority), base, b58decode(quote_mint)], PUMP_SWAP,
        )[0]
        pool = _account(self._get(pool_address), PUMP_SWAP, discriminator=POOL, minimum=203)
        if (pool[9:11] != b"\0\0" or b58(pool[11:43]) != authority
                or b58(pool[43:75]) != base_mint or b58(pool[75:107]) != quote_mint):
            raise PairDataError("canonical pool mint mismatch")
        return ResolvedPair(base_mint, quote_mint, pool_address, "pumpswap", quote_class, None)

    def verify_create(self, signature, expected_mint, expected_quote):
        """Return independently verified finalized create evidence, or fail closed."""
        if not is_pubkey(expected_mint) or not is_pubkey(expected_quote):
            return None
        try:
            status = self.rpc.call("getSignatureStatuses", [
                [signature], {"searchTransactionHistory": True},
            ])["value"][0]
            if not status or status["confirmationStatus"] != "finalized" or status["err"] is not None:
                return None
            receipt = self.rpc.call("getTransaction", [signature, {
                "encoding": "json", "commitment": "finalized", "maxSupportedTransactionVersion": 0,
            }])
            if (not receipt or receipt["meta"]["err"] is not None
                    or receipt["transaction"]["signatures"][0] != signature):
                return None
            msg = receipt["transaction"]["message"]
            loaded = receipt["meta"].get("loadedAddresses") or {}
            keys = msg["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
            creates = [
                [keys[index] for index in ix["accounts"]]
                for ix in msg["instructions"]
                if keys[ix["programIdIndex"]] == PUMP and b58decode(ix["data"]).startswith(CREATE_V2)
            ]
            if len(creates) != 1:
                return None
            accounts = creates[0]
            expected_curve = find_pda([b"bonding-curve", b58decode(expected_mint)], PUMP)[0]
            if (len(accounts) not in (16, 19) or accounts[0] != expected_mint
                    or accounts[2] != expected_curve
                    or accounts[7] != TOKEN_2022
                    or (accounts[16] if len(accounts) == 19 else WRAPPED_SOL) != expected_quote):
                return None
            if self._mint(expected_mint)[0] != TOKEN_2022:
                return None
            pair = self.resolve_pair(expected_mint)
            return pair if pair.quote_mint == expected_quote else None
        except (IndexError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            return None
