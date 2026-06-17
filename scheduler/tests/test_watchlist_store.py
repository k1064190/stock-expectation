"""Tests for watchlist storage and unified watchlist assembly."""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-prediction-store"))

from watchlist_store import (
    add_watch,
    remove_watch,
    list_watches,
    load_unified_watchlist,
    get_connection,
    POSITION_DEFAULT_STOP_PCT,
)


@pytest.fixture
def watch_db(tmp_path):
    """A temp watchlist DB connection."""
    conn = get_connection(tmp_path / "watchlist.db")
    yield conn
    conn.close()


# --- CRUD round-trip ---


def test_add_and_list(watch_db):
    """A saved watch round-trips through add → list."""
    wid = add_watch(
        watch_db,
        ticker="nvda",
        market="us",
        entry_low=100.0,
        entry_high=105.0,
        stop=95.0,
        target=130.0,
        reentry=110.0,
        note="breakout",
    )
    assert isinstance(wid, int)

    watches = list_watches(watch_db)
    assert len(watches) == 1
    w = watches[0]
    assert w.ticker == "NVDA"  # US ticker uppercased
    assert w.market == "US"
    assert w.direction == "BULL"  # default
    assert w.entry_low == 100.0
    assert w.entry_high == 105.0
    assert w.stop == 95.0
    assert w.target == 130.0
    assert w.reentry == 110.0
    assert w.source == "saved"
    assert w.label == f"saved:{wid}"


def test_kr_ticker_not_uppercased(watch_db):
    """Numeric KR codes are stored verbatim."""
    add_watch(watch_db, ticker="005930", market="KR", entry_low=70000, entry_high=72000)
    w = list_watches(watch_db)[0]
    assert w.ticker == "005930"


def test_remove(watch_db):
    """remove_watch deletes the row and reports success/failure."""
    wid = add_watch(watch_db, ticker="AAPL", market="US")
    assert remove_watch(watch_db, wid) is True
    assert list_watches(watch_db) == []
    # Removing again returns False.
    assert remove_watch(watch_db, wid) is False


def test_list_market_filter(watch_db):
    """list_watches filters by market."""
    add_watch(watch_db, ticker="NVDA", market="US")
    add_watch(watch_db, ticker="005930", market="KR")
    us = list_watches(watch_db, market="US")
    kr = list_watches(watch_db, market="kr")
    assert {w.ticker for w in us} == {"NVDA"}
    assert {w.ticker for w in kr} == {"005930"}


def test_bear_direction(watch_db):
    """BEAR direction is stored."""
    add_watch(watch_db, ticker="TSLA", market="US", direction="bear", stop=250.0)
    w = list_watches(watch_db)[0]
    assert w.direction == "BEAR"


# --- load_unified_watchlist merge + precedence ---


def _seed_predictions_db(path: Path) -> None:
    """Create a minimal predictions.db with OPEN rows for merge tests."""
    from models import get_connection as pred_conn, insert_prediction, Prediction

    conn = pred_conn(path)
    try:
        insert_prediction(
            conn,
            Prediction(
                ticker="NVDA",
                market="US",
                direction="BULL",
                confidence=0.7,
                timeframe="1W",
                reasoning="pred thesis",
                entry_price=120.0,
                target_price=140.0,
                stop_price=110.0,
            ),
        )
        insert_prediction(
            conn,
            Prediction(
                ticker="AAPL",
                market="US",
                direction="BULL",
                confidence=0.6,
                timeframe="1M",
                reasoning="aapl thesis",
                entry_price=200.0,
                target_price=220.0,
                stop_price=185.0,
            ),
        )
    finally:
        conn.close()


