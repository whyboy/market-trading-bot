"""策略目标权重及ETF排名数据。"""
from dataclasses import dataclass
from datetime import date, datetime


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
