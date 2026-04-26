# crowtax-engine

A privacy-first crypto tax engine. Library, not a SaaS — run it locally, audit the math.

## What it does

- **Per-wallet / per-account basis tracking** (Treas. Reg. §1.1012-1(j) + Rev. Proc. 2024-28; mandatory for dispositions on/after 2025-01-01)
- **Cost basis methods**: FIFO, LIFO, HIFO, Specific Identification
- **Wallet-to-wallet transfer handling** with tacked holding period (no synthetic disposals on self-transfers)
- **Fee placement** matching IRS authority: sell-side fees reduce proceeds (IRC §1001(b), Treas. Reg. §1.1001-1); buy-side fees increase basis (IRC §1012, *Commissioner v. Woodward* 397 U.S. 572 (1970))
- **Wash-sale detection / application split**: detection always on, application off by default (IRC §1091 reaches "stock or securities"; crypto is property per Notice 2014-21). Toggleable so the data is ready if law changes.
- **Ordinary-income ledger** for mining, staking, airdrops, forks (Rev. Rul. 2019-24, 2023-14) with three-tier FMV resolution
- **Perp funding payments** as ordinary income/expense with year+direction aggregator (uncertain — verify with a CPA)
- **Form 8949** (Parts I + II + collectibles 28% rate per IRC §1(h)(4)), **Schedule D** summary, **Schedule 1** ordinary income, **NC D-400 AGI** contribution line
- **HDAF audit export** (per-account lifetime ledger in CSV + summary JSON)
- **Rev. Proc. 2024-28 election** documentation with manifest/summary CPA warnings
- **1099-DA ingest + reconciliation** with adjustment-code proposals (B/O codes for column (f))
- **Wrap / stable swap** detection (USDC↔USDT, ETH↔WETH, BTC↔WBTC) with optional zero-gain suppression
- **NFT classification framework** for collectibles vs non-collectibles
- **Uniswap v3 LP** mint / burn / collect with impermanent-loss handling (uncertain — verify with a CPA)

## What it does NOT do

- **Data ingestion from exchanges or chains.** That's the SaaS wrapper's job. This engine reads from a normalized Postgres schema (see `crowtax_engine/migrations/`) and produces tax forms. Bring your own ingest layer.
- **USD pricing for on-chain events.** Inject prices upstream; the engine treats the price field as authoritative.
- **Tax advice.** Software output is not tax advice. Engage a CPA for filing decisions, especially the four open questions below.

## Install

```bash
pip install crowtax-engine            # not yet on PyPI
# or, from a local checkout:
pip install -e /path/to/crowtax-engine
```

Requires Python 3.11+, Postgres 14+.

## Database setup

```bash
createdb crowtax_engine
export CROWTAX_ENGINE_DATABASE_URL=postgresql://localhost/crowtax_engine

for f in $(ls crowtax_engine/migrations/*.sql | sort); do
  psql -d crowtax_engine -f "$f"
done
```

Migrations are forward-only and additive. Schema lives at `crowtax_engine/migrations/`.

## Quickstart

```python
from crowtax_engine import accounts, staging, engine, report

# 1. Open a connection
from crowtax_engine.db import get_conn
conn = get_conn()

# 2. Create an account (a wallet or exchange subaccount)
account_id = accounts.get_or_create_account(
    conn,
    name="coinbase-main",
    chain_or_exchange="coinbase",
    address_or_key_label="user@example.com",
)

# 3. Ingest raw transactions
# (your ingest layer writes to tax_raw_transactions; staging.promote_confirmed
# normalizes them into tax_lots and tax_disposals)

# 4. Match disposals to lots using the chosen method
engine.rematch_all(conn, method="fifo")

# 5. Generate the report
tax_report = report.generate_report(
    conn,
    year=2026,
    method="fifo",
    apply_wash_sale=False,  # crypto-as-property default
)

# 6. Export
csv_path = report.export_csv(tax_report, out_dir="./out")
```

Or use the CLI:

```bash
python -m crowtax_engine --help
```

## Run the test suite

```bash
createdb crowtax_engine_test
export CROWTAX_ENGINE_TEST_DATABASE_URL=postgresql://localhost/crowtax_engine_test
pip install -e .[dev]
pytest
```

Expected: 95 tests pass.

## Open questions (verify with a CPA)

The engine is mechanically correct under defensible interpretations of current law. Four areas remain unsettled and are explicitly flagged in the code:

1. **Rev. Proc. 2024-28 election timing.** Did you file an allocation election before your first 2025 disposition? Affects per-wallet vs. universal-pool transition rules.
2. **Wash-sale posture.** §1091 doesn't reach crypto today, but Build Back Better §138153 and similar drafts have proposed retroactive extension. The engine defaults to detect-but-don't-apply so the data is ready if law changes.
3. **Perp funding character.** No IRS primary guidance. Practitioner consensus: ordinary income/expense at payment. Mark-to-market and basis-adjustment alternatives are defensible.
4. **DeFi LP add-as-swap.** No primary guidance on whether LP deposit is a realization event. Conservative default: yes, two-leg swap at FMV.

## Origin

Extracted from the The Crow Show trading platform. The same engine powers the SaaS at https://tax.commputer.xyz.

## License

MIT — see `LICENSE`.
