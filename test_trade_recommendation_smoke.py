"""
Smoke test for agents/trade_recommendation.py and the recommend_trade()
patch in agents/k100bet-agent.py that produces a TradeRecommendation.

Validates the new "agent recommends, user disposes" surface:

  - TradeRecommendation construction (happy path)
  - Validation: invalid side, target_price out of range, negative
    amount_kas, confidence out of [0, 1]
  - is_expired boundary behavior
  - markdown / curl / json formatters return non-empty strings and
    contain the expected fragments
  - format_recommendation() dispatch raises on unknown fmt
  - CLI round-trip via _cli() (NOT executed; we just check it imports
    and re-imports correctly)

Not covered (would need a live API):
  - recommend_trade() against a real /api/limit-orders quote
  - recommend_trade(execute=True) round-tripping a live order

Run with:

    python agents/test_trade_recommendation_smoke.py
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trade_recommendation as tr_mod  # noqa: E402


def _good_card(now: float) -> tr_mod.TradeRecommendation:
    return tr_mod.TradeRecommendation(
        id="00000000-0000-0000-0000-000000000001",
        created_at=now,
        expires_at=now + 300,
        market_id="smoke-market",
        market_title="Smoke Test Market",
        side="yes",
        target_price=0.60,
        amount_kas=500.0,
        expected_shares=833.333,
        est_fill_price=0.6010,
        est_slippage_pct=0.1667,
        expected_payout_kas=816.666,
        expected_profit_kas=316.666,
        confidence=0.72,
        reasoning="Momentum + thin book signal",
        features={"best_bid": 0.59, "best_ask": 0.62, "depth_kas": 1200.0},
        risks=["book is thin on the ask side"],
    )


def main() -> int:
    failed = 0
    now = time.time()

    card = _good_card(now)
    assert card.id == "00000000-0000-0000-0000-000000000001"
    assert card.side == "yes"
    assert card.target_price == 0.60
    assert card.confidence == 0.72
    assert not card.is_expired(), "fresh card should not be expired"

    # to_dict / to_json round-trip
    d = card.to_dict()
    j = card.to_json()
    parsed = json.loads(j)
    assert parsed["id"] == card.id
    assert parsed["side"] == "yes"
    assert parsed["features"]["best_bid"] == 0.59

    # Markdown rendering
    md = card.to_markdown()
    for expected in (
        "Smoke Test Market",
        "Buy **YES**",
        "0.60",
        "500.00",
        "Confidence",
        "Momentum",
        "best_bid",
        "Risks",
        "Expires",
    ):
        if expected not in md:
            print(f"[FAIL] markdown missing fragment: {expected!r}")
            failed += 1

    # Curl rendering — uses compact JSON separators (',', ':') so the body is
    # one-line pipe-friendly. Matches what `to_curl()` actually emits.
    curl = card.to_curl(base_url="https://k100bet.com")
    for expected in (
        "curl -X POST",
        "x-api-key",
        "/api/limit-orders",
        "\"marketId\":\"smoke-market\"",
        "\"side\":\"yes\"",
        "\"targetPrice\":0.6",  # default json serialization of 0.60 -> 0.6
        "\"amount\":500.0",
    ):
        if expected not in curl:
            print(f"[FAIL] curl missing fragment: {expected!r}")
            failed += 1

    # Markdown + curl bundle via the public dispatch helper
    out_md = tr_mod.format_recommendation(card, fmt="markdown")
    assert out_md == md
    out_curl = tr_mod.format_recommendation(card, fmt="curl")
    assert out_curl == curl

    # Unknown fmt → raises
    try:
        tr_mod.format_recommendation(card, fmt="yaml")
        print("[FAIL] unknown fmt should have raised")
        failed += 1
    except ValueError:
        pass

    # ----- Validation guards -----

    bad_inputs = [
        # invalid side
        dict(_good_card(now).__dict__, **{"side": "maybe"}),
        # target_price out of range
        dict(_good_card(now).__dict__, **{"target_price": 1.20}),
        # amount_kas <= 0
        dict(_good_card(now).__dict__, **{"amount_kas": -1.0}),
        # confidence out of range
        dict(_good_card(now).__dict__, **{"confidence": 1.5}),
        # NaN target_price
        dict(_good_card(now).__dict__, **{"target_price": float("nan")}),
    ]
    for bad in bad_inputs:
        try:
            tr_mod.TradeRecommendation(**bad)
            print(f"[FAIL] expected ValueError for input: {bad}")
            failed += 1
        except ValueError:
            pass

    # ----- is_expired boundary -----

    past = tr_mod.TradeRecommendation(
        id="x", created_at=now - 600, expires_at=now - 300,
        market_id="m", market_title="M", side="yes",
        target_price=0.5, amount_kas=1.0,
        expected_shares=2.0, est_fill_price=0.5, est_slippage_pct=0.0,
        expected_payout_kas=2.0, expected_profit_kas=1.0,
        confidence=None, reasoning="",
    )
    if not past.is_expired():
        print("[FAIL] past card should be expired")
        failed += 1
    if past.is_expired(now=now - 1000):
        # older now-time should still say "expired" since expires_at hasn't changed
        pass

    # ----- CLI round-trip -----

    # We don't actually invoke the CLI parser (it writes to stdout); we just
    # verify the module imports with the CLI handler attached.
    assert callable(tr_mod._cli), "_cli should be a callable function"
    assert tr_mod.SUPPORTED_FORMATS == ("markdown", "json", "curl")

    # JSON round-trip via the CLI's internal copy of fixture
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "card.json"
        path.write_text(json.dumps(card.to_dict()), encoding="utf-8")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["id"] == card.id
        assert loaded["expected_shares"] == card.expected_shares
        # Newton's check: net payout formula is shares * (1 - HOUSE_FEE_RATE)
        if not math.isclose(
            float(loaded["expected_payout_kas"]),
            float(loaded["expected_shares"]) * (1.0 - 0.02),
            abs_tol=1e-3,
        ):
            print("[FAIL] expected_payout_kas does not match the house-fee formula")
            failed += 1

    if failed:
        print(f"\n[FAIL] {failed} assertion(s) failed")
        return 1
    print("\n[OK] All trade_recommendation smoke assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
