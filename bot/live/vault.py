"""Signer client. Keys live ONLY in the vault service (env JMI_VAULT_URL + JMI_VAULT_TOKEN);
this repo never sees a private key. The vault is expected to run its own guard before
signing (claude-code go-live note #3) and to refuse anything but create/claim txs.

⚠️ HIGH-RISK: the vault service itself is not in this repo and is not built here."""
import base64

from mogit.guard import b58
from .http import Http, env

class Vault:
    def __init__(self, http=None, url=None, token=None):
        self.http, self._url, self._tok = http or Http(timeout=10), url, token

    def _post(self, path, body):
        return self.http.json("POST", (self._url or env("JMI_VAULT_URL")).rstrip("/") + path,
                              headers={"Authorization": f"Bearer {self._tok or env('JMI_VAULT_TOKEN')}"},
                              json_body=body)

    def new_mint(self, job_id):
        """Vault generates + keeps the mint keypair for this job; returns the pubkey only.
        Same job_id must return the same mint (same-mint rebuild, §8)."""
        return self._post("/mint", {"job_id": job_id})["pubkey"]

    def sign(self, tx, purpose):
        raw = tx if isinstance(tx, (bytes, bytearray)) else base64.b64decode(tx)
        out = self._post("/sign", {"tx_b64": base64.b64encode(raw).decode(), "purpose": purpose})
        return base64.b64decode(out["signed_b64"])

    @staticmethod
    def signature_of(signed):
        raw = signed if isinstance(signed, (bytes, bytearray)) else base64.b64decode(signed)
        if len(raw) < 65 or raw[0] < 1 or not any(raw[1:65]):
            return None
        return b58(bytes(raw[1:65]))