def _seed_portfolio_db(path: Path) -> None:
    """Create a portfolio.db with one US position for merge tests."""
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from portfolio.db import (
        get_connection as pf_conn,
        create_portfolio,
        add_transaction,
    )

    conn = pf_conn(path)
    try:
        pf = create_portfolio(conn, market="US", name="Test US")
        add_transaction(
            conn,
            portfolio_id=pf.id,
            ticker="NVDA",
            side="BUY",
            quantity=10,
            price=130.0,
            currency="USD",
            transacted_at="2026-01-01",
        )
        add_transaction(
            conn,
            portfolio_id=pf.id,
            ticker="MSFT",
            side="BUY",
            quantity=5,
            price=400.0,
            currency="USD",
            transacted_at="2026-01-01",
        )
    finally:
        conn.close()


def test_merge_all_three_sources(tmp_path):
    """All three sources contribute distinct (ticker, market) entries."""
    wl_path = tmp_path / "watchlist.db"
    pred_path = tmp_path / "predictions.db"
    pf_path = tmp_path / "portfolio.db"

    conn = get_connection(wl_path)
    add_watch(conn, ticker="AVGO", market="US", entry_low=150, entry_high=155)
    conn.close()

    _seed_predictions_db(pred_path)  # NVDA, AAPL
    _seed_portfolio_db(pf_path)  # NVDA, MSFT

    targets = load_unified_watchlist(
        market="US",
        watchlist_db_path=wl_path,
        predictions_db_path=pred_path,
        portfolio_db_path=pf_path,
    )
    by_ticker = {t.ticker: t for t in targets}
    # AVGO from saved, AAPL from prediction, MSFT from position, NVDA deduped.
    assert set(by_ticker) == {"AVGO", "AAPL", "MSFT", "NVDA"}


def test_precedence_saved_over_prediction_over_position(tmp_path):
    """Saved beats prediction beats position for the same (ticker, market)."""
    wl_path = tmp_path / "watchlist.db"
    pred_path = tmp_path / "predictions.db"
    pf_path = tmp_path / "portfolio.db"

    # NVDA exists in all three; saved must win.
    conn = get_connection(wl_path)
    add_watch(
        conn, ticker="NVDA", market="US", entry_low=100, entry_high=105, target=200
    )
    conn.close()
    _seed_predictions_db(pred_path)  # NVDA prediction entry 120, target 140
    _seed_portfolio_db(pf_path)  # NVDA position avg 130

    targets = load_unified_watchlist(
        market="US",
        watchlist_db_path=wl_path,
        predictions_db_path=pred_path,
        portfolio_db_path=pf_path,
    )
    nvda = next(t for t in targets if t.ticker == "NVDA")
    assert nvda.source == "saved"
    assert nvda.entry_low == 100
    assert nvda.target == 200


def test_precedence_prediction_over_position(tmp_path):
    """With no saved row, a prediction beats a position for the same ticker."""
    pred_path = tmp_path / "predictions.db"
    pf_path = tmp_path / "portfolio.db"
    _seed_predictions_db(pred_path)  # NVDA prediction
    _seed_portfolio_db(pf_path)  # NVDA position

    targets = load_unified_watchlist(
        market="US",
        watchlist_db_path=tmp_path / "watchlist.db",
        predictions_db_path=pred_path,
        portfolio_db_path=pf_path,
    )
    nvda = next(t for t in targets if t.ticker == "NVDA")
    assert nvda.source == "prediction"
    assert nvda.target == 140.0


def test_position_default_stop(tmp_path):
    """A bare position gets a default protective stop and no target."""
    pf_path = tmp_path / "portfolio.db"
    _seed_portfolio_db(pf_path)  # MSFT avg 400, NVDA avg 130

    targets = load_unified_watchlist(
        market="US",
        watchlist_db_path=tmp_path / "watchlist.db",
        predictions_db_path=tmp_path / "missing_predictions.db",
        portfolio_db_path=pf_path,
    )
    msft = next(t for t in targets if t.ticker == "MSFT")
    assert msft.source == "position"
    assert msft.target is None
    assert msft.stop == pytest.approx(400.0 * POSITION_DEFAULT_STOP_PCT)


