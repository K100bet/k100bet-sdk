"""
TradeRecommendation — structured trade cards an AI agent can produce without
holding a `trade`-permission API token.

When an LLM does not (or will not) hold live credentials for k100bet.com, it
can still build a `TradeRecommendation` describing what it believes the user
should do. The card is then surfaced to the user in markdown / JSON / curl
form so they can review and approve it themselves.

The card is *non-executing*: it never places an order. Only the user can copy
the curl + their own API key into a terminal, or paste the markdown card into
the K100bet UI, or fire the SDK with `execute=True` after reviewing the
recommendation.

This is the "agent recommends, user disposes" pattern. It is designed to be
safe by default for agents that have policy reasons not to hold trade keys.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ============================================================
# TradeRecommendation
# ============================================================

@dataclass
class TradeRecommendation:
    """A non-executing trade proposal that an AI agent hands back to the user
    for review and approval."""

    # Identity / lifecycle
    id: str                       # uuid
    created_at: float             # unix seconds (UTC)
    expires_at: float             # unix seconds (UTC)

    # Market context
    market_id: str                # slug or uuid
    market_title: str             # human-readable
    side: str                     # "yes" or "no"

    # Trade shape
    target_price: float           # 0.01 .. 0.99
    amount_kas: float

    # Estimated outcome (mirrors get_orderbook_quote semantics + 2% CLOB fee)
    expected_shares: float
    est_fill_price: float
    est_slippage_pct: float
    expected_payout_kas: float    # if win, minus 2% fee
    expected_profit_kas: float    # expected_payout_kas - amount_kas

    # Agent's reasoning
    confidence: Optional[float]   # 0.0..1.0; None means "agent did not score"
    reasoning: str
    features: Dict[str, Any] = field(default_factory=dict)
    risks: List[str] = field(default_factory=list)

    # Execution audit
    executed: bool = False
    executed_at: Optional[float] = None
    execution_error: Optional[str] = None

    # ----- Validation -----

    def __post_init__(self) -> None:
        if self.side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {self.side!r}")
        if not (0.01 <= self.target_price <= 0.99):
            raise ValueError(f"target_price must be in [0.01, 0.99], got {self.target_price}")
        if self.amount_kas <= 0:
            raise ValueError(f"amount_kas must be > 0, got {self.amount_kas}")
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0.0, 1.0] or None, got {self.confidence}")
        if self.target_price != self.target_price:  # NaN guard
            raise ValueError("target_price must not be NaN")

    # ----- Serialization -----

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str, sort_keys=False)

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    # ----- Formatters -----

    def to_markdown(self) -> str:
        """Render as a human-readable markdown card (paste-friendly)."""
        side_label = self.side.upper()
        conf_str = (
            f"{self.confidence:.0%}" if self.confidence is not None
            else "n/a (agent did not score)"
        )
        rows: List[str] = []

        rows.append(f"## Trade recommendation \u2014 {self.market_title}")
        rows.append("")
        rows.append(f"- **Card ID**: `{self.id}`")
        rows.append(f"- **Action**: Buy **{side_label}** shares at {self.target_price:.4f} for **{self.amount_kas:.2f} KAS**")
        rows.append(f"- **Market**: `{self.market_id}`")
        rows.append(f"- **Confidence**: {conf_str}")
        rows.append("")

        rows.append("### Expected outcome")
        rows.append(f"- **Estimated fill price**: {self.est_fill_price:.4f}")
        rows.append(f"- **Estimated shares**: {self.expected_shares:.4f}")
        rows.append(f"- **Estimated slippage**: {self.est_slippage_pct:.2f}%")
        rows.append(f"- **If wins**: payout ~{self.expected_payout_kas:.4f} KAS")
        rows.append(f"- **If wins, profit**: ~{self.expected_profit_kas:+.4f} KAS")
        rows.append("")

        rows.append("### Reasoning")
        rows.append(self.reasoning or "_No reasoning supplied._")
        rows.append("")

        rows.append("### Supporting features")
        if self.features:
            for k, v in self.features.items():
                rows.append(f"- `{k}`: {v}")
        else:
            rows.append("- _(none)_")
        rows.append("")

        rows.append("### Risks / caveats")
        if self.risks:
            for r in self.risks:
                rows.append(f"- {r}")
        else:
            rows.append("- _(none surfaced)_")
        rows.append("")

        rows.append("### Action (paste to execute yourself)")
        rows.append(
            "This card does **not** place the trade on its own. Either paste it "
            "into the K100bet UI, or use the curl below with your own token."
        )
        rows.append("")

        if self.expires_at > 0:
            rows.append(
                f"**Expires**: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(self.expires_at))} "
                f"\u2014 do not execute after this time."
            )
        return "\n".join(rows)

    def to_curl(
        self,
        base_url: str = "https://k100bet.com",
        *,
        market_id_override: Optional[str] = None,
    ) -> str:
        """Render as a curl one-liner the user can paste into a terminal."""
        target_market = market_id_override or self.market_id
        # JSON body for /api/limit-orders
        body = {
            "marketId": target_market,
            "side": self.side,
            "amount": self.amount_kas,
            "targetPrice": self.target_price,
        }
        return (
            f"curl -X POST {base_url}/api/limit-orders \\\n"
            f"  -H \"x-api-key: $K100BET_API_KEY\" \\\n"
            f"  -H \"Content-Type: application/json\" \\\n"
            f"  -d '{json.dumps(body, separators=(',', ':'))}'"
        )


# ============================================================
# Public formatter dispatch
# ============================================================

SUPPORTED_FORMATS = ("markdown", "json", "curl")


def format_recommendation(
    card: TradeRecommendation,
    *,
    fmt: str = "markdown",
    base_url: str = "https://k100bet.com",
) -> str:
    """Format a TradeRecommendation in the requested style."""
    if fmt == "markdown":
        return card.to_markdown()
    if fmt == "json":
        return card.to_json()
    if fmt == "curl":
        return card.to_curl(base_url=base_url)
    raise ValueError(
        f"unknown fmt: {fmt!r}; must be one of {SUPPORTED_FORMATS}"
    )


# ============================================================
# CLI
# ============================================================

def _cli() -> None:
    """CLI helpers for inspecting a card snapshot or piping a markdown + curl
    bundle to a chat transcript."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Render a TradeRecommendation card from a JSON file."
    )
    parser.add_argument(
        "--card", required=True,
        help="Path to a TradeRecommendation JSON file (as produced by SDK)",
    )
    parser.add_argument(
        "--fmt", choices=SUPPORTED_FORMATS, default="markdown",
        help="Render format (default markdown)",
    )
    parser.add_argument(
        "--base-url", default="https://k100bet.com",
        help="Base URL used in the curl output (default https://k100bet.com)",
    )
    args = parser.parse_args()

    with open(args.card, "r", encoding="utf-8") as f:
        obj = json.loads(f.read())
    obj.setdefault("id", str(uuid.uuid4()))
    obj.setdefault("created_at", time.time())
    obj.setdefault("expires_at", time.time() + 300)
    obj.setdefault("market_title", obj.get("market_id", "?"))
    obj.setdefault("features", {})
    obj.setdefault("risks", [])
    card = TradeRecommendation(**obj)
    print(format_recommendation(card, fmt=args.fmt, base_url=args.base_url))


if __name__ == "__main__":
    _cli()
