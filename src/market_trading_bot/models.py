"""策略计算使用的行情快照与目标持仓。"""
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class DailyClose:
    """单个历史交易日的前复权收盘价。"""

    date: date
    close: float


@dataclass(frozen=True)
class MarketSnapshot:
    """单标的行情：未复权当前价、报价时间及按日期递增的历史收盘价。"""

    symbol: str
    quote_time: datetime
    price: float
    history: tuple[DailyClose, ...]


@dataclass(frozen=True)
class RankedETF:
    """单标的评分、年化波动率和报价；历史区间仅指动量使用的历史部分。"""

    symbol: str
    score: float
    annual_volatility: float
    price: float
    quote_time: datetime
    history_start: date
    history_end: date


@dataclass(frozen=True)
class StrategyResult:
    """策略信号：计算时刻、降序排名和各标的目标权重（0 至 1）。"""

    as_of: datetime
    ranking: tuple[RankedETF, ...]
    target_weights: dict[str, float]
