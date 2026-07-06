"""
VirtualWallet — paper-trading ledger for the K100bet Agent SDK.

A zero-dependency, persistent sandbox that records the decisions an agent
*would* have made on k100bet.com without ever hitting the live API or the
on-chain covenant ledger. Use it to:

  - Backtest strategies against the live order book via get_orderbook_quote().
  - Run "what-if" walks against historical snapshots saved to disk.
  - Drive smoke / CI runs without a funded Kaspa wallet.
  - Demonstrate flows to a partner or referee without touching real funds.

The ledger is append-only JSONL at:

    ~/.k100bet/wallets/<user_id>.jsonl

Each line is a VirtualTrade record. Use VirtualWallet to load, replay, and
aggregate. The ledger can be wiped via the CLI flag --reset or by deleting the
file. A `seed` event is written on first open so balance starts at a known
checkpoint.

Quick start (real API + virtual wallet):

    from k100bet_agent import K100bet
    from virtual_wallet import VirtualWallet

    vw = VirtualWallet(user_id="paper-bot-1", starting_kas=10_000)
    k = K100bet(api_key=os.environ["K100BET_API_KEY"])

    quote = k.get_orderbook_quote("crypto-btc-up-xxx", "yes", 500)
    vw.record_entry(
        market_id="crypto-btc-up-xxx",
        side="yes",
        amount_kas=500,
        shares=quote["estimatedShares"],
        avg_price=quote["estimatedPrice"],
        note="momentum entry",
    )

    # later, virtual exit
    quote2 = k.get_orderbook_quote("crypto-btc-up-xxx", "yes", shares_i_have)
    vw.record_exit(
        market_id="crypto-btc-up-xxx",
        side="yes",
        shares=shares_i_have,
        avg_price=quote2["estimatedPrice"],
    )

    # eventually settlement (market resolves)
    vw.record_settlement("crypto-btc-up-xxx", outcome="yes", won=True)
    print(vw.pnl_report())

Quick start (dry-run via SDK without a real K100bet API key):

    from k100bet_agent import K100bet
    k = K100bet(dry_run=True, virtual_wallet_user_id="paper-bot-1")
    k.place_limit_order("market-id", "yes", 100, 0.60)   # recorded virtually
    print(k.virtual_wallet.pnl_report())

This module is a sibling to k100bet-agent.py. Both files are deliberately
import-safe without third-party packages.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


# ============================================================
# Ledger location
# ============================================================

ENV_LEDGER_DIR = "K100BET_VIRTUAL_WALLET_DIR"
DEFAULT_LEDGER_DIR = Path.home() / ".k100bet" / "wallets"


def _ledger_root() -> Path:
    """Return the active root directory for virtual-wallet ledgers."""
    raw = os.environ.get(ENV_LEDGER_DIR)
    root = Path(raw).expanduser() if raw else DEFAULT_LEDGER_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


# ============================================================
# House-fee model (must mirror the live CLOB: 2%)
# ============================================================

HOUSE_FEE_RATE = 0.02


# ============================================================
# Trade record
# ============================================================

@dataclass
class VirtualTrade:
    """One append-only entry in a virtual wallet's ledger."""

    id: str
    ts: float                  # unix seconds (UTC)
    market_id: str
    side: str                  # 'yes' | 'no' | 'n/a' (seed/wallet meta events)
    action: str                # 'seed' | 'entry' | 'exit' | 'settlement'
    amount_kas: float          # KAS spent (entries) or received (exits/settlements)
    shares: float              # position size in shares
    avg_price: float           # fill price estimate
    note: Optional[str] = None
    won: Optional[bool] = None # populated on settlement events
    tx_ref: Optional[str] = None  # ref back to a real on-chain bet id if known


# ============================================================
# VirtualWallet
# ============================================================

