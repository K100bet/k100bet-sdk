#!/usr/bin/env python3
"""
K100bet Agent SDK — Trade prediction markets programmatically.

A zero-dependency Python SDK for AI agents (OpenClaw, Hermes, Claude Code, Pi, etc.)
to interact with K100bet — the world's fastest prediction market powered by a
CLOB (Continuous Limit Order Book).

Features:
  - CLOB limit orders — place, cancel, view order book
  - Market orders — cross the spread for instant fills
  - Order book quotes — estimate fills before placing
  - Full market discovery and analysis (order book depth, spread, midpoint)
  - TP/SL (Take Profit / Stop Loss) conditional orders
  - User balance and portfolio tracking
  - Deposit and withdrawal management
  - Kaspa native deposit intents (instant ~1s finality)
  - Real-time market streaming (SSE)
  - Agent token generation (local)
  - Batch operations and market analysis
  - Full admin API (create markets, manage tokens, upsert users)

Quick start:
    from k100bet_agent import K100bet

    # Initialize with your agent token
    k = K100bet(api_key="k100bet_a1b2c3...", base_url="https://k100bet.com")

    # List open markets
    markets = k.get_markets()
    for m in markets:
        print(f"{m['title']}: Yes {m['yesPrice']*100:.0f}c / No {m['noPrice']*100:.0f}c")

    # Place a limit order: buy YES at 60c per share
    order = k.place_limit_order(market_id="bitcoin-150k-2025",
                                 side="yes", amount="100", target_price=0.60)
    print(f"Order placed: {order['id']} — filled {order.get('filledAmount', '0')}")

    # Or cross the spread immediately
    order = k.place_market_order(market_id="bitcoin-150k-2025",
                                  side="yes", amount="100")
"""

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

try:
    from .virtual_wallet import VirtualWallet
except ImportError:  # noqa: BLE001
    try:
        from virtual_wallet import VirtualWallet
    except ImportError:  # noqa: BLE001
        VirtualWallet = None  # type: ignore[assignment]

try:
    from .trade_recommendation import TradeRecommendation
except ImportError:  # noqa: BLE001
    try:
        from trade_recommendation import TradeRecommendation
    except ImportError:  # noqa: BLE001
        TradeRecommendation = None  # type: ignore[assignment]


# House fee mirrors the live CLOB (2%) so virtual-wallet math and
# recommendation math stay consistent with on-platform payouts.
HOUSE_FEE_RATE = 0.02


# ============================================================
# Exceptions
# ============================================================

class K100betError(Exception):
    """Base exception for K100bet SDK errors."""
    pass


class K100betAuthError(K100betError):
    """Raised when authentication fails (invalid or missing API key)."""
    pass


class K100betRateLimitError(K100betError):
    """Raised when rate limited by the API."""
    pass


class K100betValidationError(K100betError):
    """Raised when request validation fails (missing fields, invalid values)."""
    pass


class K100betNotFoundError(K100betError):
    """Raised when a resource is not found."""
    pass


class K100betServerError(K100betError):
    """Raised when the K100bet server returns a 5xx error."""
    pass


# ============================================================
# Type Aliases
# ============================================================

MarketDict = Dict[str, Any]
BetDict = Dict[str, Any]
UserDict = Dict[str, Any]
StatsDict = Dict[str, Any]
PoolDict = Dict[str, Any]
ProposalDict = Dict[str, Any]
TokenDict = Dict[str, Any]
OrderDict = Dict[str, Any]
WithdrawalDict = Dict[str, Any]
PaymentDict = Dict[str, Any]


# ============================================================
# Main Client
# ============================================================

