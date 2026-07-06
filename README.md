# K100bet Agent SDK

Python client for [K100bet](https://k100bet.com) prediction markets — markets, CLOB order book, bets, deposits, watchlist, notifications.

## Install

```bash
pip install k100bet
```

Or from this repo:

```bash
cd agents && pip install -e .
```

## Quick start

```python
import os
from k100bet import K100bet

k = K100bet(api_key=os.environ["K100BET_API_KEY"], base_url="https://k100bet.com")

# List markets
for m in k.get_markets():
    print(m["title"], m["yesPrice"], m["noPrice"])

# Place a limit order
order = k.place_limit_order("btc-150k-2025", "yes", "100", 0.60)

# Get a pre-trade quote
quote = k.get_bet_quote("btc-150k-2025", "yes", 100)
```

## CLI

```bash
export K100BET_API_KEY=k100bet_...

# Markets
k100bet markets                          # list all markets
k100bet market --market btc-150k         # single market detail
k100bet search --query "bitcoin"         # search markets
k100bet leaderboard                      # top traders

# Trading
k100bet orderbook --market btc-150k      # live order book
k100bet quote --market btc-150k --side yes --amount 100
k100bet order --market btc-150k --side yes --amount 100 --price 0.60
k100bet market-order --market btc-150k --side yes --amount 100
k100bet cancel-order --order-id <uuid>
k100bet orders                           # open orders
k100bet trades                           # trade history

# Bet lifecycle
k100bet bet-intent --market btc-150k --side yes
k100bet confirm-bet --order-id <bet-uuid>
k100bet claim-bet --order-id <bet-uuid>
k100bet cashout-bet --order-id <bet-uuid>

# Predict Slot
k100bet slot-round                        # current round
k100bet slot-jackpot                      # jackpot pool
k100bet my-slot-bets                      # your slot bets
k100bet slot-intent --market 42 --bucket B3 --side yes

# Account & portfolio
k100bet user
k100bet stats
k100bet kas-price                         # current KAS/USD
k100bet watchlist
k100bet toggle-watchlist --market btc-150k
k100bet notifications
k100bet deposits
k100bet pool                              # liquidity pools
k100bet proposals                         # market proposals

# Utilities
k100bet kaspa-tx --tx-id <tx-hash>        # look up Kaspa transaction
k100bet generate-token                    # generate a new agent token
```

## API Reference

### Markets
| Method | Description |
|--------|-------------|
| `get_markets(category=None)` | List markets, filter by category |
| `get_market(market_id)` | Single market details |
| `search_markets(query)` | Search markets by keyword |
| `get_market_quote(market_id, side, amount)` | Server-side trade quote |
| `get_orderbook(market_id)` | Live CLOB order book |
| `get_market_comments(market_id)` | Market comments |
| `post_market_comment(market_id, text)` | Post a comment |
| `like_market_comment(market_id, comment_id)` | Like a comment |
| `stream_markets()` | SSE real-time market stream |

### Trading
| Method | Description |
|--------|-------------|
| `place_limit_order(market_id, side, amount, target_price)` | CLOB limit order |
| `place_market_order(market_id, side, amount)` | Market order (crosses spread) |
| `cancel_order(order_id)` | Cancel open order |
| `get_open_orders(market_id=None)` | View open orders |
| `get_trades(market_id=None)` | View filled trades |
| `get_bet_quote(market_id, side, amount)` | Client-side fill estimate |
| `get_orderbook_quote(market_id, side, amount)` | Order book walk for quotes |
| `place_bets_batch(bets)` | Batch place multiple bets |
| `analyze_market(market_id)` | Comprehensive market analysis |
| `recommend_trade(...)` | Build a trade recommendation card |

### Betting
| Method | Description |
|--------|-------------|
| `place_bet(market_id, side, amount)` | Place a parimutuel pool bet |
| `get_bets(user_id=None, kaspa_address=None)` | Fetch bet history |
| `create_bet_intent(market_id, side, ...)` | On-chain bet deposit (covenant) |
| `create_slot_bet_intent(round_id, bucket_id, side, ...)` | Predict Slot on-chain intent |
| `wait_for_bet(tx_id=None, reference_code=None, ...)` | Poll until bet is recorded |
| `place_bet_on_chain(market_id, side, wallet_address, amount)` | Full covenant bet helper |
| `confirm_bet(bet_id)` | Confirm a pending bet |
| `claim_bet(bet_id)` | Claim winning bet payout |
| `cashout_bet(bet_id)` | Early cashout |

### Predict Slot
| Method | Description |
|--------|-------------|
| `get_slot_round()` | Current active round |
| `get_slot_jackpot()` | Jackpot pool |
| `get_my_slot_bets()` | Your slot bet history |
| `claim_slot_bet(bet_id)` | Claim slot bet payout |

### Account
| Method | Description |
|--------|-------------|
| `get_user(kaspa_address=None)` | User profile and balance |
| `get_stats()` | Platform-wide statistics |
| `get_kas_price()` | Current KAS/USD price |
| `get_leaderboard(limit=20)` | Top traders |
| `get_watchlist()` | Your market watchlist |
| `toggle_watchlist(market_id)` | Add/remove from watchlist |
| `get_notifications()` | Your notifications |
| `subscribe_notifications(endpoint, p256dh, auth)` | Subscribe to push |
| `unsubscribe_notifications(endpoint)` | Unsubscribe from push |

### Finance
| Method | Description |
|--------|-------------|
| `get_deposits()` | Deposit history |
| `create_nowpayments_payment(amount, address)` | NOWPayments invoice |
| `create_kaspa_deposit_intent(amount, address)` | Native Kaspa deposit |
| `withdraw(user_id, amount, chain, address)` | Withdraw to source chain |
| `get_liquidity_pools(user_id=None)` | AMM pool state |
| `add_liquidity(market_id, user_id, amount)` | Add liquidity |
| `remove_liquidity(market_id, user_id, amount)` | Remove liquidity |

### Referrals & Proposals
| Method | Description |
|--------|-------------|
| `get_referral_info(user_id=None)` | Referral code and stats |
| `apply_referral_code(code, user_id)` | Apply a referral code |
| `get_proposals()` | Community market proposals |
| `submit_proposal(title, category, ...)` | Submit a new proposal |
| `vote_proposal(proposal_id, vote)` | Vote on a proposal |

### Utilities
| Method | Description |
|--------|-------------|
| `get_kaspa_tx(tx_id)` | Look up Kaspa transaction |
| `generate_token(name="agent")` | Generate agent token locally |

### Admin (requires master API key)
| Method | Description |
|--------|-------------|
| `create_market(market_id, title, ...)` | Create a new market |
| `create_agent_token(user_id, name, ...)` | Create agent token |
| `list_agent_tokens(user_id)` | List agent tokens |
| `revoke_agent_token(token_id, user_id)` | Revoke an agent token |
| `upsert_user_by_address(kaspa_address)` | Find/create user |

## Custody model

This SDK does **not** store API keys. Read `K100BET_API_KEY` from your local environment at runtime. Use a read-only token for analysis; use a `trade`-permission token only when the user has explicitly opted in.

In covenant mode the SDK returns deposit addresses and memos — it cannot sign Kaspa transactions. The user completes sends from Kasware/Kastle.

## Error handling

The SDK raises typed exceptions:

```python
from k100bet import K100bet
from k100bet.client import (
    K100betError,          # base
    K100betAuthError,      # 401
    K100betNotFoundError,  # 404
    K100betRateLimitError, # 429
    K100betValidationError,# 400
    K100betServerError,    # 5xx
)

k = K100bet(api_key="invalid")
try:
    k.get_markets()
except K100betAuthError:
    print("Bad API key")
except K100betRateLimitError:
    print("Rate limited — slow down")
```

## Legacy script

`k100bet-agent.py` in this directory remains as a backward-compatible entrypoint:

```bash
python k100bet-agent.py markets
```

Prefer `pip install k100bet` for new projects.
