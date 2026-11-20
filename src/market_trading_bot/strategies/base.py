"""策略仅根据行情计算目标持仓，不依赖交易执行器。"""
from typing import Protocol
from datetime import datetime
from ..models import MarketSnapshot, StrategyResult


class Strategy(Protocol):
    def calculate(
        self, snapshots: dict[str, MarketSnapshot], as_of: datetime
    ) -> StrategyResult: ...