class K100bet:
    """
    K100bet Agent SDK — Main client for interacting with K100bet prediction markets.

    All API calls return parsed JSON dictionaries. Errors are raised as
    typed exceptions (K100betAuthError, K100betValidationError, etc.).

    Both sync and async patterns are supported:
        sync:   k = K100bet(api_key="..."); markets = k.get_markets()
        async:  use k.get_markets_async() via a thread pool

    Args:
        api_key: Agent API token (starts with "k100bet_") or master API key.
        base_url: K100bet API base URL. Defaults to "https://k100bet.com".
        timeout: HTTP request timeout in seconds. Default 30.
        user_agent: User-Agent header sent with requests. Default identifies this SDK.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://k100bet.com",
        timeout: int = 30,
        user_agent: Optional[str] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent or f"K100betAgentSDK/1.5"

        # SSE connection (lazy)
        self._sse_connection: Optional[Any] = None

    # ==========================================================
    # Internal HTTP Methods
    # ==========================================================

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "User-Agent": self.user_agent,
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, str]] = None,
        body: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """Make an HTTP request to the K100bet API.

        Retries transient failures (429, 502, 503, 504, URLError) up to
        ``max_retries`` times with exponential backoff (1s, 2s, 4s). When a
        429 response carries a ``Retry-After`` header, that value is honored
        in place of the exponential delay. Persistent client errors
        (401/403/404/400) are surfaced immediately as typed exceptions.
        ``max_retries`` is clamped to >= 1 so callers who pass 0 get one
        attempt instead of the misleading "Exhausted 0 retries" message.
        """
        max_retries = max(1, int(max_retries))
        url = f"{self.base_url}{path}"
        if params:
            # Filter out None values
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                url += "?" + urlencode(filtered)

        data = json.dumps(body).encode("utf-8") if body and method in ("POST", "PUT", "PATCH") else None

        req = Request(url, data=data, method=method, headers=self._headers())

        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                with urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw)
            except HTTPError as e:
                # Persistent client error (other 4xx) → surface immediately.
                # Transient server / rate-limit → retry.
                if e.code in (429, 502, 503, 504):
                    last_err = e
                    if attempt >= max_retries:
                        self._handle_http_error(e, path)
                        raise RuntimeError(  # unreachable: _handle_http_error always raises
                            f"_handle_http_error did not raise for {method} {path}"
                        )
                    backoff_s = 2 ** (attempt - 1)
                    retry_after: Optional[str] = None
                    try:
                        retry_after = e.headers.get("Retry-After") if e.headers else None
                    except Exception:  # noqa: BLE001
                        retry_after = None
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.replace(".", "", 1).isdigit()
                        else backoff_s
                    )
                    time.sleep(delay)
                    continue
                self._handle_http_error(e, path)
                raise RuntimeError(  # unreachable: _handle_http_error always raises
                    f"_handle_http_error did not raise for {method} {path}"
                )
            except URLError as e:
                last_err = e
                if attempt >= max_retries:
                    raise K100betError(f"Connection error: {e.reason}") from e
                time.sleep(2 ** (attempt - 1))
                continue
        # Loop exhausted without success — should not be reachable, but if it
        # is, surface the last transient error rather than silently return.
        raise K100betError(
            f"Exhausted {max_retries} retries on {method} {path}: {last_err}"
        )

    def _handle_http_error(self, error: HTTPError, path: str) -> None:
        """Map HTTP errors to typed exceptions."""
        status = error.code
        try:
            body = json.loads(error.read().decode("utf-8"))
            err_msg = body.get("error", str(error))
        except (json.JSONDecodeError, AttributeError):
            err_msg = str(error)

        if status == 401:
            raise K100betAuthError(f"Authentication failed for {path}: {err_msg}")
        elif status == 404:
            raise K100betNotFoundError(f"Resource not found at {path}: {err_msg}")
        elif status == 429:
            raise K100betRateLimitError(f"Rate limited on {path}: {err_msg}")
        elif 400 <= status < 500:
            raise K100betValidationError(f"Validation error on {path}: {err_msg}")
        elif status >= 500:
            raise K100betServerError(f"Server error on {path}: {err_msg}")
        else:
            raise K100betError(f"HTTP {status} on {path}: {err_msg}")

    # ==========================================================
    # Agent Token Generation (local, no API call)
    # ==========================================================

    @staticmethod
    def generate_token(name: str = "agent") -> TokenDict:
        """
        Generate a new K100bet agent API token locally.

        This generates a cryptographically random token with the format
        ``k100bet_`` + 40 hex characters. The raw token should be stored
        securely and will only be shown once.

        Use the K100bet web UI (/agents) to register this token with a user.

        Returns:
            dict with keys: rawToken, tokenHash (SHA-256), name
        """
        raw_bytes = os.urandom(20)
        hex_part = raw_bytes.hex()
        raw_token = f"k100bet_{hex_part}"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        return {
            "rawToken": raw_token,
            "tokenHash": token_hash,
            "name": name,
        }

    # ==========================================================
    # Markets
    # ==========================================================

    def get_markets(self, category: Optional[str] = None) -> List[MarketDict]:
        """
        Fetch all markets, optionally filtered by category.

        Args:
            category: Optional filter. One of: politics, crypto, sports,
                      economics, technology, climate, entertainment.

        Returns:
            List of market objects with keys: id, title, description, category,
            yesPrice, noPrice, volume, liquidity, status, endTime, etc.
        """
        params = {"category": category} if category else None
        result = self._request("GET", "/api/markets", params=params)
        return result.get("data", [])

    def get_market(self, market_id: str) -> Optional[MarketDict]:
        """
        Fetch a single market by its ID/slug.

        Args:
            market_id: The market slug (e.g. "bitcoin-150k-2025").

        Returns:
            Market dict, or None if not found.
        """
        try:
            result = self._request("GET", f"/api/markets/{market_id}")
            return result.get("data")
        except K100betNotFoundError:
            return None

    def get_markets_stream_url(self) -> str:
        """
        Get the URL for the Server-Sent Events (SSE) market stream.

        Returns:
            Full URL string for the SSE endpoint.
        """
        return f"{self.base_url}/api/markets/stream"

    def stream_markets(self) -> Iterator[List[MarketDict]]:
        """
        Stream real-time market data updates via Server-Sent Events.

        This is a generator that yields parsed market lists as they arrive
        from the SSE stream. No third-party dependencies needed.

        Usage:
            k = K100bet(api_key="...")
            for markets in k.stream_markets():
                for m in markets:
                    print(f"{m['id']}: Yes {m['yesPrice']:.2f}")

        Yields:
            List of market dicts with keys: id, yesPrice, noPrice,
            volume, liquidity, etc.
        """
        import http.client

        u = urlparse(self.get_markets_stream_url())
        host = f"{u.hostname}:{u.port}" if u.port else u.hostname
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        use_ssl = u.scheme == "https"

        conn = http.client.HTTPSConnection(host, timeout=self.timeout) if use_ssl else \
            http.client.HTTPConnection(host, timeout=self.timeout)

        try:
            conn.request("GET", path, headers={
                "Accept": "text/event-stream",
                "x-api-key": self.api_key,
                "User-Agent": self.user_agent,
            })
            resp = conn.getresponse()
            if resp.status != 200:
                raise K100betServerError(
                    f"SSE stream returned HTTP {resp.status}: {resp.reason}"
                )
            buffer = ""
            while True:
                chunk = resp.read(4096).decode("utf-8")
                if not chunk:
                    break
                buffer += chunk
                while "\n\n" in buffer:
                    event_block, buffer = buffer.split("\n\n", 1)
                    for line in event_block.split("\n"):
                        if line.startswith("data: "):
                            data_str = line[6:]
                            try:
                                yield json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
        except K100betError:
            raise
        except Exception as e:
            raise K100betServerError(f"SSE stream connection failed: {e}") from e
        finally:
            conn.close()

    # ==========================================================
    # Betting
    # ==========================================================

    def get_bets(
        self,
        user_id: Optional[str] = None,
        kaspa_address: Optional[str] = None,
    ) -> List[BetDict]:
        """
        Fetch bets for a user.

        Args:
            user_id: User's UUID (from get_user or web UI).
            kaspa_address: Alternative lookup by Kaspa address.

        Returns:
            List of bet objects with keys: id, marketId, side, amount, odds,
            potentialPayout, status, placedAt, etc.
        """
        params: Dict[str, str] = {}
        if user_id:
            params["user_id"] = user_id
        if kaspa_address:
            params["kaspa_address"] = kaspa_address

        result = self._request("GET", "/api/bets", params=params if params else None)
        return result.get("data", [])

    def place_bet(
        self,
        market_id: str,
        side: str,
        amount: Union[str, float, int],
        user_id: Optional[str] = None,
    ) -> BetDict:
        """
        Place a parimutuel pool bet on behalf of the API token owner.

        Requires an agent API token (k100bet_...) with trade permission.
        Stake is debited from the user's platform KAS balance (kusdc_balance).
        Deposit KAS first via the wallet/deposit flow if balance is zero.

        Args:
            market_id: The market slug (e.g. "crypto-1h-eth-up-202606100900").
            side: "yes" or "no".
            amount: KAS stake (string or number).
            user_id: Optional — only for admin API keys; ignored for agent tokens.

        Returns:
            Bet result dict with keys: id, marketId, side, amount, odds,
            potentialPayout, houseFee, yesPrice, noPrice, status, placedAt.
        """
        body: Dict[str, Any] = {
            "marketId": market_id,
            "side": side.lower(),
            "amount": str(amount),
        }
        if user_id:
            body["userId"] = user_id
        result = self._request("POST", "/api/bets", body=body)
        return result.get("data", {})

    # ==========================================================
    # Covenant / On-chain Betting (COVENANT_ONLY=true)
    # ==========================================================

    def create_bet_intent(
        self,
        market_id: str,
        side: str,
        wallet_address: Optional[str] = None,
        price: Optional[Union[str, float]] = None,
    ) -> Dict[str, Any]:
        """
        Create an on-chain bet intent (covenant mode).

        Returns depositAddress + KBET: memo. Send KAS from the wallet
        externally (Kasware/Kastle sendKaspa) — the deposit listener
        matches the memo and records the bet.

        Args:
            market_id: Market slug.
            side: "yes" or "no".
            wallet_address: Payer kaspatest:/kaspa: address (uses linked user address if omitted).
            price: Optional limit price (0.01–0.99) for ORDER: memos.

        Returns:
            dict with intentId, referenceCode, depositAddress, memo, marketId, side.
        """
        body: Dict[str, Any] = {
            "marketId": market_id,
            "side": side.lower(),
        }
        if wallet_address:
            body["walletAddress"] = wallet_address
        if price is not None:
            body["price"] = float(price)
        result = self._request("POST", "/api/bets/intent", body=body)
        return result.get("data", {})

    def create_slot_bet_intent(
        self,
        round_id: int,
        bucket_id: str,
        side: str,
        wallet_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a Predict Slot on-chain bet intent.

        Returns depositAddress + KSLOT: memo for wallet.sendKaspa.

        Args:
            round_id: Active slot round number.
            bucket_id: Target bucket id.
            side: "yes" or "no".
            wallet_address: Payer Kaspa address (uses linked user if omitted).
        """
        body: Dict[str, Any] = {
            "roundId": round_id,
            "bucketId": bucket_id,
            "side": side.lower(),
        }
        if wallet_address:
            body["walletAddress"] = wallet_address
        result = self._request("POST", "/api/predict-slot/bet/intent", body=body)
        return result.get("data", {})

    def wait_for_bet(
        self,
        *,
        tx_id: Optional[str] = None,
        reference_code: Optional[str] = None,
        market_id: Optional[str] = None,
        timeout: int = 120,
        poll_interval: float = 3.0,
    ) -> Optional[BetDict]:
        """
        Poll GET /api/bets until a bet matching tx_id or reference_code appears.

        At least one of tx_id or reference_code should be provided.
        Returns the bet dict when found, or None on timeout.
        """
        if not tx_id and not reference_code:
            raise K100betValidationError("wait_for_bet requires tx_id or reference_code")

        deadline = time.time() + timeout
        while time.time() < deadline:
            bets = self.get_bets()
            for bet in bets:
                if market_id and bet.get("marketId") != market_id:
                    continue
                kaspa_tx = bet.get("kaspaTxId") or bet.get("kaspa_tx_id") or ""
                if tx_id and kaspa_tx == tx_id:
                    return bet
                ref = bet.get("referenceCode") or bet.get("reference_code") or ""
                if reference_code and ref == reference_code:
                    return bet
                if reference_code and kaspa_tx and reference_code in kaspa_tx:
                    return bet
            time.sleep(poll_interval)
        return None

    def place_bet_on_chain(
        self,
        market_id: str,
        side: str,
        wallet_address: str,
        amount_kas: Union[str, float, int],
        *,
        wait: bool = False,
        wait_timeout: int = 120,
    ) -> Dict[str, Any]:
        """
        Covenant-mode bet helper: create intent only (no wallet signing in SDK).

        The agent must send KAS from the linked wallet with the returned memo.
        Use wait=True to poll until the deposit listener records the bet.

        Returns:
            intent dict plus optional ``bet`` key when wait=True and bet is found.
        """
        intent = self.create_bet_intent(market_id, side, wallet_address=wallet_address)
        intent["amountKas"] = str(amount_kas)
        intent["instructions"] = (
            f"Send {amount_kas} KAS to {intent.get('depositAddress')} "
            f"with memo: {intent.get('memo')}"
        )
        if wait:
            intent["bet"] = self.wait_for_bet(
                reference_code=intent.get("referenceCode"),
                market_id=market_id,
                timeout=wait_timeout,
            )
        return intent

    def get_bet_quote(
        self,
        market_id: str,
        side: str,
        amount: Union[str, float, int],
    ) -> Optional[Dict[str, Any]]:
        """
        Estimate a trade quote using the live CLOB order book.

        Walks the order book to estimate fill price, shares, and slippage
        for the given amount. This is a client-side estimation — actual
        fills may differ as the book changes.

        Args:
            market_id: The market slug.
            side: "yes" or "no".
            amount: Amount of KAS to spend.

        Returns:
            Quote dict with keys: shares, cost, odds, potentialPayout, houseFee, slippage.
            Returns None if no liquidity exists.
        """
        quote = self.get_orderbook_quote(market_id, side, amount)
        if not quote or quote["fillableAmount"] <= 0:
            return None

        shares = quote["estimatedShares"]
        cost = quote["fillableAmount"]
        avg_price = quote["estimatedPrice"]

        house_fee = cost * 0.02
        potential_payout = shares - house_fee  # Each share pays $1.00 on win

        return {
            "marketId": market_id,
            "side": side.lower(),
            "amount": str(amount),
            "shares": round(shares, 6),
            "cost": round(cost, 6),
            "odds": round(1.0 / avg_price, 6) if avg_price > 0 else 0,
            "avgPrice": round(avg_price, 6),
            "potentialPayout": round(potential_payout, 6),
            "houseFee": round(house_fee, 6),
            "fillableAmount": round(cost, 6),
            "unfillableAmount": round(quote["unfillableAmount"], 6),
            "levelsUsed": quote["levelsUsed"],
        }

    @staticmethod
    def _estimate_shares(total_yes: float, total_no: float,
                         amount: float, side: str) -> float:
        """Binary search for shares given a cost amount."""
        k = total_yes * total_no
        if k <= 0 or amount <= 0:
            return 0

        lo, hi = 0.0, amount * 10.0
        for _ in range(100):
            mid = (lo + hi) / 2.0
            if side == "yes":
                cost = total_no - (k / (total_yes + mid))
            else:
                cost = total_yes - (k / (total_no + mid))
            if cost < amount:
                lo = mid
            else:
                hi = mid
        return lo

    # ==========================================================
    # User
    # ==========================================================

    def get_user(self, kaspa_address: Optional[str] = None) -> UserDict:
        """
        Fetch or create a user profile by Kaspa address.

        If the address doesn't exist yet, it will be upserted (created).
        If no address is provided, the API may return an error.

        Args:
            kaspa_address: Kaspa wallet address (e.g. "kaspa:qzs...").

        Returns:
            User dict with keys: kaspaAddress, kasBalance, totalBets,
            winRate, positions, createdAt, etc.
        """
        params = {"kaspa_address": kaspa_address} if kaspa_address else None
        result = self._request("GET", "/api/user", params=params)
        return result.get("data", {})

    def upsert_user(self, kaspa_address: str) -> UserDict:
        """
        Find or create a user by Kaspa address, then return their profile.

        This calls the GET /api/user endpoint which upserts automatically.

        Args:
            kaspa_address: Kaspa wallet address (e.g. "kaspa:qzs...").

        Returns:
            User profile dict.
        """
        return self.get_user(kaspa_address=kaspa_address)

    # ==========================================================
    # Stats
    # ==========================================================

    def get_stats(self) -> StatsDict:
        """
        Fetch platform-wide statistics.

        Returns:
            Stats dict with keys: totalVolume, totalMarkets, activeMarkets,
            totalUsers, totalBets, houseRevenue, averageBetSize.
        """
        result = self._request("GET", "/api/stats")
        return result.get("data", {})

    # ==========================================================
    # Conditional Orders (TP/SL)
    # ==========================================================

    def create_conditional_order(
        self,
        market_id: str,
        position_side: str,
        order_type: str,
        target_price: Union[str, float],
        shares_to_sell: Union[str, float],
    ) -> OrderDict:
        """
        Create a TP (take profit) or SL (stop loss) conditional order.

        When the market's yesPrice crosses the target_price, the order
        executes automatically — exiting the position at the target.

        Args:
            market_id: The market slug.
            position_side: The side of your position ("yes" or "no").
            order_type: "take_profit" or "stop_loss".
            target_price: Probability level (0.01–0.99) at which the
                          order triggers.
            shares_to_sell: Number of position shares to sell.

        Returns:
            Order dict with keys: id, marketId, type, positionSide,
            targetPrice, sharesToSell, status, createdAt.
        """
        if order_type not in ("take_profit", "stop_loss"):
            raise K100betValidationError(
                "order_type must be 'take_profit' or 'stop_loss'"
            )

        target = float(target_price)
        if target < 0.01 or target > 0.99:
            raise K100betValidationError(
                "target_price must be between 0.01 and 0.99"
            )

        body = {
            "marketId": market_id,
            "positionSide": position_side,
            "type": order_type,
            "targetPrice": target,
            "sharesToSell": str(shares_to_sell),
        }
        result = self._request("POST", "/api/orders", body=body)
        return result.get("data", {})

    def get_conditional_orders(
        self, market_id: Optional[str] = None
    ) -> List[OrderDict]:
        """
        Fetch active conditional orders (TP/SL).

        Args:
            market_id: Optional market ID to filter by.

        Returns:
            List of order dicts with keys: id, marketId, type,
            positionSide, targetPrice, sharesToSell, status, createdAt.
        """
        params = {"marketId": market_id} if market_id else None
        result = self._request("GET", "/api/orders", params=params)
        return result.get("data", [])

    def cancel_conditional_order(self, order_id: str) -> bool:
        """
        Cancel an active conditional order.

        Args:
            order_id: The order UUID.

        Returns:
            True if cancelled successfully.
        """
        result = self._request("DELETE", "/api/orders", params={"id": order_id})
        return result.get("data", {}).get("status") == "cancelled"

    def get_deposits(self) -> List[Dict[str, Any]]:
        """
        Fetch all deposit events.

        Returns:
            List of deposit event dicts with keys: id, txHash, sourceChain,
            amount, status, timestamp, confirmations, etc.
        """
        result = self._request("GET", "/api/deposits")
        return result.get("data", [])

    def create_nowpayments_payment(
        self,
        amount: Union[str, float, int],
        user_kaspa_address: str,
        source_chain: str = "nowpayments",
    ) -> PaymentDict:
        """
        Create a NOWPayments payment invoice for depositing KAS.

        The user sends crypto (BTC, ETH, USDT, KAS, etc.) to the returned
        deposit address. Funds credit on Kaspa finality.

        Args:
            amount: USD amount to deposit.
            user_kaspa_address: Your Kaspa address for KAS minting.
            source_chain: Payment method. Default "nowpayments".

        Returns:
            Payment dict with keys: paymentId, depositAddress, payAmount,
            payCurrency, priceAmount, expirationEstimate.
        """
        body = {
            "amount": str(amount),
            "userKaspaAddress": user_kaspa_address,
            "sourceChain": source_chain,
        }
        result = self._request("POST", "/api/create-nowpayments-payment", body=body)
        # The API may return the payment data directly or wrapped in a "data" key
        return result.get("data", result)

    def create_kaspa_deposit_intent(
        self,
        amount: Union[str, float, int],
        user_kaspa_address: str,
    ) -> Dict[str, Any]:
        """
        Create a native Kaspa deposit intent for minting KAS.

        Unlike NOWPayments (which converts crypto -> USD -> KAS via a
        third-party API), Kaspa native deposits keep everything on-chain:

            KAS -> Platform wallet -> Kaspa listener -> KAS minted 1:1

        Kaspa finality is ~1 second, making this the fastest deposit route.
        The user sends KAS to the returned deposit address with the
        reference code in the memo field.

        Args:
            amount: USD value to deposit.
            user_kaspa_address: Your Kaspa address for KAS minting
                                (must start with "kaspa:").

        Returns:
            Deposit intent dict with keys: depositAddress, referenceCode,
            expirationEstimate, minDeposit.
        """
        body = {
            "amount": str(amount),
            "userKaspaAddress": user_kaspa_address,
        }
        result = self._request("POST", "/api/deposits/kaspa", body=body)
        return result.get("data", result)

    # ==========================================================
    # Withdrawals
    # ==========================================================

    def withdraw(
        self,
        user_id: str,
        amount: Union[str, float, int],
        source_chain: str,
        destination_address: str,
    ) -> WithdrawalDict:
        """
        Withdraw KAS back to a source chain.

        KAS is burned on Kaspa, and USDC is released on the source chain.

        Args:
            user_id: Your user UUID.
            amount: Amount of KAS to withdraw (minimum 1).
            source_chain: Target chain: "ethereum", "arbitrum", or "solana".
            destination_address: Address on the source chain to receive USDC.

        Returns:
            Withdrawal dict with keys: id, amount, sourceChain, status,
            kasBurned, burnTxId, message.
        """
        body = {
            "userId": user_id,
            "amount": str(amount),
            "sourceChain": source_chain,
            "destinationAddress": destination_address,
        }
        result = self._request("POST", "/api/withdraw", body=body)
        return result.get("data", {})

    # ==========================================================
    # Liquidity
    # ==========================================================

    def get_liquidity_pools(self, user_id: Optional[str] = None) -> List[PoolDict]:
        """
        Fetch liquidity pool state for all markets.

        Args:
            user_id: Optional user UUID (currently unused, pools are global).

        Returns:
            List of pool dicts with keys: marketId, totalYes, totalNo,
            houseFee, totalPool, marketTitle, etc.
        """
        params = {"user_id": user_id} if user_id else None
        result = self._request("GET", "/api/liquidity", params=params)
        return result.get("data", [])

    def add_liquidity(
        self,
        market_id: str,
        user_id: str,
        amount: Union[str, float, int],
    ) -> PoolDict:
        """
        Add liquidity to a market's AMM pool.

        Liquidity is split 50/50 between Yes and No to provide balanced
        depth. In return you earn a share of the 2% house fee.

        Args:
            market_id: The market slug.
            user_id: Your user UUID.
            amount: KAS amount to add.

        Returns:
            Pool update dict with keys: marketId, action, amount,
            totalYes, totalNo, totalPool.
        """
        body = {
            "marketId": market_id,
            "userId": user_id,
            "amount": str(amount),
            "action": "add",
        }
        result = self._request("POST", "/api/liquidity", body=body)
        return result.get("data", {})

    def remove_liquidity(
        self,
        market_id: str,
        user_id: str,
        amount: Union[str, float, int],
    ) -> PoolDict:
        """
        Remove liquidity from a market's AMM pool.

        KAS is returned proportionally from the Yes and No sides.

        Args:
            market_id: The market slug.
            user_id: Your user UUID.
            amount: KAS amount to remove.

        Returns:
            Pool update dict with keys: marketId, action, amount, returnedToUser.
        """
        body = {
            "marketId": market_id,
            "userId": user_id,
            "amount": str(amount),
            "action": "remove",
        }
        result = self._request("POST", "/api/liquidity", body=body)
        return result.get("data", {})

    # ==========================================================
    # Referrals
    # ==========================================================

    def get_referral_info(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch referral code and stats.

        Args:
            user_id: Optional user UUID.

        Returns:
            Referral dict with keys: code, totalReferrals, totalEarned,
            referralBonusPercent, referralLink, etc.
        """
        params = {"user_id": user_id} if user_id else None
        result = self._request("GET", "/api/referrals", params=params)
        return result.get("data", {})

    def apply_referral_code(self, code: str, user_id: str) -> Dict[str, Any]:
        """
        Apply a referral code to a user account.

        Args:
            code: Referral code (e.g. "KAS-ABCDEF").
            user_id: Your user UUID.

        Returns:
            Result dict with keys: applied, bonusPercent, message.
        """
        body = {"code": code, "userId": user_id}
        result = self._request("POST", "/api/referrals", body=body)
        return result.get("data", {})

    # ==========================================================
    # Market Proposals
    # ==========================================================

    def get_proposals(self) -> List[ProposalDict]:
        """
        Fetch all market proposals submitted by the community.

        Returns:
            List of proposal dicts with keys: id, title, description,
            category, endTime, status, resolutionCriteria, votes, etc.
        """
        result = self._request("GET", "/api/market-proposals")
        return result.get("data", [])

    def submit_proposal(
        self,
        title: str,
        category: str,
        end_time: Union[str, datetime],
        resolution_criteria: str,
        description: str = "",
        tags: Optional[List[str]] = None,
        submitted_by: str = "",
    ) -> ProposalDict:
        """
        Submit a new market proposal.

        Proposals are reviewed by admins before becoming live markets.

        Args:
            title: Market question (min 5 chars).
            category: One of: politics, crypto, sports, economics,
                     technology, climate, entertainment.
            end_time: ISO datetime string or datetime object for market closure.
            resolution_criteria: How the market will be settled (min 10 chars).
            description: Optional detailed description.
            tags: Optional list of tag strings.
            submitted_by: Optional identifier for the submitter.

        Returns:
            Created proposal dict with keys: id, title, status, etc.
        """
        if isinstance(end_time, datetime):
            end_time = end_time.isoformat()

        body = {
            "title": title,
            "description": description,
            "category": category,
            "tags": tags or [],
            "endTime": end_time,
            "resolutionCriteria": resolution_criteria,
            "submittedBy": submitted_by,
        }
        result = self._request("POST", "/api/market-proposals", body=body)
        return result.get("data", {})

    # ==========================================================
    # Batch Operations
    # ==========================================================

    def place_bets_batch(self, bets: List[Dict[str, Any]]) -> List[BetDict]:
        """
        Place multiple bets sequentially (no batch endpoint yet).

        Useful for agents that need to enter multiple positions.
        Each bet is independent — one failure won't affect others.

        Args:
            bets: List of bet dicts, each with keys:
                  marketId, userId, side, amount.

        Returns:
            List of bet result dicts. Failed bets include an "error" key.
        """
        results = []
        for bet in bets:
            try:
                result = self.place_bet(
                    market_id=bet["marketId"],
                    side=bet["side"],
                    amount=bet["amount"],
                    user_id=bet.get("userId"),
                )
                results.append(result)
            except K100betError as e:
                results.append({
                    "marketId": bet.get("marketId"),
                    "side": bet.get("side"),
                    "amount": str(bet.get("amount", "")),
                    "error": str(e),
                })
        return results

    # ============================================================
    # CLOB Limit Order Trading
    # ============================================================

    def place_limit_order(
        self,
        market_id: str,
        side: str,
        amount: Union[str, float, int],
        target_price: Union[str, float],
    ) -> Dict[str, Any]:
        """
        Place a CLOB limit order at a specific price.

        Args:
            market_id: The market slug (e.g. "btc-150k-2025").
            side: "yes" or "no".
            amount: Order amount in KAS.
            target_price: Limit price between 0.01 and 0.99.

        Returns:
            Order result dict with id, status, filledAmount, matchedCount.
        """
        body = {
            "marketId": market_id,
            "side": side,
            "amount": str(amount),
            "targetPrice": float(target_price),
        }
        result = self._request("POST", "/api/limit-orders", body=body)
        return result.get("data", {})

    def get_orderbook(self, market_id: str) -> Dict[str, Any]:
        """
        Get the live order book for a market.

        Args:
            market_id: The market slug.

        Returns:
            Dict with bids and asks arrays, each entry has price, amount, total.
        """
        result = self._request("GET", f"/api/markets/{market_id}/orderbook")
        return result.get("data", {"bids": [], "asks": []})

    def get_open_orders(self, market_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get your open limit orders, optionally filtered by market.

        Args:
            market_id: Optional market slug to filter by.

        Returns:
            List of open order dicts.
        """
        params = {}
        if market_id:
            params["marketId"] = market_id
        result = self._request("GET", "/api/limit-orders", params=params)
        return result.get("data", [])

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Cancel an open limit order.

        Args:
            order_id: The order UUID to cancel.

        Returns:
            Cancellation confirmation dict.
        """
        result = self._request("DELETE", f"/api/limit-orders?id={order_id}")
        return result.get("data", {})

    def place_market_order(
        self,
        market_id: str,
        side: str,
        amount: Union[str, float, int],
    ) -> Dict[str, Any]:
        """
        Place a market order that crosses the spread immediately.

        Estimates the best available price from the order book and
        places a limit order at that price to fill immediately.

        Args:
            market_id: The market slug.
            side: "yes" or "no".
            amount: Order amount in KAS.

        Returns:
            Order result dict.
        """
        book = self.get_orderbook(market_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        # For buying YES: fill against asks (NO side). Best ask = lowest NO price.
        # For buying NO: fill against bids (YES side). Best bid = highest YES price.
        if side == "yes" and asks:
            best_price = float(asks[0]["price"])
        elif side == "no" and bids:
            best_price = float(bids[0]["price"])
        else:
            # No liquidity — place at 0.50
            best_price = 0.50

        return self.place_limit_order(market_id, side, amount, best_price)

    def get_orderbook_quote(
        self,
        market_id: str,
        side: str,
        amount: Union[str, float, int],
    ) -> Dict[str, Any]:
        """
        Estimate fill price and shares from the order book without placing an order.

        Args:
            market_id: The market slug.
            side: "yes" or "no".
            amount: Amount in KAS to spend.

        Returns:
            Dict with estimatedPrice, estimatedShares, fillableAmount, depth.
        """
        book = self.get_orderbook(market_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        target_amount = float(amount)
        remaining = target_amount
        total_shares = 0.0
        levels_used = 0

        # For buying YES: fill against asks (NO side, sorted by price asc)
        # For buying NO: fill against bids (YES side, sorted by price desc)
        levels = asks if side == "yes" else bids

        for level in levels:
            price = float(level["price"])
            available = float(level["amount"])
            cost_at_level = price * available
            if remaining <= 0:
                break
            spend = min(remaining, cost_at_level)
            shares = spend / price if price > 0 else 0
            total_shares += shares
            remaining -= spend
            levels_used += 1

        filled = target_amount - remaining
        avg_price = filled / total_shares if total_shares > 0 else 0

        return {
            "marketId": market_id,
            "side": side,
            "amount": str(target_amount),
            "estimatedPrice": round(avg_price, 6),
            "estimatedShares": round(total_shares, 6),
            "fillableAmount": round(filled, 6),
            "unfillableAmount": round(remaining, 6),
            "levelsUsed": levels_used,
        }

    def get_trades(self, market_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get your filled trades, optionally filtered by market.

        Args:
            market_id: Optional market slug to filter by.

        Returns:
            List of trade dicts with price, amount, side, timestamps.
        """
        params = {}
        if market_id:
            params["marketId"] = market_id
        result = self._request("GET", "/api/trades", params=params)
        return result.get("data", [])

    def analyze_market(self, market_id: str) -> Dict[str, Any]:
        """
        Perform a comprehensive analysis of a market using CLOB order book data.

        Combines market data, order book depth, and stats into a single
        analysis dict useful for agent decision-making.

        Args:
            market_id: The market slug.

        Returns:
            Analysis dict with keys: market, orderbook, pricing, depth, spread.
        """
        market = self.get_market(market_id)
        if not market:
            raise K100betNotFoundError(f"Market '{market_id}' not found")

        try:
            book = self.get_orderbook(market_id)
        except Exception:
            book = {"bids": [], "asks": []}

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        yes_price = market.get("yesPrice", 0.5)
        no_price = market.get("noPrice", 0.5)

        # Order book depth
        bid_depth = sum(float(b["amount"]) for b in bids)
        ask_depth = sum(float(a["amount"]) for a in asks)
        total_depth = bid_depth + ask_depth

        # Spread
        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        spread = best_ask - best_bid if best_bid > 0 and best_ask < 1 else 1.0

        # Imbalance
        imbalance = abs(bid_depth - ask_depth) / max(bid_depth, ask_depth) if max(bid_depth, ask_depth) > 0 else 0

        # Quote estimates for 100 KAS
        yes_quote = self.get_orderbook_quote(market_id, "yes", 100)
        no_quote = self.get_orderbook_quote(market_id, "no", 100)

        return {
            "market": market,
            "orderbook": {
                "bids": bids[:10],
                "asks": asks[:10],
                "bid_levels": len(bids),
                "ask_levels": len(asks),
            },
            "pricing": {
                "yes_price": yes_price,
                "no_price": no_price,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": round(spread, 6),
                "spread_pct": f"{spread * 100:.2f}%",
                "midpoint": round((best_bid + best_ask) / 2, 6) if best_bid > 0 and best_ask < 1 else yes_price,
            },
            "depth": {
                "bid_depth_kas": round(bid_depth, 2),
                "ask_depth_kas": round(ask_depth, 2),
                "total_depth_kas": round(total_depth, 2),
                "imbalance_ratio": round(imbalance, 4),
            },
            "quotes": {
                "buy_100_yes": yes_quote,
                "buy_100_no": no_quote,
            },
            "volume": {
                "volume_kas": market.get("volume", "0"),
                "liquidity_kas": market.get("liquidity", "0"),
            },
        }

    # ==========================================================
    # Admin Operations (requires master API key)
    # ==========================================================

    def create_market(
        self,
        market_id: str,
        title: str,
        category: str,
        end_time: Union[str, datetime],
        description: str = "",
        image_url: str = "",
    ) -> MarketDict:
        """
        Create a new prediction market (admin only).

        Requires the master API_KEY, not an agent token.

        Args:
            market_id: URL-safe slug (e.g. "will-btc-reach-200k").
            title: Market question.
            category: Market category.
            end_time: ISO datetime string or datetime object.
            description: Optional description.
            image_url: Optional image URL.

        Returns:
            Created market dict.
        """
        if isinstance(end_time, datetime):
            end_time = end_time.isoformat()

        body = {
            "id": market_id,
            "title": title,
            "description": description,
            "category": category,
            "imageUrl": image_url,
            "endTime": end_time,
        }
        result = self._request("POST", "/api/markets", body=body)
        return result.get("data", {})

    def create_agent_token(
        self,
        user_id: str,
        name: str,
        permissions: Optional[Dict[str, bool]] = None,
    ) -> TokenDict:
        """
        Create an agent API token for a user (admin only).

        Requires the master API_KEY.

        Args:
            user_id: User UUID to assign the token to.
            name: Human-readable name for the token.
            permissions: Dict like {"trade": true, "read": true}.
                        Defaults to trade + read.

        Returns:
            Token dict with keys: id, rawToken (⚠️ show once!), prefix, name.
        """
        body = {
            "userId": user_id,
            "name": name,
            "permissions": permissions or {"trade": True, "read": True},
        }
        result = self._request("POST", "/api/agents/tokens", body=body)
        return result.get("data", {})

    def list_agent_tokens(self, user_id: str) -> List[TokenDict]:
        """
        List all agent tokens for a user (admin only).

        Args:
            user_id: User UUID.

        Returns:
            List of token dicts with keys: id, name, prefix, permissions,
            lastUsedAt, isRevoked, createdAt.
        """
        params = {"user_id": user_id}
        result = self._request("GET", "/api/agents/tokens", params=params)
        return result.get("data", [])

    def revoke_agent_token(self, token_id: str, user_id: str) -> bool:
        """
        Revoke an agent token (admin only).

        Args:
            token_id: Token UUID.
            user_id: Owner's user UUID.

        Returns:
            True if revoked successfully.
        """
        params = {"user_id": user_id}
        result = self._request("DELETE", f"/api/agents/tokens/{token_id}", params=params)
        return result.get("data", {}).get("isRevoked", False)

    def upsert_user_by_address(self, kaspa_address: str) -> Dict[str, Any]:
        """
        Find or create a user by Kaspa address, returning their DB ID (admin only).

        Args:
            kaspa_address: Kaspa wallet address.

        Returns:
            Dict with keys: id, kaspa_address.
        """
        body = {"kaspa_address": kaspa_address}
        result = self._request("POST", "/api/agents/users", body=body)
        return result.get("data", {})

    # ==========================================================
    # TradeRecommendation — agent recommends, user disposes
    # ==========================================================
    #
    # For AI assistants that cannot or will not hold a `trade`-permission
    # API token, this method builds a non-executing ``TradeRecommendation``
    # card so the user can review and execute the trade themselves.
    #
    # Default is **recommendation-only** (``execute=False``): the SDK only
    # reads the live order book to estimate fill price/shares, never writes
    # to ``/api/limit-orders``. If the caller explicitly opts in via
    # ``execute=True``, the SDK calls ``place_limit_order`` and records any
    # error on the returned card's ``execution_error`` field.

    def recommend_trade(
        self,
        market_id: str,
        side: str,
        target_price: float,
        amount_kas: float,
        *,
        confidence: Optional[float] = None,
        reasoning: str = "",
        features: Optional[Dict[str, Any]] = None,
        risks: Optional[List[str]] = None,
        ttl_seconds: int = 300,
        execute: bool = False,
        skip_quote: bool = False,
    ) -> "TradeRecommendation":  # raises RuntimeError if the sibling module is absent
        """Build a TradeRecommendation describing how to place a limit order.

        Args:
            market_id: Market slug or uuid.
            side: "yes" or "no".
            target_price: Limit price between 0.01 and 0.99.
            amount_kas: KAS stake.
            confidence: Optional 0.0–1.0 agent confidence score.
            reasoning: Free-text justification (rendered in the markdown card).
            features: Supporting numbers (best_bid, depth, volume, ...) to
                include in the markdown card.
            risks: Free-form risk list to include in the card.
            ttl_seconds: Card validity window (default 5 minutes).
            execute: If True and the SDK has trade permission, place the order
                via ``/api/limit-orders``. **Default False** — recommend only.
            skip_quote: If True, skip the order-book quote and use the
                target price as the fill estimate (useful for offline
                backtests where no network call is desired).

        Returns:
            ``TradeRecommendation``. Raises ``RuntimeError`` if the sibling
            ``trade_recommendation`` module is missing from this install.
        """
        if TradeRecommendation is None:
            raise RuntimeError(
                "trade_recommendation module not found — install it "
                "alongside k100bet-agent.py."
            )

        side_l = side.lower()
        if side_l not in ("yes", "no"):
            raise K100betValidationError(
                f"side must be 'yes' or 'no', got {side!r}"
            )
        target_price = float(target_price)
        if not (0.01 <= target_price <= 0.99):
            raise K100betValidationError(
                f"target_price must be in [0.01, 0.99], got {target_price}"
            )
        amount_kas = float(amount_kas)
        if amount_kas <= 0:
            raise K100betValidationError(
                f"amount_kas must be > 0, got {amount_kas}"
            )

        now = time.time()
        risks_list: List[str] = list(risks or [])

        # Quote for fill estimate (read-only; works with any token).
        est_fill_price = target_price
        expected_shares = amount_kas / target_price if target_price else 0.0
        est_slippage_pct = 0.0
        if not skip_quote:
            try:
                quote = self.get_orderbook_quote(market_id, side_l, amount_kas)
                est_fill_price = float(quote.get("estimatedPrice") or target_price)
                expected_shares = float(quote.get("estimatedShares") or 0.0)
                fillable = float(quote.get("fillableAmount") or 0.0)
                if est_fill_price > 0 and target_price > 0:
                    est_slippage_pct = abs(
                        (est_fill_price - target_price) / max(target_price, 1e-9)
                    ) * 100.0
                if fillable + 1e-9 < amount_kas:
                    risks_list.append(
                        f"only {fillable:.2f} of {amount_kas:.2f} KAS would fill "
                        f"at the requested price — book is thin"
                    )
            except K100betNotFoundError:
                risks_list.append(
                    f"market {market_id!r} not found on the live order book"
                )
            except K100betError as err:
                risks_list.append(f"order book quote failed: {err}")

        # Expected outcome (mirrors the live CLOB 2% fee).
        gross_payout = expected_shares
        net_payout = round(gross_payout * (1.0 - HOUSE_FEE_RATE), 6)
        net_profit = round(net_payout - amount_kas, 6)

        # Best-effort market title — prefer narrow K100betError so genuine
        # bugs surface instead of being silently swallowed.
        market_title = market_id
        try:
            market_obj = self.get_market(market_id)
            if isinstance(market_obj, dict):
                market_title = market_obj.get("title") or market_id
        except K100betError:
            pass

        card = TradeRecommendation(
            id=str(uuid.uuid4()),
            created_at=now,
            expires_at=now + max(1, int(ttl_seconds)),
            market_id=market_id,
            market_title=market_title,
            side=side_l,
            target_price=target_price,
            amount_kas=amount_kas,
            expected_shares=round(expected_shares, 6),
            est_fill_price=round(est_fill_price, 6),
            est_slippage_pct=round(est_slippage_pct, 4),
            expected_payout_kas=net_payout,
            expected_profit_kas=net_profit,
            confidence=confidence,
            reasoning=reasoning or "",
            features=features or {},
            risks=risks_list,
            executed=False,
            executed_at=None,
            execution_error=None,
        )

        if execute:
            try:
                self.place_limit_order(
                    market_id=market_id,
                    side=side_l,
                    amount=amount_kas,
                    target_price=target_price,
                )
                card.executed = True
                card.executed_at = time.time()
            except K100betError as err:
                card.execution_error = str(err)
            except Exception as err:  # noqa: BLE001
                card.execution_error = (
                    f"unexpected error placing order: {err}"
                )

        return card    # ==========================================================
    # Leaderboard
    # ==========================================================

    def get_leaderboard(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Fetch the top traders leaderboard.

        Args:
            limit: Number of top traders to return (default 20).

        Returns:
            List of leaderboard entry dicts with keys: rank, userId,
            kaspaAddress, totalVolume, totalBets, winRate, etc.
        """
        params = {"limit": str(limit)}
        result = self._request("GET", "/api/leaderboard", params=params)
        return result.get("data", [])

    # ==========================================================
    # KAS Price
    # ==========================================================

    def get_kas_price(self) -> Dict[str, Any]:
        """
        Fetch the current KAS price in USD.

        Returns:
            Price dict with keys: price, change24h, source, timestamp.
        """
        result = self._request("GET", "/api/prices/kas")
        return result.get("data", {})

    # ==========================================================
    # Market Quote (server-side)
    # ==========================================================

    def get_market_quote(self, market_id: str, side: str, amount: Union[str, float, int]) -> Dict[str, Any]:
        """
        Get a server-side trade quote for a market.

        Unlike get_bet_quote (which walks the order book client-side),
        this calls the server's /quote endpoint for an authoritative fill
        estimate.

        Args:
            market_id: The market slug.
            side: "yes" or "no".
            amount: Amount in KAS to spend.

        Returns:
            Quote dict with estimated fill price, shares, slippage, etc.
        """
        params = {"side": side, "amount": str(amount)}
        result = self._request("GET", f"/api/markets/{market_id}/quote", params=params)
        return result.get("data", {})

    # ==========================================================
    # Bet Lifecycle — confirm, claim, cashout
    # ==========================================================

    def confirm_bet(self, bet_id: str) -> Dict[str, Any]:
        """
        Confirm a pending bet (e.g. after on-chain deposit is detected).

        Args:
            bet_id: The bet UUID.

        Returns:
            Updated bet dict.
        """
        body = {"betId": bet_id}
        result = self._request("POST", "/api/bets/confirm", body=body)
        return result.get("data", {})

    def claim_bet(self, bet_id: str) -> Dict[str, Any]:
        """
        Claim payout for a winning bet.

        Args:
            bet_id: The bet UUID.

        Returns:
            Claim result dict with keys: betId, payout, status.
        """
        result = self._request("POST", f"/api/bets/{bet_id}/claim", body={})
        return result.get("data", {})

    def cashout_bet(self, bet_id: str) -> Dict[str, Any]:
        """
        Cash out a bet early (if supported by the market).

        Args:
            bet_id: The bet UUID.

        Returns:
            Cashout result dict with keys: betId, cashoutAmount, status.
        """
        body = {"betId": bet_id}
        result = self._request("POST", "/api/bets/cashout", body=body)
        return result.get("data", {})

    # ==========================================================
    # Predict Slot
    # ==========================================================

    def get_slot_round(self) -> Dict[str, Any]:
        """
        Fetch the current active Predict Slot round.

        Returns:
            Round dict with keys: roundId, status, buckets, endTime, etc.
        """
        result = self._request("GET", "/api/predict-slot/round")
        return result.get("data", {})

    def get_slot_jackpot(self) -> Dict[str, Any]:
        """
        Fetch the current Predict Slot jackpot pool.

        Returns:
            Jackpot dict with keys: totalPool, jackpotAmount, lastWinner.
        """
        result = self._request("GET", "/api/predict-slot/jackpot")
        return result.get("data", {})

    def get_my_slot_bets(self) -> List[Dict[str, Any]]:
        """
        Fetch the authenticated user's Predict Slot bet history.

        Returns:
            List of slot bet dicts.
        """
        result = self._request("GET", "/api/predict-slot/my-bets")
        return result.get("data", [])

    def claim_slot_bet(self, bet_id: str) -> Dict[str, Any]:
        """
        Claim payout for a winning Predict Slot bet.

        Args:
            bet_id: The slot bet UUID.

        Returns:
            Claim result dict.
        """
        result = self._request("POST", f"/api/predict-slot/bets/{bet_id}/claim", body={})
        return result.get("data", {})

    # ==========================================================
    # Watchlist
    # ==========================================================

    def get_watchlist(self) -> List[Dict[str, Any]]:
        """
        Fetch the authenticated user's market watchlist.

        Returns:
            List of market dicts the user has bookmarked.
        """
        result = self._request("GET", "/api/watchlist")
        return result.get("data", [])

    def toggle_watchlist(self, market_id: str) -> Dict[str, Any]:
        """
        Add or remove a market from the watchlist (toggle).

        Args:
            market_id: The market slug.

        Returns:
            Result dict with keys: marketId, added (bool).
        """
        body = {"marketId": market_id}
        result = self._request("POST", "/api/watchlist", body=body)
        return result.get("data", {})

    # ==========================================================
    # Notifications
    # ==========================================================

    def get_notifications(self) -> List[Dict[str, Any]]:
        """
        Fetch the authenticated user's notifications.

        Returns:
            List of notification dicts.
        """
        result = self._request("GET", "/api/notifications")
        return result.get("data", [])

    def subscribe_notifications(self, endpoint: str, p256dh: str, auth: str) -> Dict[str, Any]:
        """
        Subscribe to push notifications via web push.

        Args:
            endpoint: Push subscription endpoint URL.
            p256dh: Push subscription P256DH key.
            auth: Push subscription auth key.

        Returns:
            Result dict with keys: subscribed, subscriptionId.
        """
        body = {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}
        result = self._request("POST", "/api/notifications/subscribe", body=body)
        return result.get("data", {})

    def unsubscribe_notifications(self, endpoint: str) -> Dict[str, Any]:
        """
        Unsubscribe from push notifications.

        Args:
            endpoint: The push subscription endpoint to remove.

        Returns:
            Result dict with keys: unsubscribed.
        """
        body = {"endpoint": endpoint}
        result = self._request("POST", "/api/notifications/subscribe", body=body)
        return result.get("data", {})

    # ==========================================================
    # Market Comments
    # ==========================================================

    def get_market_comments(self, market_id: str) -> List[Dict[str, Any]]:
        """
        Fetch comments for a market.

        Args:
            market_id: The market slug.

        Returns:
            List of comment dicts.
        """
        result = self._request("GET", f"/api/markets/{market_id}/comments")
        return result.get("data", [])

    def post_market_comment(self, market_id: str, text: str) -> Dict[str, Any]:
        """
        Post a comment on a market.

        Args:
            market_id: The market slug.
            text: Comment text.

        Returns:
            Created comment dict.
        """
        body = {"text": text}
        result = self._request("POST", f"/api/markets/{market_id}/comments", body=body)
        return result.get("data", {})

    def like_market_comment(self, market_id: str, comment_id: str) -> Dict[str, Any]:
        """
        Like (toggle) a market comment.

        Args:
            market_id: The market slug.
            comment_id: The comment UUID.

        Returns:
            Result dict with keys: liked, likesCount.
        """
        result = self._request("POST", f"/api/markets/{market_id}/comments/{comment_id}/like", body={})
        return result.get("data", {})

    # ==========================================================
    # Proposal Voting
    # ==========================================================

    def vote_proposal(self, proposal_id: str, vote: str) -> Dict[str, Any]:
        """
        Vote on a market proposal.

        Args:
            proposal_id: The proposal UUID.
            vote: "up" or "down".

        Returns:
            Vote result dict with keys: proposalId, vote, totalVotes.
        """
        body = {"proposalId": proposal_id, "vote": vote}
        result = self._request("POST", "/api/market-proposals/vote", body=body)
        return result.get("data", {})

    # ==========================================================
    # Market Search
    # ==========================================================

    def search_markets(self, query: str) -> List[MarketDict]:
        """
        Search markets by keyword.

        Args:
            query: Search query string.

        Returns:
            List of matching market dicts.
        """
        params = {"q": query}
        result = self._request("GET", "/api/markets/search", params=params)
        return result.get("data", [])

    # ==========================================================
    # Kaspa Transaction Lookup
    # ==========================================================

    def get_kaspa_tx(self, tx_id: str) -> Dict[str, Any]:
        """
        Look up a Kaspa transaction by its ID.

        Args:
            tx_id: The Kaspa transaction ID.

        Returns:
            Transaction dict with confirmation status and details.
        """
        result = self._request("GET", f"/api/kaspa/tx/{tx_id}")
        return result.get("data", {})

    # ==========================================================
    # CLI Entrypoint
    # ============================================================

def main():
    """Simple CLI for testing the SDK."""
    import argparse

    data = None

    parser = argparse.ArgumentParser(description="K100bet Agent SDK CLI")
    parser.add_argument("--api-key", "-k", help="API key (or K100BET_API_KEY env var)",
                        default=os.environ.get("K100BET_API_KEY"))
    parser.add_argument("--base-url", "-u", help="Base URL",
                        default=os.environ.get("K100BET_BASE_URL", "https://k100bet.com"))
    parser.add_argument("command", nargs="?", default="markets",
                        choices=["markets", "market", "stats", "user", "bets", "pool",
                                 "analyze", "quote", "proposals", "generate-token",
                                 "orderbook", "orders", "order", "cancel-order",
                                 "trades", "market-order", "bet-intent", "slot-intent",
                                 "leaderboard","kas-price","",                                 "","un","",                                 "confirm-bet", "claim-bet", "cashout-bet",
                                 "slot-round", "slot-jackpot", "my-slot-bets",
                                 "watchlist", "toggle-watchlist", "notifications",
                                 "market-quote", "search", "kaspa-tx"])
    parser.add_argument("--market", "-m", help="Market ID")
    parser.add_argument("--user", help="User ID")
    parser.add_argument("--kaspa", help="Kaspa address")
    parser.add_argument("--side", choices=["yes", "no"], default="yes")
    parser.add_argument("--amount", type=float, default=100.0)
    parser.add_argument("--price", type=float, help="Limit price (0.01-0.99)")
    parser.add_argument("--order-id", help="Order ID for cancel-order / bet ID / tx ID")
    parser.add_argument("--bucket", help="Predict Slot bucket id (slot-intent)")
    parser.add_argument("--limit", type=int, default=20, help="Limit for leaderboard (default 20)")
    parser.add_argument("--query", help="Search query for search command")
    parser.add_argument("--tx-id", help="Kaspa transaction ID for kaspa-tx")
    parser.add_argument("--json", "-j", action="store_true", help="Pretty-print JSON")

    args = parser.parse_args()

    if args.command == "generate-token":
        token = K100bet.generate_token()
        print(f"Raw Token: {token['rawToken']}")
        print(f"SHA-256:   {token['tokenHash']}")
        print("\n⚠️  Store the raw token securely! It will not be shown again.")
        return

    if not args.api_key:
        parser.error("API key required. Set K100BET_API_KEY env var or pass --api-key.")

    k = K100bet(api_key=args.api_key, base_url=args.base_url)

    if args.command == "markets":
        data = k.get_markets()
        print(f"\n{'ID':<30} {'Title':<45} {'Yes':<8} {'No':<8} {'Status':<10}")
        print("-" * 105)
        for m in data[:20]:
            print(f"{m['id']:<30} {m['title'][:42]:<45} "
                  f"{m['yesPrice']*100:>5.0f}%   {m['noPrice']*100:>5.0f}%   "
                  f"{m['status']:<10}")

    elif args.command == "stats":
        stats = k.get_stats()
        print(json.dumps(stats, indent=2))

    elif args.command == "user":
        if args.kaspa:
            user = k.get_user(kaspa_address=args.kaspa)
        else:
            user = k.get_user()
        print(json.dumps(user, indent=2, default=str))

    elif args.command == "bets":
        if args.user:
            bets = k.get_bets(user_id=args.user)
        elif args.kaspa:
            bets = k.get_bets(kaspa_address=args.kaspa)
        else:
            bets = []
        print(f"\n{'ID':<36} {'Market':<30} {'Side':<6} {'Amount':<12} {'Status':<10}")
        print("-" * 100)
        for b in bets[:15]:
            print(f"{b['id'][:34]:<36} {b.get('marketId', '')[:28]:<30} "
                  f"{b['side']:<6} {b['amount']:<12} {b['status']:<10}")

    elif args.command == "pool":
        pools = k.get_liquidity_pools()
        print(f"\n{'Market':<30} {'Total Yes':<12} {'Total No':<12} {'Total Pool':<12}")
        print("-" * 70)
        for p in pools[:10]:
            print(f"{p['marketId'][:28]:<30} {p['totalYes'][:10]:<12} "
                  f"{p['totalNo'][:10]:<12} {p['totalPool'][:10]:<12}")

    elif args.command == "analyze":
        if not args.market:
            parser.error("--market required for analyze command")
        analysis = k.analyze_market(args.market)
        print(json.dumps(analysis, indent=2, default=str))

    elif args.command == "quote":
        if not args.market:
            parser.error("--market required for quote command")
        quote = k.get_bet_quote(args.market, args.side, args.amount)
        if quote:
            print(json.dumps(quote, indent=2))
        else:
            print("Could not generate quote (market may not exist)")

    elif args.command == "proposals":
        proposals = k.get_proposals()
        print(json.dumps(proposals, indent=2, default=str))

    # ── CLOB Commands ─────────────────────────────────────────

    elif args.command == "orderbook":
        if not args.market:
            parser.error("--market required for orderbook command")
        book = k.get_orderbook(args.market)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        print(f"\n{'ORDER BOOK':^60}")
        print(f"{'BIDS (Buy YES)':^30} | {'ASKS (Buy NO)':^30}")
        print(f"{'Price':<10} {'Amount':<10} {'Total':<10} | {'Price':<10} {'Amount':<10} {'Total':<10}")
        print("-" * 60)
        max_rows = max(len(bids), len(asks), 1)
        for i in range(min(max_rows, 15)):
            bid = bids[i] if i < len(bids) else {}
            ask = asks[i] if i < len(asks) else {}
            bid_str = f"{float(bid['price']):<10.4f} {float(bid['amount']):<10.2f} {float(bid.get('total', 0)):<10.2f}" if bid else " " * 30
            ask_str = f"{float(ask['price']):<10.4f} {float(ask['amount']):<10.2f} {float(ask.get('total', 0)):<10.2f}" if ask else ""
            print(f"{bid_str} | {ask_str}")
        if not bids and not asks:
            print("  (empty order book)")

    elif args.command == "orders":
        orders = k.get_open_orders(market_id=args.market)
        if not orders:
            print("No open orders.")
        else:
            print(f"\n{'ID':<36} {'Market':<25} {'Side':<5} {'Amount':<10} {'Price':<8} {'Status':<15}")
            print("-" * 105)
            for o in orders:
                print(f"{o['id'][:34]:<36} {o.get('marketId', '')[:23]:<25} "
                      f"{o.get('side', ''):<5} {o.get('amount', ''):<10} "
                      f"{float(o.get('targetPrice', 0)):<8.4f} {o.get('status', ''):<15}")

    elif args.command == "order":
        if not args.market or not args.price:
            parser.error("--market and --price required for order command")
        result = k.place_limit_order(args.market, args.side, args.amount, args.price)
        print(f"Order placed: {result.get('id', 'unknown')}")
        print(f"  Status:       {result.get('status', 'unknown')}")
        print(f"  Filled:       {result.get('filledAmount', '0')}")
        print(f"  Matched:      {result.get('matchedCount', 0)} orders")
        if args.json:
            print(json.dumps(result, indent=2, default=str))

    elif args.command == "market-order":
        if not args.market:
            parser.error("--market required for market-order command")
        result = k.place_market_order(args.market, args.side, args.amount)
        print(f"Market order placed: {result.get('id', 'unknown')}")
        print(f"  Status:       {result.get('status', 'unknown')}")
        print(f"  Filled:       {result.get('filledAmount', '0')}")
        if args.json:
            print(json.dumps(result, indent=2, default=str))

    elif args.command == "cancel-order":
        if not args.order_id:
            parser.error("--order-id required for cancel-order command")
        result = k.cancel_order(args.order_id)
        print(f"Order {args.order_id} cancelled.")

    elif args.command == "trades":
        trades = k.get_trades(market_id=args.market)
        if not trades:
            print("No trades found.")
        else:
            print(f"\n{'ID':<36} {'Market':<25} {'Side':<5} {'Price':<8} {'Amount':<10} {'Time':<20}")
            print("-" * 105)
            for t in trades:
                print(f"{t['id'][:34]:<36} {t.get('marketId', '')[:23]:<25} "
                      f"{t.get('side', ''):<5} {float(t.get('price', 0)):<8.4f} "
                      f"{float(t.get('amount', 0)):<10.2f} {t.get('createdAt', '')[:19]:<20}")

    elif args.command == "bet-intent":
        if not args.market:
            parser.error("--market required for bet-intent command")
        intent = k.create_bet_intent(args.market, args.side, wallet_address=args.kaspa)
        print(json.dumps(intent, indent=2))

    elif args.command == "slot-intent":
        if not args.market:
            parser.error("--market required as round id for slot-intent (e.g. --market 42)")
        bucket = args.bucket or args.order_id
        if not bucket:
            parser.error("--bucket required for slot-intent")
        intent = k.create_slot_bet_intent(int(args.market), bucket, args.side, wallet_address=args.kaspa)
        print(json.dumps(intent, indent=2))

    elif args.command == "market":
        if not args.market:
            parser.error("--market required for market command")
        m = k.get_market(args.market)
        if m:
            print(json.dumps(m, indent=2, default=str))
        else:
            print("Market not found.")

    elif args.command == "leaderboard":
        lb = k.get_leaderboard(limit=args.limit)
        if not lb:
            print("No leaderboard data.")
        else:
            print(f"\n{'Rank':<6} {'Address':<36} {'Volume':<14} {'Bets':<8} {'Win%':<8}")
            print("-" * 72)
            for entry in lb[:20]:
                print(f"{entry.get('rank', ''):<6} {(entry.get('kaspaAddress', '') or '')[:34]:<36} "
                      f"{entry.get('totalVolume', ''):<14} {entry.get('totalBets', ''):<8} "
                      f"{entry.get('winRate', 0)*100:>5.0f}%")

    elif args.command == "kas-price":
        price = k.get_kas_price()
        print(json.dumps(price, indent=2, default=str))

    elif args.command == "confirm-bet":
        if not args.order_id:
            parser.error("--order-id required (bet ID) for confirm-bet")
        result = k.confirm_bet(args.order_id)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "claim-bet":
        if not args.order_id:
            parser.error("--order-id required (bet ID) for claim-bet")
        result = k.claim_bet(args.order_id)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "cashout-bet":
        if not args.order_id:
            parser.error("--order-id required (bet ID) for cashout-bet")
        result = k.cashout_bet(args.order_id)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "slot-round":
        round_data = k.get_slot_round()
        print(json.dumps(round_data, indent=2, default=str))

    elif args.command == "slot-jackpot":
        jackpot = k.get_slot_jackpot()
        print(json.dumps(jackpot, indent=2, default=str))

    elif args.command == "my-slot-bets":
        bets = k.get_my_slot_bets()
        if not bets:
            print("No slot bets found.")
        else:
            print(json.dumps(bets, indent=2, default=str))

    elif args.command == "watchlist":
        wl = k.get_watchlist()
        if not wl:
            print("Watchlist is empty.")
        else:
            for m in wl[:20]:
                print(f"{m['id']:<30} {m.get('title', '')[:40]:<40} Yes {m.get('yesPrice', 0)*100:.0f}%")

    elif args.command == "toggle-watchlist":
        if not args.market:
            parser.error("--market required for toggle-watchlist")
        result = k.toggle_watchlist(args.market)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "notifications":
        notes = k.get_notifications()
        if not notes:
            print("No notifications.")
        else:
            for n in notes[:20]:
                print(f"{n.get('type', ''):<20} {n.get('message', '')[:60]}")

    elif args.command == "market-quote":
        if not args.market:
            parser.error("--market required for market-quote")
        quote = k.get_market_quote(args.market, args.side, args.amount)
        print(json.dumps(quote, indent=2, default=str))

    elif args.command == "search":
        q = args.query or args.market
        if not q:
            parser.error("--query required for search command")
        results = k.search_markets(q)
        if not results:
            print("No results.")
        else:
            for m in results[:20]:
                print(f"{m['id']:<30} {m.get('title', '')[:40]:<40} Yes {m.get('yesPrice', 0)*100:.0f}%")

    elif args.command == "kaspa-tx":
        tx_id = args.tx_id or args.order_id
        if not tx_id:
            parser.error("--tx-id required for kaspa-tx")
        tx = k.get_kaspa_tx(tx_id)
        print(json.dumps(tx, indent=2, default=str))

    if args.json and data is not None:
        print(json.dumps(data, indent=2, default=str))


if __name__ == "__main__":
    main()
