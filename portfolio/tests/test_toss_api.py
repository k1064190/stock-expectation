"""Tests for the official Toss Open API client and the sync source dispatcher.

httpx calls are mocked with monkeypatch — no network access. Response shapes
mirror the canonical Toss OpenAPI spec consumed by ``portfolio.toss_api``:
the OAuth2 token endpoint, ``GET /api/v1/accounts`` (``result`` is a list of
Account), and ``GET /api/v1/holdings`` (``result.items`` is a list of
HoldingsItem).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from portfolio import toss_api, toss_sync
from portfolio.toss_sync import reconcile


@pytest.fixture(autouse=True)
def _reset_token_cache():
    """Clear the module token cache before each test for isolation."""
    toss_api._token_cache = None
    yield
    toss_api._token_cache = None


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _holding(symbol, market_country, currency, quantity, avg, last, name="Test"):
    """Build a HoldingsItem dict in the Toss Open API shape (prices are strings)."""
    return {
        "symbol": symbol,
        "name": name,
        "marketCountry": market_country,
        "currency": currency,
        "quantity": str(quantity),
        "averagePurchasePrice": str(avg),
        "lastPrice": str(last),
    }


class TestConfigured:
    def test_true_when_both_set(self, monkeypatch):
        monkeypatch.setenv("TOSS_CLIENT_ID", "id")
        monkeypatch.setenv("TOSS_CLIENT_SECRET", "secret")
        assert toss_api.toss_api_configured() is True

    def test_false_when_missing(self, monkeypatch):
        monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
        monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
        assert toss_api.toss_api_configured() is False

    def test_false_when_only_id(self, monkeypatch):
        monkeypatch.setenv("TOSS_CLIENT_ID", "id")
        monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
        assert toss_api.toss_api_configured() is False


class TestAccessToken:
    def _set_creds(self, monkeypatch):
        monkeypatch.setenv("TOSS_CLIENT_ID", "id")
        monkeypatch.setenv("TOSS_CLIENT_SECRET", "secret")

    def test_issues_and_caches(self, monkeypatch):
        self._set_creds(monkeypatch)
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs.get("data")))
            return _FakeResponse({"access_token": "tok-1", "expires_in": 3600})

        monkeypatch.setattr(toss_api.httpx, "post", fake_post)

        assert toss_api.get_access_token() == "tok-1"
        # Second call within validity reuses the cached token (no new POST).
        assert toss_api.get_access_token() == "tok-1"
        assert len(calls) == 1
        # Credentials are sent as body params per the Toss spec.
        url, data = calls[0]
        assert url.endswith("/oauth2/token")
        assert data["grant_type"] == "client_credentials"
        assert data["client_id"] == "id"
        assert data["client_secret"] == "secret"

    def test_refreshes_when_expired(self, monkeypatch):
        self._set_creds(monkeypatch)
        tokens = iter(["tok-1", "tok-2"])

        def fake_post(url, **kwargs):
            return _FakeResponse({"access_token": next(tokens), "expires_in": 3600})

        monkeypatch.setattr(toss_api.httpx, "post", fake_post)
        clock = [1000.0]
        monkeypatch.setattr(toss_api.time, "time", lambda: clock[0])

        assert toss_api.get_access_token() == "tok-1"
        clock[0] = 1000.0 + 3600  # past validity minus margin
        assert toss_api.get_access_token() == "tok-2"

    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
        monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
        with pytest.raises(EnvironmentError):
            toss_api.get_access_token()

    def test_missing_token_field_raises(self, monkeypatch):
        self._set_creds(monkeypatch)
        monkeypatch.setattr(
            toss_api.httpx,
            "post",
            lambda url, **kw: _FakeResponse({"expires_in": 3600}),
        )
        with pytest.raises(RuntimeError):
            toss_api.get_access_token()


class TestFetchPositionsApi:
    def _wire(self, monkeypatch, accounts, holdings_by_seq):
        monkeypatch.setenv("TOSS_CLIENT_ID", "id")
        monkeypatch.setenv("TOSS_CLIENT_SECRET", "secret")
        monkeypatch.setattr(toss_api, "get_access_token", lambda: "tok")

        def fake_get(url, **kwargs):
            if url.endswith("/api/v1/accounts"):
                return _FakeResponse({"result": accounts})
            if url.endswith("/api/v1/holdings"):
                seq = kwargs["headers"]["X-Tossinvest-Account"]
                return _FakeResponse({"result": {"items": holdings_by_seq[seq]}})
            raise AssertionError(f"unexpected url {url}")

        monkeypatch.setattr(toss_api.httpx, "get", fake_get)

    def test_normalizes_kr_and_us(self, monkeypatch):
        accounts = [{"accountNo": "1", "accountSeq": 1, "accountType": "BROKERAGE"}]
        holdings = {
            "1": [
                _holding("005930", "KR", "KRW", 10, 55000, 60000, "삼성전자"),
                _holding("NVDA", "US", "USD", 5, 120.0, 130.0, "NVIDIA"),
            ]
        }
        self._wire(monkeypatch, accounts, holdings)

        positions = toss_api.fetch_toss_positions_api()
        assert len(positions) == 2

        kr = next(p for p in positions if p["symbol"] == "005930")
        assert kr["market_type"] == "KR_STOCK"
        assert kr["quantity"] == 10
        assert kr["average_price"] == 55000
        assert "average_price_usd" not in kr

        us = next(p for p in positions if p["symbol"] == "NVDA")
        assert us["market_type"] == "US_STOCK"
        assert us["average_price"] == 120.0
        assert us["average_price_usd"] == 120.0
        assert us["current_price_usd"] == 130.0

    def test_skips_unmappable_instrument(self, monkeypatch):
        """A holding with no marketCountry and no currency is dropped."""
        accounts = [{"accountNo": "1", "accountSeq": 1, "accountType": "BROKERAGE"}]
        holdings = {
            "1": [
                {
                    "symbol": "FUND-XYZ",
                    "name": "Some Fund",
                    "quantity": "1",
                    "averagePurchasePrice": "100",
                    "lastPrice": "100",
                }
            ]
        }
        self._wire(monkeypatch, accounts, holdings)
        assert toss_api.fetch_toss_positions_api() == []

    def test_aggregates_multiple_accounts(self, monkeypatch):
        accounts = [
            {"accountNo": "1", "accountSeq": 1, "accountType": "BROKERAGE"},
            {"accountNo": "2", "accountSeq": 2, "accountType": "BROKERAGE"},
        ]
        holdings = {
            "1": [_holding("005930", "KR", "KRW", 10, 55000, 60000, "삼성전자")],
            "2": [_holding("AAPL", "US", "USD", 3, 200.0, 210.0, "Apple")],
        }
        self._wire(monkeypatch, accounts, holdings)
        positions = toss_api.fetch_toss_positions_api()
        assert {p["symbol"] for p in positions} == {"005930", "AAPL"}

    def test_empty_holdings(self, monkeypatch):
        """An account with no holdings yields no positions."""
        accounts = [{"accountNo": "1", "accountSeq": 1, "accountType": "BROKERAGE"}]
        self._wire(monkeypatch, accounts, {"1": []})
        assert toss_api.fetch_toss_positions_api() == []

    def test_output_feeds_reconcile(self, monkeypatch):
        """Adapter output is consumable by the existing reconcile() unchanged."""
        accounts = [{"accountNo": "1", "accountSeq": 1, "accountType": "BROKERAGE"}]
        holdings = {
            "1": [_holding("005930", "KR", "KRW", 10, 55000, 60000, "삼성전자")]
        }
        self._wire(monkeypatch, accounts, holdings)
        positions = toss_api.fetch_toss_positions_api()
        actions = reconcile(positions, [], "KR")
        assert len(actions) == 1
        assert actions[0]["ticker"] == "005930"
        assert actions[0]["side"] == "BUY"
        assert actions[0]["quantity"] == 10


class TestFetchPositionsDispatcher:
    def test_auto_prefers_api_when_configured(self, monkeypatch):
        monkeypatch.setattr(toss_api, "toss_api_configured", lambda: True)
        monkeypatch.setattr(
            toss_api, "fetch_toss_positions_api", lambda: [{"symbol": "X"}]
        )
        positions, source = toss_sync.fetch_positions("auto")
        assert source == "toss-api"
        assert positions == [{"symbol": "X"}]

    def test_auto_falls_back_to_tossctl(self, monkeypatch):
        monkeypatch.setattr(toss_api, "toss_api_configured", lambda: False)
        monkeypatch.setattr(toss_sync, "tossctl_available", lambda: True)
        monkeypatch.setattr(
            toss_sync, "fetch_toss_positions", lambda: [{"symbol": "Y"}]
        )
        positions, source = toss_sync.fetch_positions("auto")
        assert source == "tossctl"
        assert positions == [{"symbol": "Y"}]

    def test_auto_raises_when_neither(self, monkeypatch):
        monkeypatch.setattr(toss_api, "toss_api_configured", lambda: False)
        monkeypatch.setattr(toss_sync, "tossctl_available", lambda: False)
        with pytest.raises(RuntimeError):
            toss_sync.fetch_positions("auto")

    def test_force_tossctl(self, monkeypatch):
        monkeypatch.setattr(
            toss_sync, "fetch_toss_positions", lambda: [{"symbol": "Z"}]
        )
        positions, source = toss_sync.fetch_positions("tossctl")
        assert source == "tossctl"
        assert positions == [{"symbol": "Z"}]

    def test_force_toss_api(self, monkeypatch):
        monkeypatch.setattr(
            toss_api, "fetch_toss_positions_api", lambda: [{"symbol": "W"}]
        )
        positions, source = toss_sync.fetch_positions("toss-api")
        assert source == "toss-api"
        assert positions == [{"symbol": "W"}]
