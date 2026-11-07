"""pump.fun via PumpPortal Local Transaction API (pumpportal.fun/api/trade-local):
returns UNSIGNED tx bytes; our vault signs. Metadata goes to Pinata (JWT env JMI_PINATA_JWT),
since PumpPortal says pump.fun/api/ipfs is no longer supported.

Creator fees on pump.fun pool in ONE creator vault per creator and are claimed all at once
(PumpPortal docs) — so per-coin attribution is done by the engine from volume weights."""
import base64
import json

from mogit.guard import GuardError
from ..pumpguard import COMPUTE_BUDGET, PUMP, parse as parse_tx
from .http import Http, env
from .solana import find_pda

TRADE_LOCAL = "https://pumpportal.fun/api/trade-local"
PINATA = "https://uploads.pinata.cloud/v3/files"
COLLECT_CREATOR_FEE = bytes.fromhex("1416567bc61cdb84")   # sha256("global:collect_creator_fee")[:8]
RENT_EXEMPT_ZERO = 890_880                                 # lamports, 0-byte account

class PumpPortal:
    def __init__(self, bot_wallet, rpc, http=None, pinata_jwt=None, slippage=10):
        self.wallet, self.rpc, self.http = bot_wallet, rpc, http or Http(timeout=15)
        self._jwt, self.slippage = pinata_jwt, slippage

    def _pin(self, filename, data, ctype):
        r = self.http.json("POST", PINATA, headers={"Authorization": f"Bearer {self._jwt or env('JMI_PINATA_JWT')}"},
                           form={"network": "public", "file": (filename, data, ctype)})
        return f"https://ipfs.io/ipfs/{r['data']['cid']}"

    def upload_metadata(self, *, name, symbol, description, image_bytes, image_type, socials):
        image = self._pin("image", image_bytes, image_type)
        meta = {"name": name, "symbol": symbol, "description": description, "image": image,
                **{k: socials.get(k, "") for k in ("twitter", "telegram", "website")}}
        return self._pin("metadata.json", json.dumps(meta).encode(), "application/json")

    def build_create(self, *, name, symbol, description, image_url, socials, mint, dev_buy_sol,
                     priority_cap_sol, image_fetch=None):
        if str(dev_buy_sol) not in ("0", "0.0"):
            raise ValueError("dev buy must be 0")
        img = (image_fetch or (lambda u: self.http.request("GET", u)))(image_url)
        uri = self.upload_metadata(name=name, symbol=symbol, description=description, image_bytes=img,
                                   image_type="image/png", socials=socials or {})
        return self.http.request("POST", TRADE_LOCAL, json_body={
            "publicKey": self.wallet, "action": "create",
            "tokenMetadata": {"name": name, "symbol": symbol, "uri": uri},
            "mint": mint, "denominatedInSol": "true", "amount": 0, "slippage": self.slippage,
            "priorityFee": float(priority_cap_sol) / 5, "pool": "pump"})

    # ---- creator-fee loop --------------------------------------------------------
    def creator_vault(self):
        from ..parse import b58decode
        return find_pda([b"creator-vault", b58decode(self.wallet)], PUMP)[0]   # INFERENCE: seeds per IDL helpers

    def claimable(self):
        return max(0, self.rpc.balance(self.creator_vault()) - RENT_EXEMPT_ZERO)

    def claim_cost(self):
        return 10_000

    def build_claim(self):
        return self.http.request("POST", TRADE_LOCAL, json_body={
            "publicKey": self.wallet, "action": "collectCreatorFee", "priorityFee": 0.000001})

def inspect_claim(raw, bot_wallet):
    """Claim tx guard: only compute-budget + pump collect_creator_fee, bot wallet sole signer."""
    raw = raw if isinstance(raw, (bytes, bytearray)) else base64.b64decode(raw)
    tx = parse_tx(bytes(raw))
    if tx["header"][0] != 1 or tx["fee_payer"] != bot_wallet:
        raise GuardError("claim must have the bot wallet as sole signer and fee payer")
    n = 0
    for ix in tx["instructions"]:
        if ix["program"] == COMPUTE_BUDGET:
            continue
        if ix["program"] != PUMP or ix["data"][:8] != COLLECT_CREATOR_FEE:
            raise GuardError("claim tx contains a non-claim instruction")
        n += 1
    if n != 1:
        raise GuardError("expected exactly one collect_creator_fee")
    return True
