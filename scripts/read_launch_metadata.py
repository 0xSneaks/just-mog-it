#!/usr/bin/env python3
"""READ-ONLY: Metaplex metadata check for a launched mint (Launch #001 by default).

One JSON-RPC call (getAccountInfo) on the metadata PDA. No signing, no keys, no writes.
Prints isMutable, updateAuthority, uri, and whether the uri is content-addressed
(IPFS/Arweave), so the owner can tell whether the description can be corrected.

    python3 scripts/read_launch_metadata.py [MINT]
    RPC: env JMI_RPC_URL, default https://api.mainnet-beta.solana.com
"""
import base64
import json
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.parse import b58decode  # noqa: E402
from bot.live.solana import find_pda  # noqa: E402
from mogit.guard import b58  # noqa: E402

METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
LAUNCH_001 = "2bhR4BoUVAPnKi46VYt6UcRUrVtT1UYtwsb4QUpx7tnK"  # synthetic placeholder mint (offline fixture only)
_CID = re.compile(r"/ipfs/(Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{20,})")


def metadata_pda(mint):
    return find_pda([b"metadata", b58decode(METADATA_PROGRAM), b58decode(mint)], METADATA_PROGRAM)[0]


def _str(data, i):
    n = int.from_bytes(data[i:i + 4], "little"); i += 4
    return data[i:i + n].decode("utf-8", "replace").rstrip("\x00"), i + n


def decode_metadata(data):
    """Metaplex Token Metadata v1 layout up to is_mutable."""
    if not data or data[0] != 4:
        raise ValueError("not a MetadataV1 account")
    update_authority, mint = b58(data[1:33]), b58(data[33:65])
    name, i = _str(data, 65)
    symbol, i = _str(data, i)
    uri, i = _str(data, i)
    i += 2  # seller_fee_basis_points
    if data[i]:  # Option<Vec<Creator>>
        n = int.from_bytes(data[i + 1:i + 5], "little"); i += 5 + 34 * n
    else:
        i += 1
    i += 1  # primary_sale_happened
    return {"updateAuthority": update_authority, "mint": mint, "name": name, "symbol": symbol,
            "uri": uri, "isMutable": bool(data[i])}


def uri_storage(uri):
    p = urlparse(uri)
    host = (p.hostname or "").lower()
    if p.scheme in ("ipfs", "ar") or host.endswith("arweave.net") or _CID.search(p.path or ""):
        return "content-addressed (IPFS/Arweave): the JSON at this uri cannot be edited in place"
    return f"mutable host ({host or 'none'}): the JSON can change without an on-chain update"


def main(mint=LAUNCH_001):
    url = os.environ.get("JMI_RPC_URL", "https://api.mainnet-beta.solana.com")
    pda = metadata_pda(mint)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                       "params": [pda, {"encoding": "base64"}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        value = json.load(r)["result"]["value"]
    if value is None:
        raise SystemExit(f"no metadata account at {pda}")
    meta = decode_metadata(base64.b64decode(value["data"][0]))
    meta.update(metadataPda=pda, owner=value["owner"], uriStorage=uri_storage(meta["uri"]),
                descriptionCorrectable=(
                    "yes: update authority can point uri at corrected JSON" if meta["isMutable"]
                    else "no: metadata is immutable on-chain"))
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:2])
