"""策略计算使用的行情快照与目标持仓。"""
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class DailyClose:
    date: date
    close: float


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    quote_time: datetime
    price: float
    history: tuple[DailyClose, ...]


@dataclass(frozen=True)
class RankedETF:
    symbol: str
    score: float
    annual_volatility: float
    price: float
    quote_time: datetime
    history_start: date
    history_end: date


@dataclass(frozen=True)
class StrategyResult:
    as_of: datetime
    ranking: tuple[RankedETF, ...]
    target_weights: dict[str, float]
