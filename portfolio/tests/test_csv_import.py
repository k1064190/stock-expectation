"""Tests for CSV import functionality."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from portfolio.csv_import import parse_csv, validate_csv_row


def _write_csv(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
    f.write(content)
    f.close()
    return Path(f.name)


class TestValidation:
    def test_valid_kr_row(self):
        row = {
            "date": "2026-03-15",
            "ticker": "005930",
            "side": "BUY",
            "quantity": "10",
            "price": "55000",
        }
        errors = validate_csv_row(row, line_num=1, market="KR")
        assert errors == []

    def test_valid_us_row(self):
        row = {
            "date": "2026-03-15",
            "ticker": "NVDA",
            "side": "BUY",
            "quantity": "5.5",
            "price": "120.50",
        }
        errors = validate_csv_row(row, line_num=1, market="US")
        assert errors == []

    def test_invalid_side(self):
        row = {
            "date": "2026-03-15",
            "ticker": "NVDA",
            "side": "HOLD",
            "quantity": "5",
            "price": "120",
        }
        errors = validate_csv_row(row, line_num=1, market="US")
        assert len(errors) == 1
        assert "side" in errors[0].lower()

    def test_ticker_market_mismatch(self):
        row = {
            "date": "2026-03-15",
            "ticker": "NVDA",
            "side": "BUY",
            "quantity": "5",
            "price": "120",
        }
        errors = validate_csv_row(row, line_num=1, market="KR")
        assert len(errors) == 1
        assert "ticker" in errors[0].lower()

    def test_negative_quantity(self):
        row = {
            "date": "2026-03-15",
            "ticker": "NVDA",
            "side": "BUY",
            "quantity": "-5",
            "price": "120",
        }
        errors = validate_csv_row(row, line_num=1, market="US")
        assert len(errors) == 1


class TestParseCsv:
    def test_parse_valid_csv(self):
        path = _write_csv(
            "date,ticker,side,quantity,price,note\n"
            "2026-01-10,005930,BUY,10,55000,earnings play\n"
            "2026-03-01,005930,SELL,5,60000,partial take-profit\n"
        )
        result = parse_csv(path, market="KR")
        assert len(result.rows) == 2
        assert len(result.errors) == 0
        assert result.rows[0]["ticker"] == "005930"
        path.unlink()

    def test_parse_csv_with_errors(self):
        path = _write_csv(
            "date,ticker,side,quantity,price,note\n"
            "2026-01-10,NVDA,BUY,10,120,\n"
            "2026-03-01,005930,BUY,5,55000,\n"
        )
        result = parse_csv(path, market="KR")
        assert len(result.rows) == 1
        assert len(result.errors) == 1
        path.unlink()

    def test_parse_csv_with_optional_note(self):
        path = _write_csv(
            "date,ticker,side,quantity,price,note\n" "2026-01-10,NVDA,BUY,10,120,\n"
        )
        result = parse_csv(path, market="US")
        assert len(result.rows) == 1
        assert result.rows[0].get("note", "") == ""
        path.unlink()

    def test_parse_csv_with_thesis_id(self):
        path = _write_csv(
            "date,ticker,side,quantity,price,note,thesis_id\n"
            "2026-01-10,NVDA,BUY,10,120,AI play,th_abc123\n"
        )
        result = parse_csv(path, market="US")
        assert len(result.rows) == 1
        assert result.rows[0]["thesis_id"] == "th_abc123"
        path.unlink()

    def test_duplicate_detection(self):
        path = _write_csv(
            "date,ticker,side,quantity,price,note\n"
            "2026-01-10,NVDA,BUY,10,120,first\n"
            "2026-01-10,NVDA,BUY,10,120,duplicate\n"
        )
        result = parse_csv(path, market="US")
        assert len(result.rows) == 1
        assert len(result.duplicates) == 1
        path.unlink()
