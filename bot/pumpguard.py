"""Gate 3 for pump.fun: inspect the raw create-tx bytes from PumpPortal BEFORE the vault signs.
Strict by design: anything unrecognized is refused (a false refusal is safe; a false accept
is not).

Layouts are from the public pump.fun IDL (program 6EF8rr..., create disc 181ec828051c0777,
args name/symbol/uri/creator; buy disc 66063d1201daebea, args amount u64, max_sol_cost u64).
INFERENCE until pinned against a REAL PumpPortal create tx — add that tx as a fixture in
tests/fixtures/ and a test before any live use. create_v2 (Token-2022 layout) is refused."""
import base64
from urllib.parse import urlparse

from mogit.guard import GuardError, _borsh_str, _cu16, b58

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
CREATE = bytes.fromhex("181ec828051c0777")
CREATE_V2 = bytes.fromhex("d6904cec5f8b31b4")
BUY = bytes.fromhex("66063d1201daebea")
URI_HOSTS = frozenset({"ipfs.io", "gateway.pinata.cloud"})
ALLOWED_PROGRAMS = {PUMP, COMPUTE_BUDGET, ATA}

def parse(raw):
    """Legacy or v0 message. v0 with address-lookup tables is refused (keys not verifiable)."""
    n, i = _cu16(raw, 0)
    i += 64 * n
    if i >= len(raw):
        raise GuardError("truncated")
    version = None
    if raw[i] & 0x80:
        version = raw[i] & 0x7F
        if version != 0:
            raise GuardError(f"unsupported tx version {version}")
        i += 1
    nreq, nro_s, nro_u = raw[i], raw[i + 1], raw[i + 2]
    i += 3
    nk, i = _cu16(raw, i)
    keys = [b58(raw[i + 32 * k:i + 32 * k + 32]) for k in range(nk)]
    i += 32 * nk
    blockhash = b58(raw[i:i + 32]); i += 32
    nix, i = _cu16(raw, i)
    ixs = []
    for _ in range(nix):
        p = raw[i]; i += 1
        na, i = _cu16(raw, i); acc = list(raw[i:i + na]); i += na
        nd, i = _cu16(raw, i); data = bytes(raw[i:i + nd]); i += nd
        if p >= nk or any(a >= nk for a in acc):
            raise GuardError("account index out of range")
        ixs.append({"program": keys[p], "accounts": [keys[a] for a in acc], "data": data})
    if version == 0:
        nlt, i = _cu16(raw, i)
        if nlt:
            raise GuardError("address lookup tables refused")
    if i != len(raw):
        raise GuardError("trailing bytes")
    return {"num_signatures": n, "header": (nreq, nro_s, nro_u), "keys": keys, "signers": keys[:nreq],
            "fee_payer": keys[0] if keys else None, "instructions": ixs, "blockhash": blockhash}

def _u32(d, i): return int.from_bytes(d[i:i + 4], "little")
def _u64(d, i): return int.from_bytes(d[i:i + 8], "little")

def priority_lamports(ixs):
    price = limit = None
    for ix in ixs:
        if ix["program"] != COMPUTE_BUDGET:
            continue
        d = ix["data"]
        if d[:1] == b"\x02" and len(d) == 5:
            limit = _u32(d, 1)
        elif d[:1] == b"\x03" and len(d) == 9:
            price = _u64(d, 1)
        else:
            raise GuardError("unknown compute-budget instruction")
    if price is None:
        return 0
    limit = limit if limit is not None else 200_000 * len(ixs)
    return price * limit // 1_000_000

def inspect(raw, *, bot_wallet, expected_mint, expected_name, expected_symbol,
            dev_buy_lamports=0, priority_cap_lamports=5_000_000, uri_hosts=URI_HOSTS):
    if isinstance(raw, str):
        raw = base64.b64decode(raw)
    if dev_buy_lamports != 0:
        raise GuardError("dev buy must be 0 (owner §0.7)")
    try:
        tx = parse(bytes(raw))
    except (IndexError, ValueError) as e:
        raise GuardError(f"malformed tx bytes: {e}")
    if tx["num_signatures"] != tx["header"][0] or tx["header"][0] != 2:
        raise GuardError("expected exactly 2 signers (bot wallet + mint)")
    if tx["fee_payer"] != bot_wallet:
        raise GuardError("fee payer is not the bot wallet")
    if set(tx["signers"]) != {bot_wallet, expected_mint}:
        raise GuardError("signers must be exactly the bot wallet and the expected mint")
    creates = []
    for ix in tx["instructions"]:
        if ix["program"] not in ALLOWED_PROGRAMS:
            raise GuardError(f"program {ix['program']} not allowed")
        if ix["program"] != PUMP:
            continue
        disc = ix["data"][:8]
        if disc == CREATE:
            creates.append(ix)
        elif disc == CREATE_V2:
            raise GuardError("create_v2 layout not pinned — refuse until a real fixture is added")
        elif disc == BUY:
            if len(ix["data"]) < 24 or _u64(ix["data"], 16) != 0:
                raise GuardError("buy with SOL spend refused (dev buy must be 0)")
        else:
            raise GuardError("unknown pump instruction")
    if len(creates) != 1:
        raise GuardError(f"expected exactly 1 create, got {len(creates)}")
    ix = creates[0]
    name, i = _borsh_str(ix["data"], 8)
    symbol, i = _borsh_str(ix["data"], i)
    uri, i = _borsh_str(ix["data"], i)
    if i + 32 != len(ix["data"]):
        raise GuardError("create args do not end with exactly one creator pubkey")
    creator = b58(ix["data"][i:i + 32])
    if creator != bot_wallet:
        raise GuardError("creator is not the bot wallet — creator fees would go elsewhere")
    if not ix["accounts"] or ix["accounts"][0] != expected_mint:
        raise GuardError("create mint account != expected mint")
    if name != expected_name:
        raise GuardError(f"tx name {name!r} != requested {expected_name!r}")
    if symbol != expected_symbol:
        raise GuardError(f"tx symbol {symbol!r} != requested {expected_symbol!r}")
    if urlparse(uri).scheme != "https" or urlparse(uri).hostname not in uri_hosts:
        raise GuardError(f"metadata uri host not allowlisted: {uri!r}")
    fee = priority_lamports(tx["instructions"])
    if fee > priority_cap_lamports:
        raise GuardError(f"priority fee {fee} > cap {priority_cap_lamports}")
    return {"mint": expected_mint, "name": name, "symbol": symbol, "uri": uri, "creator": creator,
            "priority_lamports": fee, "blockhash": tx["blockhash"]}

class PumpGuard:
    """Adapter the engine calls before the vault signs anything."""
    def __init__(self, bot_wallet, priority_cap_lamports):
        self.wallet, self.cap = bot_wallet, int(priority_cap_lamports)

    def inspect(self, tx, *, expected_mint, expected_name, expected_symbol, dev_buy_lamports=0):
        return inspect(tx, bot_wallet=self.wallet, expected_mint=expected_mint, expected_name=expected_name,
                       expected_symbol=expected_symbol, dev_buy_lamports=dev_buy_lamports,
                       priority_cap_lamports=self.cap)

    def inspect_claim(self, tx):
        from .live.pumpportal import inspect_claim
        return inspect_claim(tx, self.wallet)
