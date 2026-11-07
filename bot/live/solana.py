"""Solana JSON-RPC adapter: simulate (payer lamport delta), dual-RPC send, signature status,
blockhash expiry, balances, PDA derivation. Endpoints from env JMI_RPC_URLS (comma list)."""
import base64
import hashlib
import time

from mogit.guard import b58
from ..parse import b58decode
from ..pumpguard import parse as parse_tx
from .http import Http, env

# --- ed25519 on-curve check (for PDAs) -------------------------------------------
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)

def on_curve(b):
    y = int.from_bytes(b, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    if y >= _P:
        return False
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return not sign
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    return (x * x - x2) % _P == 0

def find_pda(seeds, program_id):
    prog = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58(h), bump
    raise ValueError("no PDA")

class Rpc:
    def __init__(self, http=None, urls=None, sleep=time.sleep, clock=time.time):
        self.http = http or Http(timeout=8)
        self.urls = urls or [u.strip() for u in env("JMI_RPC_URLS").split(",") if u.strip()]
        self.sleep, self.clock = sleep, clock
        self._blockhash = {}

    def call(self, method, params, url=None):
        r = self.http.json("POST", url or self.urls[0],
                           json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in r:
            raise RuntimeError(f"rpc {method}: {r['error']}")
        return r["result"]

    def balance(self, addr):
        return int(self.call("getBalance", [addr, {"commitment": "confirmed"}])["value"])

    def simulate(self, tx, payer=None):
        raw = tx if isinstance(tx, (bytes, bytearray)) else base64.b64decode(tx)
        payer = payer or parse_tx(bytes(raw))["fee_payer"]
        pre = self.balance(payer)
        v = self.call("simulateTransaction", [base64.b64encode(raw).decode(), {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
            "commitment": "confirmed", "accounts": {"encoding": "base64", "addresses": [payer]}}])["value"]
        if v.get("err") is not None:
            return {"ok": False, "error": v["err"], "logs": v.get("logs")}
        post = int(v["accounts"][0]["lamports"])
        return {"ok": True, "payer_lamports_delta": pre - post, "units": v.get("unitsConsumed")}

    def send(self, signed):
        raw = signed if isinstance(signed, (bytes, bytearray)) else base64.b64decode(signed)
        self._blockhash[b58(bytes(raw)[1:65])] = parse_tx(bytes(raw))["blockhash"]
        b64, sigs, errs = base64.b64encode(raw).decode(), [], []
        for u in self.urls:                                       # dual-RPC broadcast (§8)
            try:
                sigs.append(self.call("sendTransaction", [b64, {"encoding": "base64", "skipPreflight": True,
                                                                "maxRetries": 0}], url=u))
            except Exception as e:
                errs.append(str(e))
        if not sigs:
            raise RuntimeError(f"all RPCs refused send: {errs}")
        return sigs[0]

    def status(self, sig, timeout_s=30):
        deadline = self.clock() + timeout_s
        while True:
            v = self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])["value"][0]
            if v:
                if v.get("err") is not None:
                    return "failed"
                if v.get("confirmationStatus") in ("confirmed", "finalized"):
                    return v["confirmationStatus"]
            if self.clock() >= deadline:
                return None
            self.sleep(0.4)

    def blockhash_expired(self, sig):
        """Unknown blockhash (e.g. after a restart) answers False: wait, never rebuild blind."""
        bh = self._blockhash.get(sig)
        if not bh:
            return False
        return self.call("isBlockhashValid", [bh, {"commitment": "processed"}])["value"] is False
