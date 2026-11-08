"""Owner policy loader. Every money/safety number comes from config/policy.yaml (spec §0).
Missing or malformed keys make load() raise — the bot never boots on guessed values."""
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "policy.yaml"

class PolicyError(Exception):
    pass

REQUIRED = ["launches_per_day", "cost_cap_sol", "launch_fee_sol", "fee_split", "trigger_policy",
            "pair_quote", "dev_buy_sol", "reply_budget_usd_per_day", "kill_switch_on_at_boot",
            "owner_approval_per_item", "x_reply_approval_ref", "claim_threshold_sol",
            "priority_fee_cap_sol", "mog_me_enabled", "venue", "allowlist", "rug", "wrong_pair",
            "description_prefix", "description_max_chars", "mogger", "tiers", "limits", "x_prices_usd",
            "mog_me"]

@dataclass(frozen=True)
class Policy:
    raw: dict

    def __getattr__(self, k):
        try:
            return self.raw[k]
        except KeyError:
            raise AttributeError(k)

    def d(self, k):
        """Decimal value of a top-level money key (strings in YAML to avoid float drift)."""
        return Decimal(str(self.raw[k]))

def load(path=DEFAULT_PATH, overrides=None):
    raw = yaml.safe_load(Path(path).read_text())
    if overrides:
        raw = {**raw, **overrides}
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise PolicyError(f"policy missing keys: {missing}")
    split = raw["fee_split"]
    if sorted(split) != ["bankroll", "holders", "mogger"] or sum(split.values()) != 100:
        raise PolicyError("fee_split must be mogger/holders/bankroll summing to 100")
    if raw["pair_quote"] != "SOL":
        raise PolicyError("pair_quote must be SOL (owner §0.6)")
    if raw["venue"] != "pump.fun":
        raise PolicyError("venue must be pump.fun (owner 2026-11-04)")
    if Decimal(str(raw["dev_buy_sol"])) != 0:
        raise PolicyError("dev_buy_sol must be 0 (owner §0.7); changing it needs every burn table updated")
    if raw["trigger_policy"] not in ("allowlist", "open"):
        raise PolicyError("trigger_policy must be allowlist|open")
    if raw["rug"]["rule"] not in ("strict", "any"):
        raise PolicyError("rug.rule must be strict|any")
    if Decimal(str(raw["cost_cap_sol"])) <= 0 or int(raw["launches_per_day"]) < 0:
        raise PolicyError("cost cap and daily cap must be positive")
    return Policy(raw)