class VirtualWallet:
    """
    Paper-trading wallet. Loads an append-only JSONL ledger from disk and
    further appends each record through record_* methods. Position state is
    derived by replaying entries / exits / settlements, not cached — so the
    class is safe to reopen mid-session without losing accuracy.
    """

    def __init__(
        self,
        user_id: str,
        starting_kas: float = 0.0,
        ledger_path: Optional[Union[str, Path]] = None,
    ):
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must be a non-empty string")
        if starting_kas < 0:
            raise ValueError("starting_kas must be >= 0")

        self.user_id: str = user_id.strip()
        self.starting_kas: float = float(starting_kas)
        self.ledger_path: Path = (
            Path(ledger_path).expanduser()
            if ledger_path is not None
            else _ledger_root() / f"{self.user_id}.jsonl"
        )

        # Replay-existing-ledger to recover balance + history.
        self.trades: List[VirtualTrade] = self._load_ledger()
        self.balance_kas: float = self._replay_balance()

        # Persist a "seed" event the first time the wallet is opened so the
        # ledger contains a recoverable checkpoint rather than an implicit
        # implicit initial state.
        if not self.trades:
            seed = VirtualTrade(
                id=str(uuid.uuid4()),
                ts=time.time(),
                market_id="__wallet__",
                side="n/a",
                action="seed",
                amount_kas=float(starting_kas),
                shares=0.0,
                avg_price=0.0,
                note=f"Wallet seeded with {starting_kas:.6f} KAS",
            )
            self._append_ledger(seed)
            self.trades.append(seed)
            self.balance_kas = float(starting_kas)

    # ----- I/O -----

    def _load_ledger(self) -> List[VirtualTrade]:
        if not self.ledger_path.exists():
            return []
        out: List[VirtualTrade] = []
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    out.append(VirtualTrade(**obj))
                except Exception as err:  # noqa: BLE001
                    # Skip malformed lines instead of crashing the whole wallet.
                    # The ledger is append-only so re-saving would lose history.
                    print(
                        f"[VirtualWallet:{self.user_id}] "
                        f"skipping malformed ledger line {lineno}: {err}"
                    )
        return out

    def _append_ledger(self, trade: VirtualTrade) -> None:
        # Best-effort atomic append:
        #   * open with O_APPEND so the OS appends at EOF even with concurrent writers
        #   * flush Python buffer
        #   * fsync() so the kernel flushes to disk before close
        #
        # Two concurrent processes can still interleave ON THE SAME LINE because
        # POSIX guarantees atomicity only up to PIPE_BUF (~4 KB) for O_APPEND;
        # a single VirtualTrade line is well below that, so concurrent writers
        # never tear each other's records. Multi-host federation (e.g. NFS)
        # would need a real lock or an external store — out of scope here.
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(asdict(trade), separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(
            str(self.ledger_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _replay_balance(self) -> float:
        """Recompute wallet balance by replaying the trade list."""
        bal = 0.0
        for t in self.trades:
            if t.action == "seed":
                bal += t.amount_kas
            elif t.action == "entry":
                bal -= t.amount_kas
            elif t.action in ("exit", "settlement"):
                bal += t.amount_kas
        return round(bal, 6)

    # ----- Recording -----

    def record_entry(
        self,
        market_id: str,
        side: str,
        amount_kas: float,
        shares: float,
        avg_price: float,
        note: Optional[str] = None,
        tx_ref: Optional[str] = None,
    ) -> VirtualTrade:
        """Record a virtual entry (buy). Decrements virtual balance by amount_kas."""
        side = side.lower()
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        if amount_kas <= 0:
            raise ValueError(f"amount_kas must be > 0, got {amount_kas}")
        if shares <= 0:
            raise ValueError(f"shares must be > 0, got {shares}")
        if avg_price <= 0 or avg_price >= 1:
            raise ValueError(f"avg_price must be in (0, 1), got {avg_price}")
        if amount_kas > self.balance_kas:
            raise ValueError(
                f"insufficient balance: have {self.balance_kas:.4f} KAS, "
                f"want {amount_kas:.4f} KAS (start a wallet with a higher "
                f"starting_kas or top up via record_topup)"
            )

        trade = VirtualTrade(
            id=str(uuid.uuid4()),
            ts=time.time(),
            market_id=market_id,
            side=side,
            action="entry",
            amount_kas=float(amount_kas),
            shares=float(shares),
            avg_price=float(avg_price),
            note=note,
            tx_ref=tx_ref,
        )
        self.balance_kas = round(self.balance_kas - trade.amount_kas, 6)
        self._append_ledger(trade)
        self.trades.append(trade)
        return trade

    def record_exit(
        self,
        market_id: str,
        side: str,
        shares: float,
        avg_price: float,
        note: Optional[str] = None,
    ) -> VirtualTrade:
        """Record a virtual exit (sell). Mirrors the live CLOB: gross minus 2% house fee."""
        side = side.lower()
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        if shares <= 0:
            raise ValueError(f"shares must be > 0, got {shares}")
        if avg_price <= 0 or avg_price >= 1:
            raise ValueError(f"avg_price must be in (0, 1), got {avg_price}")

        # Verify we hold the shares we're selling.
        open_shares = self._open_position(market_id, side)
        if shares > open_shares + 1e-9:
            raise ValueError(
                f"cannot exit {shares} shares of {market_id}/{side}: "
                f"only {open_shares:.6f} open"
            )

        gross = float(shares) * float(avg_price)
        fee = gross * HOUSE_FEE_RATE
        net = round(gross - fee, 6)

        trade = VirtualTrade(
            id=str(uuid.uuid4()),
            ts=time.time(),
            market_id=market_id,
            side=side,
            action="exit",
            amount_kas=net,
            shares=float(shares),
            avg_price=float(avg_price),
            note=note
            or f"exit @ {avg_price:.4f} (gross {gross:.4f}, fee {fee:.4f})",
        )
        self.balance_kas = round(self.balance_kas + trade.amount_kas, 6)
        self._append_ledger(trade)
        self.trades.append(trade)
        return trade

    def record_settlement(
        self,
        market_id: str,
        outcome: str,
        won: bool,
        note: Optional[str] = None,
    ) -> VirtualTrade:
        """Record a virtual settlement. Payout = sum of open position shares on
        the winning side (winner gets $1/share, loser gets $0)."""
        outcome = outcome.lower()
        if outcome not in ("yes", "no"):
            raise ValueError(f"outcome must be 'yes' or 'no', got {outcome!r}")

        pos_shares = self._open_position(market_id, outcome)
        if pos_shares <= 0:
            # No open position on the winning side — record a zero-payout settlement
            # so the ledger captures the resolution event anyway.
            payout = 0.0
        else:
            payout = float(pos_shares if won else 0.0)

        trade = VirtualTrade(
            id=str(uuid.uuid4()),
            ts=time.time(),
            market_id=market_id,
            side=outcome,
            action="settlement",
            amount_kas=payout,
            shares=float(pos_shares),
            avg_price=1.0 if won else 0.0,
            won=won,
            note=note
            or f"settlement market={market_id} outcome={outcome} "
               f"won={won} payout={payout:.6f}",
        )
        self.balance_kas = round(self.balance_kas + trade.amount_kas, 6)
        self._append_ledger(trade)
        self.trades.append(trade)
        return trade

    def record_topup(
        self,
        amount_kas: float,
        note: Optional[str] = None,
    ) -> VirtualTrade:
        """Add KAS to the virtual wallet — useful when backtesting with multiple
        'deposit' moments without re-seeding the ledger."""
        if amount_kas <= 0:
            raise ValueError(f"amount_kas must be > 0, got {amount_kas}")
        trade = VirtualTrade(
            id=str(uuid.uuid4()),
            ts=time.time(),
            market_id="__wallet__",
            side="n/a",
            action="seed",
            amount_kas=float(amount_kas),
            shares=0.0,
            avg_price=0.0,
            note=note or f"topup +{amount_kas:.4f} KAS",
        )
        self.balance_kas = round(self.balance_kas + trade.amount_kas, 6)
        self._append_ledger(trade)
        self.trades.append(trade)
        return trade

    # ----- Position math -----

    def _open_position(self, market_id: str, side: str) -> float:
        """Net shares held at a side in a market. Replayed from the ledger."""
        side = side.lower()
        net = 0.0
        for t in self.trades:
            if t.market_id != market_id or t.side != side:
                continue
            if t.action == "entry":
                net += t.shares
            elif t.action == "exit":
                net -= t.shares
            elif t.action == "settlement":
                # Settlements close out the position.
                net = 0.0
        return max(0.0, net)

    def positions(self) -> Dict[str, Dict[str, float]]:
        """Snapshot of open positions, keyed by market_id then side."""
        out: Dict[str, Dict[str, float]] = {}
        markets = {t.market_id for t in self.trades if t.action == "entry"}
        for mkt in markets:
            for side in ("yes", "no"):
                sh = self._open_position(mkt, side)
                if sh > 0:
                    out.setdefault(mkt, {})[side] = round(sh, 6)
        return out

    # ----- Reporting -----

    def pnl_report(self) -> Dict[str, Any]:
        """Aggregate P&L across the entire wallet history."""
        deposits = sum(t.amount_kas for t in self.trades if t.action == "seed")
        spent = sum(t.amount_kas for t in self.trades if t.action == "entry")
        received = sum(t.amount_kas for t in self.trades if t.action in ("exit", "settlement"))

        wins = sum(1 for t in self.trades if t.action == "settlement" and t.won)
        losses = sum(1 for t in self.trades if t.action == "settlement" and not t.won)
        win_rate = round(wins / max(wins + losses, 1), 4)

        realized = round(received - spent, 6)
        total_pnl = round(self.balance_kas - self.starting_kas, 6)

        return {
            "user_id": self.user_id,
            "starting_kas": round(self.starting_kas, 6),
            "balance_kas": round(self.balance_kas, 6),
            "deposited_kas": round(deposits, 6),
            "spent_kas": round(spent, 6),
            "received_kas": round(received, 6),
            "realized_pnl_kas": realized,
            "total_pnl_kas": total_pnl,
            "open_positions": self.positions(),
            "settlements": {
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
            },
            "trade_count": len(self.trades),
            "ledger_path": str(self.ledger_path),
        }

    def __repr__(self) -> str:
        return (
            f"VirtualWallet(user_id={self.user_id!r}, "
            f"balance={self.balance_kas:.4f} KAS, "
            f"ledger={self.ledger_path})"
        )


# ============================================================
# CLI
# ============================================================

def _cli() -> None:
    """Tiny CLI for inspecting virtual wallets from the shell."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Inspect a K100bet virtual-wallet ledger."
    )
    parser.add_argument("--user", required=True, help="Virtual wallet user id")
    parser.add_argument(
        "--starting-kas",
        type=float,
        default=10_000.0,
        help="Starting balance (only consulted if the ledger is empty; default 10,000)",
    )
    parser.add_argument(
        "--ledger",
        help="Override ledger file path (default ~/.k100bet/wallets/<user>.jsonl)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe the existing ledger for this user before loading.",
    )
    args = parser.parse_args()

    if args.reset and not args.ledger:
        target = _ledger_root() / f"{args.user}.jsonl"
        if target.exists():
            target.unlink()
            print(f"[reset] wiped {target}")

    vw = VirtualWallet(
        user_id=args.user,
        starting_kas=args.starting_kas,
        ledger_path=args.ledger,
    )
    # Default to JSON so the output is pipe-friendly into jq / other tools.
    # Always-print-JSON is intentional — the wallet is for programmatic use.
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    _cli()
