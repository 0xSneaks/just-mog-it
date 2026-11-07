"""0.02 SOL launch-fee verifier (spec §0.3 rail). Requester sends SOL to the bot's fee
address with memo = job_id. We accept only a confirmed, successful SystemProgram transfer
of >= fee lamports to that address in a tx whose memo is exactly the job_id, and each
payment signature can pay for one job only (no replay)."""
MEMO_PROGRAMS = {"MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"}

class Payments:
    def __init__(self, rpc, fee_address, used=lambda sig: False):
        self.rpc, self.addr, self.used = rpc, fee_address, used

    def job_address(self, job_id):
        return self.addr

    def verify(self, job_id, lamports):
        sigs = self.rpc.call("getSignaturesForAddress", [self.addr, {"limit": 100, "commitment": "confirmed"}])
        for s in sigs:
            if s.get("err") is not None or job_id not in (s.get("memo") or "") or self.used(s["signature"]):
                continue
            tx = self.rpc.call("getTransaction", [s["signature"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                                  "maxSupportedTransactionVersion": 0}])
            if not tx or (tx.get("meta") or {}).get("err") is not None:
                continue
            ixs = tx["transaction"]["message"]["instructions"]
            memo_ok = any(ix.get("programId") in MEMO_PROGRAMS and ix.get("parsed") == job_id for ix in ixs)
            paid = [ix["parsed"]["info"] for ix in ixs
                    if ix.get("program") == "system" and (ix.get("parsed") or {}).get("type") == "transfer"
                    and ix["parsed"]["info"].get("destination") == self.addr]
            if memo_ok and paid and sum(int(p["lamports"]) for p in paid) >= int(lamports):
                return {"ok": True, "signature": s["signature"], "payer": paid[0]["source"]}
        return None
