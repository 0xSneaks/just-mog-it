"""Gate 3: inspect the raw create-transaction bytes before anything is signed.
Pins come from the two real launches (SA + TREMP, 2026-11-01), read from mainnet RPC 2026-11-04:
one instruction to Meteora DBC, discriminator 8c55d7b06636684f, 16 accounts, fixed accounts below."""
import base64

PROGRAM_ID = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
CREATE_DISCRIMINATOR = bytes.fromhex("8c55d7b06636684f")
N_ACCOUNTS = 16
LAUNCH_WALLET = "2LYQUArYnaYR2YTePK99mUiYU5mFXn1qZqe5b3xGsvHt"
PINNED = {  # instruction account index -> required pubkey (identical in both real txs)
    0: "B646J17uWbihX7GtAW3PJW7iNFFiHCTef2XfAWQBQgZW",
    1: "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM",
    2: LAUNCH_WALLET,
    4: "So11111111111111111111111111111111111111112",
    9: "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
    10: LAUNCH_WALLET,
    11: "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    12: "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    13: "11111111111111111111111111111111",
    14: "8Ks12pbrD6PXxfty1hVQiE9sc289zgU1zHkvXhrSdriF",
    15: PROGRAM_ID,
}
MINT_INDEX = 3
_A = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

class GuardError(Exception):
    pass

def b58(b):
    n = int.from_bytes(b, "big"); s = ""
    while n:
        n, r = divmod(n, 58); s = _A[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s

def _cu16(buf, i):
    v = s = 0
    while True:
        if i >= len(buf): raise GuardError("truncated")
        c = buf[i]; i += 1; v |= (c & 0x7F) << s; s += 7
        if not c & 0x80: return v, i

def parse(raw):
    """Parse a legacy Solana transaction (signatures + message). v0 is refused."""
    n, i = _cu16(raw, 0); i += 64 * n
    if i >= len(raw): raise GuardError("truncated")
    if raw[i] & 0x80: raise GuardError("versioned tx refused: real launches are legacy")
    nreq, _, _ = raw[i], raw[i+1], raw[i+2]; i += 3
    nk, i = _cu16(raw, i)
    keys = [b58(raw[i+32*k:i+32*k+32]) for k in range(nk)]; i += 32 * nk + 32  # + blockhash
    nix, i = _cu16(raw, i); ixs = []
    for _ in range(nix):
        p = raw[i]; i += 1
        na, i = _cu16(raw, i); acc = list(raw[i:i+na]); i += na
        nd, i = _cu16(raw, i); data = bytes(raw[i:i+nd]); i += nd
        if p >= nk or any(a >= nk for a in acc): raise GuardError("index out of range")
        ixs.append({"program": keys[p], "accounts": [keys[a] for a in acc], "data": data})
    if i != len(raw): raise GuardError("trailing bytes")
    return {"signers": keys[:nreq], "fee_payer": keys[0], "instructions": ixs, "keys": keys}

def inspect(raw, expected_mint=None):
    if isinstance(raw, str): raw = base64.b64decode(raw)
    try:
        tx = parse(raw)
    except (IndexError, ValueError) as e:
        raise GuardError(f"malformed tx bytes: {e}")
    if len(tx["instructions"]) != 1:
        raise GuardError(f"expected 1 instruction (no bundled dev buy), got {len(tx['instructions'])}")
    ix = tx["instructions"][0]
    if ix["program"] != PROGRAM_ID: raise GuardError(f"program {ix['program']} not pinned")
    if ix["data"][:8] != CREATE_DISCRIMINATOR: raise GuardError("not the pinned create instruction")
    if len(ix["accounts"]) != N_ACCOUNTS: raise GuardError("account count mismatch")
    for idx, key in PINNED.items():
        if ix["accounts"][idx] != key: raise GuardError(f"account {idx} {ix['accounts'][idx]} != pinned {key}")
    if tx["fee_payer"] != LAUNCH_WALLET: raise GuardError("fee payer is not the launch wallet")
    mint = ix["accounts"][MINT_INDEX]
    if mint not in tx["signers"]: raise GuardError("mint is not a signer")
    if expected_mint and mint != expected_mint: raise GuardError("mint differs from quoted mint")
    if set(tx["signers"]) - {LAUNCH_WALLET, mint}: raise GuardError("unexpected signer")
    return {"mint": mint, "fee_payer": tx["fee_payer"]}
