# Changelog

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
