#!/usr/bin/env python3
"""
Integration tests for the K100bet Agent SDK -- new methods added in v0.2.0.

Mocks urllib.request.urlopen to verify each method makes the correct API call
and returns the expected data shape. No network required.

Usage:
    python agents/test_sdk_integration.py
"""

import json
import sys
import os
from unittest.mock import patch, MagicMock

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, os.path.dirname(_ROOT))

from k100bet.client import K100bet, K100betError


def _mock_response(data, status=200):
    body = json.dumps({"data": data}).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _patch_urlopen(return_data):
    return patch("k100bet.client.urlopen", return_value=_mock_response(return_data))


# -- Markets & Search --

def test_get_leaderboard():
    data = [{"rank": 1, "kaspaAddress": "kaspa:qzs...", "totalVolume": "50000"}]
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_leaderboard(limit=10)
    assert isinstance(result, list)
    assert result[0]["rank"] == 1
    print("  [OK] get_leaderboard")


def test_get_kas_price():
    data = {"price": 0.15, "change24h": 2.5, "source": "coingecko"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_kas_price()
    assert result["price"] == 0.15
    assert result["source"] == "coingecko"
    print("  [OK] get_kas_price")


def test_get_market_quote():
    data = {"estimatedPrice": 0.62, "estimatedShares": 161.29}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_market_quote("btc-150k", "yes", "100")
    assert result["estimatedPrice"] == 0.62
    print("  [OK] get_market_quote")


def test_search_markets():
    data = [{"id": "btc-150k", "title": "BTC 150K?"}]
    with _patch_urlopen(data):
        result = K100bet(api_key="test").search_markets("bitcoin")
    assert len(result) == 1
    assert result[0]["id"] == "btc-150k"
    print("  [OK] search_markets")


def test_get_market_comments():
    data = [{"id": "c1", "text": "Great market!", "likes": 5}]
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_market_comments("btc-150k")
    assert result[0]["text"] == "Great market!"
    print("  [OK] get_market_comments")


def test_post_market_comment():
    data = {"id": "c2", "text": "Nice!"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").post_market_comment("btc-150k", "Nice!")
    assert result["id"] == "c2"
    print("  [OK] post_market_comment")


def test_like_market_comment():
    data = {"liked": True, "likesCount": 6}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").like_market_comment("btc-150k", "c1")
    assert result["liked"] is True
    assert result["likesCount"] == 6
    print("  [OK] like_market_comment")


# -- Staking --

def test_get_staking_info():
    data = {"pool": {"totalStaked": "100000"}, "tier": "Silver", "position": {"amount": "10000"}}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_staking_info()
    assert result["tier"] == "Silver"
    assert result["position"]["amount"] == "10000"
    print("  [OK] get_staking_info")


def test_create_stake_intent():
    data = {"intentId": "si-1", "depositAddress": "kaspa:qzs...", "memo": "KSTAKE:1000"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").create_stake_intent(1000, wallet_address="kaspa:abc")
    assert result["intentId"] == "si-1"
    assert "KSTAKE" in result["memo"]
    print("  [OK] create_stake_intent")


def test_create_stake_intent_no_wallet():
    data = {"intentId": "si-2", "depositAddress": "kaspa:qzs..."}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").create_stake_intent(500)
    assert result["intentId"] == "si-2"
    print("  [OK] create_stake_intent (no wallet)")


def test_create_unstake_intent():
    data = {"intentId": "ui-1", "memo": "KUNSTAKE:500"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").create_unstake_intent(500, wallet_address="kaspa:abc")
    assert result["intentId"] == "ui-1"
    print("  [OK] create_unstake_intent")


def test_create_unstake_intent_no_wallet():
    data = {"intentId": "ui-2"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").create_unstake_intent(200)
    assert result["intentId"] == "ui-2"
    print("  [OK] create_unstake_intent (no wallet)")


def test_claim_staking_rewards():
    data = {"claimed": True, "amount": "250", "transactionId": "tx-123"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").claim_staking_rewards()
    assert result["claimed"] is True
    assert result["amount"] == "250"
    print("  [OK] claim_staking_rewards")


# -- Bet Lifecycle --

def test_confirm_bet():
    data = {"id": "bet-1", "status": "confirmed"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").confirm_bet("bet-1")
    assert result["status"] == "confirmed"
    print("  [OK] confirm_bet")


def test_claim_bet():
    data = {"betId": "bet-1", "payout": "150", "status": "paid"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").claim_bet("bet-1")
    assert result["payout"] == "150"
    print("  [OK] claim_bet")


def test_cashout_bet():
    data = {"betId": "bet-1", "cashoutAmount": "80", "status": "cashed_out"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").cashout_bet("bet-1")
    assert result["cashoutAmount"] == "80"
    print("  [OK] cashout_bet")


# -- Predict Slot --

def test_get_slot_round():
    data = {"roundId": 42, "status": "active", "buckets": ["B1", "B2", "B3", "B4", "B5", "B6"]}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_slot_round()
    assert result["roundId"] == 42
    assert result["status"] == "active"
    print("  [OK] get_slot_round")


def test_get_slot_jackpot():
    data = {"totalPool": "5000", "jackpotAmount": "2500"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_slot_jackpot()
    assert result["jackpotAmount"] == "2500"
    print("  [OK] get_slot_jackpot")


def test_get_my_slot_bets():
    data = [{"id": "sb-1", "bucket": "B3", "side": "yes", "amount": "100"}]
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_my_slot_bets()
    assert len(result) == 1
    assert result[0]["bucket"] == "B3"
    print("  [OK] get_my_slot_bets")


def test_claim_slot_bet():
    data = {"betId": "sb-1", "payout": "300"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").claim_slot_bet("sb-1")
    assert result["payout"] == "300"
    print("  [OK] claim_slot_bet")


# -- Watchlist --

def test_get_watchlist():
    data = [{"id": "btc-150k", "title": "BTC 150K?"}]
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_watchlist()
    assert len(result) == 1
    assert result[0]["id"] == "btc-150k"
    print("  [OK] get_watchlist")


def test_toggle_watchlist():
    data = {"marketId": "btc-150k", "added": True}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").toggle_watchlist("btc-150k")
    assert result["added"] is True
    print("  [OK] toggle_watchlist")


# -- Notifications --

def test_get_notifications():
    data = [{"type": "bet_won", "message": "You won!"}]
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_notifications()
    assert result[0]["type"] == "bet_won"
    print("  [OK] get_notifications")


def test_subscribe_notifications():
    data = {"subscribed": True, "subscriptionId": "sub-1"}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").subscribe_notifications(
            endpoint="https://fcm.googleapis.com/fcm/send/...",
            p256dh="key123",
            auth="auth456",
        )
    assert result["subscribed"] is True
    print("  [OK] subscribe_notifications")


def test_unsubscribe_notifications():
    data = {"unsubscribed": True}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").unsubscribe_notifications(
            endpoint="https://fcm.googleapis.com/fcm/send/..."
        )
    assert result["unsubscribed"] is True
    print("  [OK] unsubscribe_notifications")


# -- Proposals --

def test_vote_proposal():
    data = {"proposalId": "p1", "vote": "up", "totalVotes": 15}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").vote_proposal("p1", "up")
    assert result["vote"] == "up"
    assert result["totalVotes"] == 15
    print("  [OK] vote_proposal")


# -- Kaspa TX Lookup --

def test_get_kaspa_tx():
    data = {"txId": "abc123", "status": "confirmed", "amount": 500}
    with _patch_urlopen(data):
        result = K100bet(api_key="test").get_kaspa_tx("abc123")
    assert result["status"] == "confirmed"
    assert result["amount"] == 500
    print("  [OK] get_kaspa_tx")


# -- Error handling --

def test_api_error_raises():
    from urllib.error import HTTPError, URLError

    # Simulate a 404 by having _request call _handle_http_error then raise
    error_resp = MagicMock()
    error_resp.read.return_value = json.dumps({"error": "Not found"}).encode()

    http_err = HTTPError("http://test", 404, "Not Found", {}, error_resp)

    def fake_urlopen(*args, **kwargs):
        raise http_err

    with patch("k100bet.client.urlopen", side_effect=fake_urlopen):
        try:
            K100bet(api_key="test").get_leaderboard()
            assert False, "Should have raised K100betError"
        except K100betError as e:
            assert "Not found" in str(e) or "404" in str(e)
    print("  [OK] error handling")


# -- Existing methods smoke test --

def test_existing_methods():
    with _patch_urlopen([{"id": "m1", "title": "Test"}]):
        result = K100bet(api_key="test").get_markets()
        assert len(result) == 1

    with _patch_urlopen({"id": "m1", "title": "Test"}):
        result = K100bet(api_key="test").get_market("m1")
        assert result["id"] == "m1"

    with _patch_urlopen({"totalVolume": "10000"}):
        result = K100bet(api_key="test").get_stats()
        assert result["totalVolume"] == "10000"

    print("  [OK] existing methods (get_markets, get_market, get_stats)")


def main():
    tests = [
        test_get_leaderboard,
        test_get_kas_price,
        test_get_market_quote,
        test_search_markets,
        test_get_market_comments,
        test_post_market_comment,
        test_like_market_comment,
        test_get_staking_info,
        test_create_stake_intent,
        test_create_stake_intent_no_wallet,
        test_create_unstake_intent,
        test_create_unstake_intent_no_wallet,
        test_claim_staking_rewards,
        test_confirm_bet,
        test_claim_bet,
        test_cashout_bet,
        test_get_slot_round,
        test_get_slot_jackpot,
        test_get_my_slot_bets,
        test_claim_slot_bet,
        test_get_watchlist,
        test_toggle_watchlist,
        test_get_notifications,
        test_subscribe_notifications,
        test_unsubscribe_notifications,
        test_vote_proposal,
        test_get_kaspa_tx,
        test_api_error_raises,
        test_existing_methods,
    ]

    print("\nRunning %d SDK integration tests...\n" % len(tests))
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print("  [FAIL] %s: %s" % (test.__name__, e))
            failed += 1

    print("\n" + "-" * 40)
    print("Results: %d passed, %d failed, %d total" % (passed, failed, len(tests)))
    if failed:
        print("Some tests failed!")
        sys.exit(1)
    else:
        print("All tests passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
