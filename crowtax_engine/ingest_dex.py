"""DEX on-chain trade ingestion from block explorers."""

import argparse
import logging
import os

import requests

from crowtax_engine.db import PONYBOY_DSN, get_conn
from crowtax_engine.staging import ingest_raw

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10


def _get_api_key(env_var: str) -> str | None:
    """Read API key from environment. Returns None if not set."""
    key = os.environ.get(env_var)
    if not key:
        log.warning("Environment variable %s not set — skipping", env_var)
    return key


def _fetch_eth_transactions(address: str, api_key: str,
                            base_url: str = "https://api.etherscan.io/api") -> list:
    """Fetch ETH/INK normal + token transactions via Etherscan-compatible API."""
    txns = []

    # Normal transactions
    try:
        resp = requests.get(base_url, params={
            "module": "account", "action": "txlist",
            "address": address, "sort": "asc", "apikey": api_key,
        }, timeout=REQUEST_TIMEOUT)
        data = resp.json()
        if data.get("status") == "1" and data.get("result"):
            for tx in data["result"]:
                txns.append({
                    "tx_hash": tx["hash"],
                    "block_number": int(tx.get("blockNumber", 0)),
                    "from": tx.get("from", ""),
                    "to": tx.get("to", ""),
                    "value": tx.get("value", "0"),
                    "timestamp": int(tx.get("timeStamp", 0)),
                    "fee": str(int(tx.get("gasUsed", 0)) * int(tx.get("gasPrice", 0))),
                    "token": "ETH",
                    "tx_type": "normal",
                })
    except Exception as e:
        log.error("ETH txlist failed: %s", e)

    # Token transfers
    try:
        resp = requests.get(base_url, params={
            "module": "account", "action": "tokentx",
            "address": address, "sort": "asc", "apikey": api_key,
        }, timeout=REQUEST_TIMEOUT)
        data = resp.json()
        if data.get("status") == "1" and data.get("result"):
            for tx in data["result"]:
                txns.append({
                    "tx_hash": tx["hash"],
                    "block_number": int(tx.get("blockNumber", 0)),
                    "from": tx.get("from", ""),
                    "to": tx.get("to", ""),
                    "value": tx.get("value", "0"),
                    "timestamp": int(tx.get("timeStamp", 0)),
                    "fee": "0",
                    "token": tx.get("tokenSymbol", "UNKNOWN"),
                    "token_decimal": int(tx.get("tokenDecimal", 18)),
                    "tx_type": "token",
                })
    except Exception as e:
        log.error("ETH tokentx failed: %s", e)

    return txns


def _fetch_sol_transactions(address: str) -> list:
    """Fetch Solana transactions via public RPC."""
    txns = []
    rpc_url = "https://api.mainnet-beta.solana.com"

    try:
        # Get signatures
        resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": 100}],
        }, timeout=REQUEST_TIMEOUT)
        sigs = resp.json().get("result", [])

        for sig_info in sigs:
            sig = sig_info["signature"]
            try:
                tx_resp = requests.post(rpc_url, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getParsedTransaction",
                    "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                }, timeout=REQUEST_TIMEOUT)
                tx_data = tx_resp.json().get("result")
                if tx_data:
                    txns.append({
                        "tx_hash": sig,
                        "block_number": tx_data.get("slot", 0),
                        "timestamp": tx_data.get("blockTime", 0),
                        "raw": tx_data,
                        "token": "SOL",
                        "tx_type": "solana",
                    })
            except Exception as e:
                log.error("SOL getParsedTransaction failed for %s: %s", sig, e)
                continue
    except Exception as e:
        log.error("SOL getSignaturesForAddress failed: %s", e)

    return txns


def _fetch_hype_fills(address: str) -> list:
    """Fetch Hyperliquid user fills."""
    txns = []
    try:
        resp = requests.post("https://api.hyperliquid.xyz/info", json={
            "type": "userFills", "user": address,
        }, timeout=REQUEST_TIMEOUT)
        fills = resp.json()
        if isinstance(fills, list):
            for fill in fills:
                txns.append({
                    "tx_hash": f"hype:{fill.get('tid', fill.get('oid', ''))}",
                    "timestamp": int(fill.get("time", 0)) // 1000,  # ms to s
                    "token": fill.get("coin", ""),
                    "side": fill.get("side", ""),
                    "sz": fill.get("sz", "0"),
                    "px": fill.get("px", "0"),
                    "fee": fill.get("fee", "0"),
                    "tx_type": "hype_fill",
                })
    except Exception as e:
        log.error("HYPE userFills failed: %s", e)

    return txns


def _fetch_sui_transactions(address: str) -> list:
    """Fetch SUI transactions."""
    txns = []
    rpc_url = "https://fullnode.mainnet.sui.io:443"

    try:
        resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "suix_queryTransactionBlocks",
            "params": [{"filter": {"FromAddress": address}}, None, 50, True],
        }, timeout=REQUEST_TIMEOUT)
        result = resp.json().get("result", {})
        for tx in result.get("data", []):
            txns.append({
                "tx_hash": tx.get("digest", ""),
                "timestamp": int(tx.get("timestampMs", 0)) // 1000,
                "raw": tx,
                "token": "SUI",
                "tx_type": "sui",
            })
    except Exception as e:
        log.error("SUI queryTransactionBlocks failed: %s", e)

    return txns


