# Changelog

## 0.4.4 — 2026-05-06

### Features

- `crowtax_engine.simple.estimate()` — pure-function tax estimator
  for the public no-auth `/simple` page. Takes 5 inputs (year,
  filing status, state, proceeds, basis) plus an optional AGI;
  returns federal + state + city breakdown.
- Holding-period split: `"short"` / `"long"` / `"mixed"` with
  user-adjustable percent (default 50/50).
- Top-bracket fallback (37%) when AGI is omitted; flagged in the
  result for UI disclosure.
- Unsupported state (no YAML) returns federal-only with
  `state_supported=False` instead of raising.
- Reuses existing `compute_jurisdiction_tax` machinery — same
  brackets, NIIT, WA excise, NYC city layering, etc.

## 0.4.3 — 2026-05-06

### Added

- `crowtax_engine.transfer_pairs` module exposes `ExclusionSet` (a frozen
  dataclass holding the out-leg and in-leg event ids of confirmed
  internal transfers) and `is_excluded_event(event_id, exclusions)` —
  the predicate engine consumers use to skip events that are one half
  of a non-taxable transfer between the user's own accounts.
- `staging.promote_confirmed(conn, *, exclusions=None)` honors the
  exclusion set: rows whose `raw_json["source_tx_id"]` appears in
  either leg of the set get advanced to `status='promoted'` without
  producing any `tax_lots` or `tax_disposals`. Engine consumers that
  don't go through CrowTax's translate-layer filter can supply this
  directly; CrowTax filters at the translate-to-staging boundary so
  it can pass `None` (default) and rely on its own filtering.

### Compatibility

- `promote_confirmed` keeps the `(conn, batch_size)` positional contract;
  `exclusions` is keyword-only with a `None` default. No existing call
  sites need to change.

## 0.1.0 — 2026-04-25

Initial extraction from The Crow Show platform.

### Features

- FIFO / LIFO / HIFO / Specific-ID basis matching
- Per-wallet / per-account basis tracking (Treas. Reg. §1.1012-1(j); Rev. Proc. 2024-28)
- Wallet-to-wallet transfer handling with tacked holding period
- Fee placement: sell fees reduce proceeds, buy fees increase basis (IRC §§1001(b), 1012)
- Wash-sale detection with apply/don't-apply split (IRC §1091; default detect-only for crypto-as-property)
- Ordinary-income ledger: mining, staking, airdrops, forks (Rev. Rul. 2019-24, 2023-14)
- Perp funding events: signed direction inference, year aggregator
- Schedule 1 / Schedule D / Form 8949 / NC D-400 structured output via `FilingPackage`
- Rev. Proc. 2024-28 election documentation
- HDAF audit export (per-account lifetime ledger)
- Wrap / stable-swap realization (USDC/USDT/DAI; BTC/WBTC/cbBTC; ETH/WETH/stETH) with optional zero-gain suppression
- NFT classification framework (collectibles 28% rate per IRC §1(h)(4))
- 1099-DA ingest + reconciliation with B/O column-(f) adjustment codes
- Uniswap v3 LP impermanent loss (mint / burn / collect)

### Known limits

- Engine reads from a normalized Postgres schema; data ingestion (exchange APIs, chain RPCs) is out of scope
- USD pricing for on-chain events expected pre-injected; no oracle inside engine
- Ethereum L2s, Curve, Balancer, SushiSwap deferred
- Open CPA questions: wash-sale posture, perp funding character, 2024-28 election timing, LP add-as-swap election
