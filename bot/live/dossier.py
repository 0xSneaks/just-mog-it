"""Read-only coin facts for the ORIGINAL coin.
- Helius DAS getAsset (env JMI_DAS_URL, a Helius RPC URL): on-chain name/symbol/image
- pump.fun frontend-api-v3 coins/{mint}: description/socials when the original lived on pump
- DexScreener tokens/{mint}: pair quote, venue, liquidity, 1h price change, pair age
Field names for pump.fun + DexScreener are INFERENCE from their public JSON; any missing
field becomes None, and None never counts as a rug signal (rug.py)."""
from decimal import Decimal

from ..rug import CoinSnapshot
from .http import Http, HttpError, env

PUMP_API = "https://frontend-api-v3.pump.fun/coins/"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"

class Dossier:
    def __init__(self, http=None, das_url=None, gemini=None):
        self.http, self._das, self.gemini = http or Http(), das_url, gemini

    def das_asset(self, ca):
        r = self.http.json("POST", self._das or env("JMI_DAS_URL"),
                           json_body={"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": {"id": ca}})
        a = r.get("result") or {}
        md = (a.get("content") or {}).get("metadata") or {}
        links = (a.get("content") or {}).get("links") or {}
        if not a.get("id"):
            raise RuntimeError("DAS: no asset")
        return {"mint": a["id"], "name": md.get("name"), "symbol": md.get("symbol"),
                "description": md.get("description"), "image_uri": links.get("image")}

    def pump_coin(self, ca):
        try:
            c = self.http.json("GET", PUMP_API + ca)
        except HttpError as e:
            if e.status == 404:
                return None                                  # original was not a pump.fun coin
            raise
        return {k: c.get(k) for k in ("mint", "name", "symbol", "description", "image_uri",
                                      "twitter", "telegram", "website", "complete", "real_sol_reserves")}

    def _pairs(self, ca):
        return (self.http.json("GET", DEXSCREENER + ca) or {}).get("pairs") or []

    def snapshot(self, ca):
        pairs = self._pairs(ca)
        pump = self.pump_coin(ca)
        if not pairs and not pump:
            return CoinSnapshot(venue_alive=False)
        if pairs:
            top = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            q = (top.get("quoteToken") or {}).get("symbol")
            liq = (top.get("liquidity") or {}).get("quote")
            h1 = (top.get("priceChange") or {}).get("h1")
            return CoinSnapshot(
                quote_symbol=("SOL" if q in ("SOL", "WSOL") else q), venue=top.get("dexId"),
                venue_alive=True, graduated=(pump or {}).get("complete"),
                pair_liquidity_sol=Decimal(str(liq)) if liq is not None and q in ("SOL", "WSOL") else None,
                price_drop_pct_window=Decimal(str(-float(h1))) if h1 is not None and float(h1) < 0 else None)
        rs = pump.get("real_sol_reserves")
        return CoinSnapshot(quote_symbol="SOL", venue="pump.fun", venue_alive=True,
                            graduated=pump.get("complete"),
                            pair_liquidity_sol=Decimal(rs) / Decimal(10 ** 9) if rs is not None else None)

    def fee_weight(self, mint):
        """24h volume (USD) as this coin's share of the pooled creator vault."""
        return sum(float((p.get("volume") or {}).get("h24") or 0) for p in self._pairs(mint))

    def inspect_image(self, uri):
        if not uri:
            return None
        img = self.http.request("GET", uri)
        if len(img) > 10_000_000:
            raise RuntimeError("image over 10 MB")
        return self.gemini.inspect(img) if self.gemini else None
