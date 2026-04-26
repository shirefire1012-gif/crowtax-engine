"""Tax engine dataclasses for The Crow Show."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class RawTransaction:
    id: int
    source: str
    source_file: Optional[str]
    chain: Optional[str]
    tx_hash: Optional[str]
    block_number: Optional[int]
    timestamp: int
    raw_json: dict
    status: str
    confirmation_count: int
    required_confirmations: int


@dataclass
class TaxLot:
    id: int
    wallet_address: Optional[str]
    chain: str
    symbol: str
    acquired_at: int
    quantity: Decimal
    cost_basis_usd: Decimal
    cost_basis_per_unit: Decimal
    remaining_quantity: Decimal
    acquisition_type: str
    fee_usd: Decimal
    source: str
    source_tx_id: Optional[str]
    raw_transaction_id: Optional[int]


@dataclass
class Disposal:
    id: int
    wallet_address: Optional[str]
    chain: str
    symbol: str
    disposed_at: int
    quantity: Decimal
    proceeds_usd: Decimal
    fee_usd: Decimal
    source: str
    source_tx_id: Optional[str]
    raw_transaction_id: Optional[int]
    wash_sale_flag: bool = False


@dataclass
class LotMatch:
    id: int
    disposal_id: int
    lot_id: int
    quantity_matched: Decimal
    cost_basis_usd: Decimal
    proceeds_usd: Decimal
    gain_loss_usd: Decimal
    holding_period: str
    method: str


@dataclass
class Form8949Line:
    description: str
    date_acquired: str
    date_sold: str
    proceeds: float
    cost_basis: float
    gain_loss: float
    wash_sale: bool
    adjustment_code: str = ""
    adjustment_amount: float = 0.0
    box: str = "C"
    # Roadmap 2.2 — populated when both legs of the disposal+lot are in
    # the same wrap/stable family (USDC<->USDT, BTC<->WBTC, ETH<->WETH).
    # None for ordinary capital-gain dispositions.
    wrap_family: Optional[str] = None
    # Roadmap 2.4 — 'fungible' (default), 'nft_collectible', or
    # 'nft_non_collectible'.  Routes collectible NFT long-term gains
    # to the 28% Schedule D line.
    asset_class: str = "fungible"


@dataclass
class GainLossSummary:
    total_proceeds: float = 0.0
    total_cost_basis: float = 0.0
    total_gain_loss: float = 0.0
    num_transactions: int = 0
    wash_sale_count: int = 0


@dataclass
class TaxReport:
    year: int
    method: str
    short_term_items: list = field(default_factory=list)
    long_term_items: list = field(default_factory=list)
    short_term_total: GainLossSummary = field(default_factory=GainLossSummary)
    long_term_total: GainLossSummary = field(default_factory=GainLossSummary)
    wash_sale_count: int = 0
