"""CSV import for Coinbase, Binance, and generic exchange formats."""

import argparse
import csv
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

from crowtax_engine.db import PONYBOY_DSN, get_conn
from crowtax_engine.staging import ingest_raw, promote_confirmed

log = logging.getLogger(__name__)

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB


def _parse_timestamp(ts_str: str) -> int:
    """Parse a timestamp string to epoch seconds. Tries common formats."""
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
    ):
        try:
            dt = datetime.strptime(ts_str.strip(), fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts_str}")


def _coinbase_type(tx_type: str) -> str:
    """Map Coinbase transaction type to buy/sell/airdrop/etc."""
    tx_type = tx_type.strip().lower()
    if tx_type in ("buy", "advanced trade buy"):
        return "buy"
    if tx_type in ("sell", "advanced trade sell"):
        return "sell"
    if tx_type in ("send", "transfer"):
        return "send"
    if tx_type in ("receive",):
        return "receive"
    if tx_type in ("staking income", "rewards income", "earning reward"):
        return "staking"
    if tx_type in ("airdrop",):
        return "airdrop"
    if tx_type in ("fork",):
        return "fork"
    if tx_type in ("mining",):
        return "mining"
    if "convert" in tx_type or "swap" in tx_type:
        return "swap"
    return "other"


class CSVRowParseError(ValueError):
    """Raised when a specific CSV row cannot be parsed.

    ``row_num`` is 1-based and refers to the data row number (first data row
    after the header is row 1). ``filepath`` identifies the source file.
    """

    def __init__(self, filepath: str, row_num: int, detail: str,
                 raw_row: dict | None = None):
        self.filepath = filepath
        self.row_num = row_num
        self.detail = detail
        self.raw_row = raw_row
        super().__init__(
            f"{os.path.basename(filepath)} row {row_num}: {detail}"
        )


def _decimal_or_raise(value: str, field: str, filepath: str, row_num: int,
                     raw_row: dict) -> Decimal:
    """Parse a Decimal from a CSV cell, attributing failure to (file, row)."""
    try:
        cleaned = (value or "0").replace(",", "").replace("$", "").strip()
        return Decimal(cleaned) if cleaned else Decimal(0)
    except Exception as e:
        raise CSVRowParseError(
            filepath, row_num,
            f"could not parse {field}={value!r} as decimal ({e})",
            raw_row=raw_row,
        ) from e


def parse_coinbase(filepath: str) -> list:
    """Parse Coinbase Transaction History CSV."""
    rows = []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        # Skip header lines that start with non-CSV content
        lines = f.readlines()

    # Find the actual CSV header row
    header_idx = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("Timestamp"):
            header_idx = i
            break

    reader = csv.DictReader(lines[header_idx:])
    row_num = 0
    for r in reader:
        row_num += 1
        ts_str = r.get("Timestamp", "").strip()
        if not ts_str:
            continue
        try:
            tx_type = _coinbase_type(r.get("Transaction Type", ""))
            symbol = r.get("Asset", "").strip()
            quantity = _decimal_or_raise(
                r.get("Quantity Transacted", "0"),
                "Quantity Transacted", filepath, row_num, r,
            )
            price = _decimal_or_raise(
                r.get("Spot Price at Transaction", "0"),
                "Spot Price at Transaction", filepath, row_num, r,
            )
            fee = _decimal_or_raise(
                r.get("Fees", "0"), "Fees", filepath, row_num, r,
            )
            try:
                ts = _parse_timestamp(ts_str)
            except ValueError as e:
                raise CSVRowParseError(
                    filepath, row_num,
                    f"invalid Timestamp={ts_str!r}: {e}",
                    raw_row=r,
                ) from e
        except CSVRowParseError:
            raise
        except Exception as e:
            raise CSVRowParseError(
                filepath, row_num, f"unexpected error: {e}", raw_row=r,
            ) from e

        rows.append({
            "type": tx_type,
            "symbol": symbol,
            "quantity": str(abs(quantity)),
            "price_usd": str(price),
            "fee_usd": str(fee),
            "timestamp": ts,
            "source": "csv",
            "chain": "coinbase",
            "wallet_address": None,
        })
    return rows