def test_open_prediction_dedup_keeps_newest(tmp_path):
    """When two OPEN predictions share (ticker, market), the newest wins.

    Predictions are selected ORDER BY created_at DESC so the most recent
    prediction's levels are the ones that survive the (ticker, market) dedup —
    an older/arbitrary prediction must not mask the latest levels.
    """
    from models import get_connection as pred_conn, insert_prediction, Prediction

    pred_path = tmp_path / "predictions.db"
    conn = pred_conn(pred_path)
    try:
        # Older prediction (stale levels) inserted first.
        insert_prediction(
            conn,
            Prediction(
                ticker="NVDA",
                market="US",
                direction="BULL",
                confidence=0.7,
                timeframe="1W",
                reasoning="old thesis",
                entry_price=100.0,
                target_price=120.0,
                stop_price=90.0,
                created_at="2026-01-01T00:00:00+00:00",
            ),
        )
        # Newer prediction (current levels) — must win the dedup.
        insert_prediction(
            conn,
            Prediction(
                ticker="NVDA",
                market="US",
                direction="BULL",
                confidence=0.8,
                timeframe="1M",
                reasoning="new thesis",
                entry_price=150.0,
                target_price=180.0,
                stop_price=140.0,
                created_at="2026-06-01T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()

    targets = load_unified_watchlist(
        market="US",
        watchlist_db_path=tmp_path / "watchlist.db",
        predictions_db_path=pred_path,
        portfolio_db_path=tmp_path / "no_pf.db",
    )
    nvda = next(t for t in targets if t.ticker == "NVDA")
    assert nvda.source == "prediction"
    assert nvda.target == 180.0  # newest prediction's level
    assert nvda.entry_low == 150.0


def test_open_prediction_dedup_same_created_at_uses_insertion_order(tmp_path):
    """Same created_at → the later-inserted prediction wins (rowid DESC tiebreak).

    predictions.id is a UUID, so it can't order ties chronologically; the query
    falls back to rowid (insertion order) so the most recently inserted row wins.
    """
    from models import get_connection as pred_conn, insert_prediction, Prediction

    same_ts = "2026-03-01T00:00:00+00:00"
    pred_path = tmp_path / "predictions.db"
    conn = pred_conn(pred_path)
    try:
        insert_prediction(
            conn,
            Prediction(
                ticker="NVDA",
                market="US",
                direction="BULL",
                confidence=0.7,
                timeframe="1W",
                reasoning="first",
                entry_price=100.0,
                target_price=110.0,
                stop_price=95.0,
                created_at=same_ts,
            ),
        )
        insert_prediction(
            conn,
            Prediction(
                ticker="NVDA",
                market="US",
                direction="BULL",
                confidence=0.7,
                timeframe="1M",
                reasoning="second",
                entry_price=200.0,
                target_price=210.0,
                stop_price=195.0,
                created_at=same_ts,
            ),
        )
    finally:
        conn.close()

    targets = load_unified_watchlist(
        market="US",
        watchlist_db_path=tmp_path / "watchlist.db",
        predictions_db_path=pred_path,
        portfolio_db_path=tmp_path / "no_pf.db",
    )
    nvda = next(t for t in targets if t.ticker == "NVDA")
    assert nvda.target == 210.0  # later-inserted row wins the tie


def test_missing_dbs_yield_only_saved(tmp_path):
    """Absent predictions/portfolio DBs degrade to saved-only."""
    wl_path = tmp_path / "watchlist.db"
    conn = get_connection(wl_path)
    add_watch(conn, ticker="NVDA", market="US", entry_low=100, entry_high=105)
    conn.close()

    targets = load_unified_watchlist(
        market="US",
        watchlist_db_path=wl_path,
        predictions_db_path=tmp_path / "nope_pred.db",
        portfolio_db_path=tmp_path / "nope_pf.db",
    )
    assert len(targets) == 1
    assert targets[0].source == "saved"
