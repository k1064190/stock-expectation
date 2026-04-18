"""CSV import for portfolio transactions.

Parses CSV files with trade data, validates rows against market rules,
detects duplicates, and returns structured results.
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CsvParseResult:
    """Result of parsing a CSV file.

    Args:
        rows: Valid rows ready for insertion.
        errors: List of error messages for invalid rows.
        duplicates: List of messages for duplicate rows.
    """

    rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)


_KR_TICKER_RE = re.compile(r"^\d{6}$")
_US_TICKER_RE = re.compile(r"^[A-Za-z]{1,5}$")


def validate_csv_row(row: dict, line_num: int, market: str) -> list[str]:
    """Validate a single CSV row.

    Args:
        row: Dict with keys: date, ticker, side, quantity, price.
        line_num: Line number in the CSV file.
        market: "US" or "KR".

    Returns:
        List of error messages. Empty = valid.
    """
    errors = []
    side = row.get("side", "").upper()
    if side not in ("BUY", "SELL"):
        errors.append(
            f"Line {line_num}: side must be BUY or SELL, got '{row.get('side')}'"
        )

    try:
        qty = float(row.get("quantity", ""))
        if qty <= 0:
            errors.append(f"Line {line_num}: quantity must be positive, got {qty}")
    except ValueError:
        errors.append(
            f"Line {line_num}: quantity is not a number: '{row.get('quantity')}'"
        )

    try:
        price = float(row.get("price", ""))
        if price <= 0:
            errors.append(f"Line {line_num}: price must be positive, got {price}")
    except ValueError:
        errors.append(f"Line {line_num}: price is not a number: '{row.get('price')}'")

    ticker = row.get("ticker", "").strip()
    if market == "KR" and not _KR_TICKER_RE.match(ticker):
        errors.append(
            f"Line {line_num}: ticker '{ticker}' doesn't match KR format (6 digits)"
        )
    elif market == "US" and not _US_TICKER_RE.match(ticker):
        errors.append(
            f"Line {line_num}: ticker '{ticker}' doesn't match US format (1-5 letters)"
        )

    return errors


def parse_csv(path: Path, market: str) -> CsvParseResult:
    """Parse a CSV file of trade records.

    Args:
        path: Path to the CSV file.
        market: "US" or "KR".

    Returns:
        CsvParseResult with valid rows, errors, and duplicates.
    """
    result = CsvParseResult()
    seen: set[tuple] = set()

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=1):
            errors = validate_csv_row(row, line_num=i, market=market)
            if errors:
                result.errors.extend(errors)
                continue

            key = (
                row["date"].strip(),
                row["ticker"].strip(),
                row["side"].strip().upper(),
                row["quantity"].strip(),
                row["price"].strip(),
            )
            if key in seen:
                result.duplicates.append(
                    f"Line {i}: duplicate of ({key[0]}, {key[1]}, {key[2]}, qty={key[3]}, price={key[4]})"
                )
                continue
            seen.add(key)

            result.rows.append(
                {
                    "date": row["date"].strip(),
                    "ticker": row["ticker"].strip(),
                    "side": row["side"].strip().upper(),
                    "quantity": float(row["quantity"]),
                    "price": float(row["price"]),
                    "note": row.get("note", "").strip() if row.get("note") else "",
                    "thesis_id": (
                        row.get("thesis_id", "").strip()
                        if row.get("thesis_id")
                        else None
                    ),
                }
            )

    return result
