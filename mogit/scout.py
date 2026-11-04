"""INTEL/scout: turn ranked narratives into pack drafts (packs/drafts/, max 5, drafts only, never launched)."""
import json, re, sys
from pathlib import Path
def draft(items, launcher="grok-bot", out="packs/drafts"):
    Path(out).mkdir(parents=True, exist_ok=True); paths = []
    for rank, it in enumerate(items[:5], 1):
        t = re.sub(r"[^A-Z0-9]", "", it["name"].upper())[:6] or "DRAFT"
        d = {"pack": t, "status": "draft", "rank": rank, "source": it["source"],
             "coin": {"name": it["name"], "ticker": t, "description": f"{it['name']}. Launched by {launcher}.",
                      "image_idea": it.get("image_idea", "")},
             "needs": "Sneaks yes per launch; real-person/brand names need explicit yes"}
        p = Path(out) / f"{t.lower()}.json"; p.write_text(json.dumps(d, indent=2)); paths.append(str(p))
    return paths
if __name__ == "__main__": print(draft(json.load(sys.stdin)))
