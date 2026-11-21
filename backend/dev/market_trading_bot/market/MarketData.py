"""行情快照及历史收盘价数据。"""
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class DailyClose:
    """单个历史交易日的前复权收盘价。"""

    date: date
    close: float


@dataclass(frozen=True)
class MarketData:
    """单标的行情：未复权当前价、报价时间及按日期递增的历史收盘价。"""

    symbol: str
    quote_time: datetime
    price: float
    history: tuple[DailyClose, ...]