def _fetch_btc_transactions(address: str) -> list:
    """Fetch BTC transactions via Blockstream API."""
    txns = []
    try:
        resp = requests.get(
            f"https://blockstream.info/api/address/{address}/txs",
            timeout=REQUEST_TIMEOUT)
        data = resp.json()
        if isinstance(data, list):
            for tx in data:
                txns.append({
                    "tx_hash": tx.get("txid", ""),
                    "block_number": tx.get("status", {}).get("block_height", 0),
                    "timestamp": tx.get("status", {}).get("block_time", 0),
                    "raw": tx,
                    "token": "BTC",
                    "tx_type": "btc",
                })
    except Exception as e:
        log.error("BTC fetch failed: %s", e)

    return txns


# Chain handlers: (fetch_fn, api_key_env, requires_key, default_status)
CHAIN_HANDLERS = {
    "ETH": {
        "fetch": lambda addr, key: _fetch_eth_transactions(addr, key),
        "api_key_env": "ETHERSCAN_API_KEY",
        "requires_key": True,
        "default_status": "pending",
    },
    "INK": {
        "fetch": lambda addr, key: _fetch_eth_transactions(
            addr, key, "https://explorer.inkonchain.com/api"),
        "api_key_env": "INKSCAN_API_KEY",
        "requires_key": True,
        "default_status": "pending",
    },
    "SOL": {
        "fetch": lambda addr, _: _fetch_sol_transactions(addr),
        "api_key_env": None,
        "requires_key": False,
        "default_status": "pending",
    },
    "HYPE": {
        "fetch": lambda addr, _: _fetch_hype_fills(addr),
        "api_key_env": None,
        "requires_key": False,
        "default_status": "confirmed",  # Exchange-confirmed
    },
    "SUI": {
        "fetch": lambda addr, _: _fetch_sui_transactions(addr),
        "api_key_env": None,
        "requires_key": False,
        "default_status": "pending",
    },
    "BTC": {
        "fetch": lambda addr, _: _fetch_btc_transactions(addr),
        "api_key_env": None,
        "requires_key": False,
        "default_status": "pending",
    },
}


def sync_dex_trades(conn, chain: str, address: str):
    """Pull transactions from block explorers and ingest into tax engine."""
    conn.autocommit = False

    chain = chain.upper()
    handler = CHAIN_HANDLERS.get(chain)
    if not handler:
        raise ValueError(f"Unsupported chain: {chain}. Supported: {list(CHAIN_HANDLERS.keys())}")

    # Get API key if needed
    api_key = None
    if handler["requires_key"]:
        api_key = _get_api_key(handler["api_key_env"])
        if not api_key:
            log.warning("Skipping %s — no API key", chain)
            return 0

    # Fetch transactions
    log.info("Fetching %s transactions for %s", chain, address)
    txns = handler["fetch"](address, api_key)
    log.info("Found %d %s transactions", len(txns), chain)

    ingested = 0
    for tx in txns:
        tx_hash = tx.get("tx_hash")
        if not tx_hash:
            continue

        raw_json = {
            "chain": chain,
            "address": address,
            "wallet_address": address,
            **tx,
        }
        # Remove non-serializable data
        raw_json.pop("raw", None)

        try:
            row_id = ingest_raw(
                conn, source="dex", chain=chain,
                timestamp=tx.get("timestamp", 0),
                raw_json=raw_json,
                tx_hash=tx_hash,
                block_number=tx.get("block_number"),
                status=handler["default_status"])
            if row_id > 0:
                ingested += 1
        except Exception as e:
            log.error("Failed to ingest %s tx %s: %s", chain, tx_hash, e)
            continue

    log.info("Ingested %d new %s transactions", ingested, chain)
    return ingested


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Ingest DEX on-chain trades")
    parser.add_argument("--chain", required=True,
                        choices=list(CHAIN_HANDLERS.keys()),
                        help="Blockchain to scan")
    parser.add_argument("--address", required=True, help="Wallet address")
    args = parser.parse_args()

    conn = get_conn(PONYBOY_DSN)
    conn.autocommit = False
    try:
        count = sync_dex_trades(conn, args.chain, args.address)
        print(f"Ingested {count} {args.chain} transactions")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------
# Roadmap 2.3: Uniswap v3 LP event dispatch
#
# When a raw transaction (already in tax_raw_transactions) matches
# Uniswap v3 mint/burn/collect, route it through tax.lp_uniswap_v3
# rather than the generic ETH-tx promotion path.  See file 1 sec 1.10
# (uncertain -- verify with a CPA) for the deposit-as-disposition
# position adopted there.
#
# Other DEX events (Uniswap v2, Curve, Balancer, SushiSwap) are
# explicitly out of scope for 2.3 and continue down the existing
# generic path unchanged.
# ---------------------------------------------------------------------

def route_uniswap_v3(conn, raw_row, normalized_event):
    '''Forward a normalised v3 event into the LP handler.

    ``raw_row`` is the tax_raw_transactions dict; ``normalized_event``
    is whatever shape the caller has (chain-specific decoder).  The
    caller is responsible for normalising.  Returns the dispatcher
    output dict.

    A no-op (returns None) when the row is not a Uniswap v3 event.
    Other DEX events fall through to the generic path.
    '''
    from crowtax_engine.lp_uniswap_v3 import dispatch, is_uniswap_v3_event

    if not is_uniswap_v3_event(raw_row):
        return None
    return dispatch(conn, normalized_event)

