"""Fetch portfolio holdings from the official Toss Securities Open API.

OAuth2 client-credentials flow against ``https://openapi.tossinvest.com``
(canonical spec: ``/openapi-docs/latest/openapi.json``): issue an access
token, list accounts, then read holdings per account. Each holding is
normalized into the same dict shape that ``portfolio.toss_sync.reconcile``
already consumes (the shape previously produced by the ``tossctl`` CLI), so
the reconciliation logic is untouched.

Credentials come from the environment (loaded from ``.env`` by stock_cli.py):
    TOSS_CLIENT_ID, TOSS_CLIENT_SECRET
    TOSS_OPENAPI_BASE_URL  (optional, defaults to the public base URL)

If the credentials are absent, callers fall back to the tossctl path.
"""

import os
import time
from typing import Optional

import httpx

DEFAULT_BASE_URL = "https://openapi.tossinvest.com"
_TIMEOUT = 30
# Refresh the token slightly before it actually expires to avoid races.
_TOKEN_REFRESH_MARGIN_SEC = 60

# Module-level token cache: (access_token, expires_at_epoch_seconds).
# Single-process synchronous CLI usage, so a plain module cache is enough.
_token_cache: Optional[tuple[str, float]] = None


def toss_api_configured() -> bool:
    """Return True if both Toss Open API credentials are set in the env."""
    return bool(os.environ.get("TOSS_CLIENT_ID")) and bool(
        os.environ.get("TOSS_CLIENT_SECRET")
    )


def _base_url() -> str:
    """Return the Open API base URL (override via TOSS_OPENAPI_BASE_URL)."""
    return os.environ.get("TOSS_OPENAPI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _credentials() -> tuple[str, str]:
    """Read Toss Open API credentials from the environment.

    Returns:
        Tuple of (client_id, client_secret).

    Raises:
        EnvironmentError: If either credential is missing.
    """
    client_id = os.environ.get("TOSS_CLIENT_ID", "")
    client_secret = os.environ.get("TOSS_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise EnvironmentError(
            "Set TOSS_CLIENT_ID and TOSS_CLIENT_SECRET environment variables"
        )
    return client_id, client_secret


def get_access_token(force_refresh: bool = False) -> str:
    """Return a valid OAuth2 access token, issuing/caching as needed.

    Uses the client-credentials grant. Per the Toss spec, ``grant_type``,
    ``client_id`` and ``client_secret`` are sent as form-encoded body
    parameters. The token is cached in module memory and reused until shortly
    before its expiry.

    Args:
        force_refresh: Bypass the cache and request a fresh token.

    Returns:
        Bearer access token string.

    Raises:
        EnvironmentError: If credentials are missing.
        httpx.HTTPStatusError: If the token endpoint returns an error.
    """
    global _token_cache

    if not force_refresh and _token_cache is not None:
        token, expires_at = _token_cache
        if time.time() < expires_at - _TOKEN_REFRESH_MARGIN_SEC:
            return token

    client_id, client_secret = _credentials()

    resp = httpx.post(
        f"{_base_url()}/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()

    token = payload.get("access_token")
    if not token:
        raise RuntimeError(
            "Toss OAuth response did not include an access_token "
            "(check TOSS_CLIENT_ID/TOSS_CLIENT_SECRET)"
        )
    expires_in = float(payload.get("expires_in", 3600))
    _token_cache = (token, time.time() + expires_in)
    return token


def _auth_headers(token: str, account_seq: Optional[int] = None) -> dict:
    """Build request headers with the bearer token and optional account key."""
    headers = {"Authorization": f"Bearer {token}"}
    if account_seq is not None:
        headers["X-Tossinvest-Account"] = str(account_seq)
    return headers


def list_accounts(token: str) -> list[dict]:
    """List the authenticated user's accounts (``GET /api/v1/accounts``).

    Args:
        token: Bearer access token.

    Returns:
        List of Account dicts, each with accountNo, accountSeq, accountType.
    """
    resp = httpx.get(
        f"{_base_url()}/api/v1/accounts",
        headers=_auth_headers(token),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def fetch_holdings(token: str, account_seq: int) -> list[dict]:
    """Fetch raw stock holdings for one account (``GET /api/v1/holdings``).

    Args:
        token: Bearer access token.
        account_seq: Account identifier for the ``X-Tossinvest-Account``
            header (the ``accountSeq`` int from list_accounts).

    Returns:
        List of raw HoldingsItem dicts (``result.items``).
    """
    resp = httpx.get(
        f"{_base_url()}/api/v1/holdings",
        headers=_auth_headers(token, account_seq),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json().get("result") or {}
    return result.get("items", [])


def _market_type(holding: dict) -> Optional[str]:
    """Derive the reconcile market_type ('KR_STOCK'/'US_STOCK') for a holding.

    Uses the explicit ``marketCountry`` field ('KR'/'US'), falling back to
    ``currency`` (KRW/USD). Returns None for anything unmapped.
    """
    market = (holding.get("marketCountry") or "").upper()
    if market == "KR":
        return "KR_STOCK"
    if market == "US":
        return "US_STOCK"

    currency = (holding.get("currency") or "").upper()
    if currency == "KRW":
        return "KR_STOCK"
    if currency == "USD":
        return "US_STOCK"
    return None


def _to_normalized_position(holding: dict) -> Optional[dict]:
    """Convert one Open API HoldingsItem into the reconcile() dict shape.

    Target shape (matches the previous tossctl output consumed by reconcile):
        symbol, name, market_type ('KR_STOCK'|'US_STOCK'), quantity,
        average_price, current_price, and for US: average_price_usd,
        current_price_usd. Toss prices are quoted in the holding's trade
        currency, so US prices are already USD.

    Args:
        holding: One raw HoldingsItem dict from the Open API.

    Returns:
        Normalized position dict, or None if the holding cannot be mapped to
        a supported market.
    """
    market_type = _market_type(holding)
    if market_type is None:
        return None

    symbol = str(holding.get("symbol") or "")
    quantity = float(holding.get("quantity", 0) or 0)
    avg_price = float(holding.get("averagePurchasePrice", 0) or 0)
    current_price = float(holding.get("lastPrice", 0) or 0)

    pos = {
        "symbol": symbol,
        "name": holding.get("name", symbol),
        "market_type": market_type,
        "quantity": quantity,
        "average_price": avg_price,
        "current_price": current_price,
    }
    if market_type == "US_STOCK":
        # US holdings are priced in USD; reconcile reads the *_usd fields.
        pos["average_price_usd"] = avg_price
        pos["current_price_usd"] = current_price
    return pos


def fetch_toss_positions_api() -> list[dict]:
    """Fetch and normalize all holdings via the official Toss Open API.

    Issues a token, enumerates accounts, reads each account's holdings, and
    normalizes them into the reconcile() dict shape.

    Returns:
        List of normalized position dicts (see _to_normalized_position).

    Raises:
        EnvironmentError: If credentials are missing.
        httpx.HTTPStatusError: On any API error response.
    """
    token = get_access_token()
    positions: list[dict] = []
    for account in list_accounts(token):
        account_seq = account.get("accountSeq")
        if account_seq is None:
            continue
        for holding in fetch_holdings(token, account_seq):
            normalized = _to_normalized_position(holding)
            if normalized is not None:
                positions.append(normalized)
    return positions