def parse_binance(filepath: str) -> list:
    """Parse Binance Trade History CSV."""
    rows = []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        row_num = 0
        for r in reader:
            row_num += 1
            ts_str = r.get("Date(UTC)", "").strip()
            if not ts_str:
                continue
            try:
                side = r.get("Side", "").strip().lower()
                pair = r.get("Pair", "").strip()
                price = _decimal_or_raise(
                    r.get("Price", "0"), "Price", filepath, row_num, r,
                )
                executed_raw = (r.get("Executed", "0") or "0").rstrip(
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ "
                )
                executed = _decimal_or_raise(
                    executed_raw, "Executed", filepath, row_num, r,
                )
                fee_raw = (r.get("Fee", "0") or "0").rstrip(
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ "
                )
                fee = _decimal_or_raise(
                    fee_raw, "Fee", filepath, row_num, r,
                )
                try:
                    ts = _parse_timestamp(ts_str)
                except ValueError as e:
                    raise CSVRowParseError(
                        filepath, row_num,
                        f"invalid Date(UTC)={ts_str!r}: {e}",
                        raw_row=r,
                    ) from e
            except CSVRowParseError:
                raise
            except Exception as e:
                raise CSVRowParseError(
                    filepath, row_num, f"unexpected error: {e}", raw_row=r,
                ) from e

            # Extract base symbol from pair (e.g., BTCUSDT -> BTC)
            symbol = pair
            for quote in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB"):
                if pair.endswith(quote) and len(pair) > len(quote):
                    symbol = pair[:-len(quote)]
                    break

            # Estimate fee in USD
            fee_usd = fee * price if fee else Decimal(0)

            rows.append({
                "type": "buy" if side == "buy" else "sell",
                "symbol": symbol,
                "quantity": str(abs(executed)),
                "price_usd": str(price),
                "fee_usd": str(fee_usd),
                "timestamp": ts,
                "source": "csv",
                "chain": "binance",
                "wallet_address": None,
            })
    return rows


def parse_generic(filepath: str) -> list:
    """Parse generic CSV: date, type, symbol, quantity, price_usd, fee_usd, fee_currency."""
    rows = []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        row_num = 0
        for r in reader:
            row_num += 1
            ts_str = r.get("date", "").strip()
            if not ts_str:
                continue
            try:
                quantity = _decimal_or_raise(
                    r.get("quantity", "0"), "quantity", filepath, row_num, r,
                )
                price = _decimal_or_raise(
                    r.get("price_usd", "0"), "price_usd", filepath, row_num, r,
                )
                fee = _decimal_or_raise(
                    r.get("fee_usd", "0"), "fee_usd", filepath, row_num, r,
                )
                try:
                    ts = _parse_timestamp(ts_str)
                except ValueError as e:
                    raise CSVRowParseError(
                        filepath, row_num,
                        f"invalid date={ts_str!r}: {e}",
                        raw_row=r,
                    ) from e
            except CSVRowParseError:
                raise
            except Exception as e:
                raise CSVRowParseError(
                    filepath, row_num, f"unexpected error: {e}", raw_row=r,
                ) from e
            rows.append({
                "type": r.get("type", "buy").strip().lower(),
                "symbol": r.get("symbol", "").strip(),
                "quantity": str(quantity),
                "price_usd": str(price),
                "fee_usd": str(fee),
                "timestamp": ts,
                "source": "csv",
                "chain": r.get("chain", ""),
                "wallet_address": None,
            })
    return rows


PARSERS = {
    "coinbase": parse_coinbase,
    "binance": parse_binance,
    "generic": parse_generic,
}


def import_csv(conn, filepath: str, exchange: str):
    """Parse and ingest a CSV file. Prevents double-import via tax_csv_imports.

    The caller owns the connection's autocommit/isolation mode.
    """
    # Validate file
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")
    file_size = os.path.getsize(filepath)
    if file_size > MAX_FILE_SIZE:
        raise ValueError(f"File too large: {file_size} bytes (max {MAX_FILE_SIZE})")

    basename = os.path.basename(filepath)
    parser = PARSERS.get(exchange)
    if not parser:
        raise ValueError(f"Unknown exchange: {exchange}. Supported: {list(PARSERS.keys())}")

    # Check for duplicate import
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM tax_csv_imports WHERE filename = %s AND exchange = %s",
            (basename, exchange))
        if cur.fetchone():
            raise ValueError(f"File '{basename}' already imported for {exchange}")
    finally:
        cur.close()

    # Parse
    parsed_rows = parser(filepath)
    if not parsed_rows:
        log.warning("No rows parsed from %s", filepath)
        return 0

    # Ingest each row
    for i, row in enumerate(parsed_rows):
        row["source_tx_id"] = f"csv:{basename}:{exchange}:{i}"
        ingest_raw(
            conn, source="csv", chain=row.get("chain", exchange),
            timestamp=row["timestamp"], raw_json=row,
            source_file=basename, status="confirmed")

    # Record the import
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO tax_csv_imports (filename, exchange, rows_imported)
            VALUES (%s, %s, %s)
        """, (basename, exchange, len(parsed_rows)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()

    # Promote all confirmed
    promote_confirmed(conn)

    log.info("Imported %d rows from %s (%s)", len(parsed_rows), basename, exchange)
    return len(parsed_rows)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Import CSV trades into tax engine")
    parser.add_argument("--file", required=True, help="Path to CSV file")
    parser.add_argument("--exchange", required=True,
                        choices=list(PARSERS.keys()),
                        help="Exchange format")
    args = parser.parse_args()

    conn = get_conn(PONYBOY_DSN)
    conn.autocommit = False
    try:
        count = import_csv(conn, args.file, args.exchange)
        print(f"Imported {count} rows from {args.file}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
