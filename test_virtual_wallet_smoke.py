"""
Smoke test for agents/virtual_wallet.py.

Validates the four record_* paths (seed / entry / exit / settlement),
exercises positions() and pnl_report(), and asserts invariants that any
regression would break:

  - balance round-trips through seed → entry → exit → settlement
  - pnl_report fields are present and consistent
  - open positions close to zero after settlement
  - the JSONL ledger has the expected number of records

Run with:

    python agents/test_virtual_wallet_smoke.py

(no third-party packages required)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Make sure the sibling virtual_wallet module imports without a packaged install.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import virtual_wallet as vw_mod  # noqa: E402


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "smoke-wallet.jsonl"

        # 1. Fresh wallet with a custom ledger path.
        wallet = vw_mod.VirtualWallet(
            user_id="smoke-bot",
            starting_kas=10_000.0,
            ledger_path=ledger,
        )
        assert wallet.balance_kas == 10_000.0, "fresh wallet should equal starting balance"
        assert len(wallet.trades) == 1, "fresh wallet should write one seed event"

        # 2. Place a virtual entry.
        wallet.record_entry(
            market_id="smoke-market-1",
            side="yes",
            amount_kas=500.0,
            shares=1_000.0,
            avg_price=0.50,
            note="smoke entry",
        )
        assert abs(wallet.balance_kas - 9_500.0) < 1e-6, \
            f"balance after entry should be 9,500 KAS, got {wallet.balance_kas}"
        assert wallet._open_position("smoke-market-1", "yes") == 1_000.0

        # 3. Exit part of the position.
        wallet.record_exit(
            market_id="smoke-market-1",
            side="yes",
            shares=500.0,
            avg_price=0.62,
        )
        # gross 500*0.62 = 310; fee 310 * 0.02 = 6.20; net = 303.80
        assert abs(wallet.balance_kas - (9_500.0 + 303.80)) < 1e-3, \
            f"balance after exit should include 2% fee net, got {wallet.balance_kas}"
        assert wallet._open_position("smoke-market-1", "yes") == 500.0

        # 4. Settle the market as a winning yes.
        wallet.record_settlement(
            market_id="smoke-market-1",
            outcome="yes",
            won=True,
        )
        # Open 500 shares × $1 payout = 500 KAS
        assert abs(wallet.balance_kas - (9_500.0 + 303.80 + 500.0)) < 1e-3, \
            f"balance after winning settlement should include payout, got {wallet.balance_kas}"
        assert wallet._open_position("smoke-market-1", "yes") == 0, \
            "settlement should zero the position"

        # 5. pnl_report sanity.
        report = wallet.pnl_report()
        for key in (
            "user_id",
            "starting_kas",
            "balance_kas",
            "deposited_kas",
            "spent_kas",
            "received_kas",
            "realized_pnl_kas",
            "total_pnl_kas",
            "open_positions",
            "settlements",
            "trade_count",
            "ledger_path",
        ):
            if key not in report:
                print(f"FAIL: pnl_report missing key: {key}")
                failed += 1
        assert report["starting_kas"] == 10_000.0
        assert report["spent_kas"] == 500.0
        assert report["received_kas"] == round(303.80 + 500.0, 6)
        assert report["settlements"]["wins"] == 1
        assert report["settlements"]["losses"] == 0
        assert report["open_positions"] == {}, \
            f"expected no open positions, got {report['open_positions']}"

        # 6. Ledger has the expected number of records: 1 seed + 1 entry +
        # 1 exit + 1 settlement = 4 records.
        with open(ledger, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().split("\n") if ln.strip()]
        assert len(lines) == 4, f"expected 4 ledger lines, got {len(lines)}"

        # 7. Reopen the wallet — should recover without re-writing the seed.
        reopened = vw_mod.VirtualWallet(
            user_id="smoke-bot",
            starting_kas=10_000.0,
            ledger_path=ledger,
        )
        assert reopened.balance_kas == wallet.balance_kas, \
            "balance should round-trip through reopen"
        assert reopened.trades == wallet.trades, \
            "trade history should round-trip through reopen"

        # 8. Validation guards.
        try:
            wallet.record_entry(
                market_id="bad", side="maybe",
                amount_kas=1.0, shares=1.0, avg_price=0.5,
            )
            print("FAIL: invalid side should have raised")
            failed += 1
        except ValueError:
            pass
        try:
            wallet.record_entry(
                market_id="bad", side="yes",
                amount_kas=10_000_000.0, shares=1.0, avg_price=0.5,
            )
            print("FAIL: insufficient balance should have raised")
            failed += 1
        except ValueError:
            pass

    if failed:
        print(f"\n[FAIL] {failed} assertion(s) failed")
        return 1
    print("\n[OK] All virtual_wallet smoke assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
