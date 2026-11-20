"""复现聚宽加权动量评分与目标波动率仓位。"""
import math
import numpy as np
from ..models import MarketSnapshot, RankedETF, StrategyResult
from datetime import datetime


class ETFRotationStrategy:
    def __init__(self, parameters: dict):
        self.symbols = tuple(parameters['etf_pool'])
        self.momentum_points = parameters['momentum_price_points']
        self.volatility_points = parameters['volatility_price_points']
        self.target_vol = float(parameters['target_annual_volatility'])
        self.annual_days = parameters['annualization_days']
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError('ETF池不能为空或包含重复标的')
        if parameters['select_num'] != 1:
            raise ValueError('当前ETF轮动策略仅支持select_num=1')
        if any(type(x) is not int or x < minimum for x, minimum in (
            (self.momentum_points, 3), (self.volatility_points, 3), (self.annual_days, 1)
        )):
            raise ValueError('窗口必须为至少3点的整数，年化天数必须为正整数')
        if not math.isfinite(self.target_vol) or self.target_vol < 0:
            raise ValueError('目标波动率必须为非负有限数')

    @property
    def history_points(self) -> int:
        return max(self.momentum_points, self.volatility_points) - 1

    def calculate(self, snapshots: dict[str, MarketSnapshot], as_of: datetime) -> StrategyResult:
        ranking = []
        for symbol in self.symbols:
            snapshot = snapshots[symbol]
            if len(snapshot.history) < self.history_points:
                raise ValueError(f'{symbol}: 历史价格不足')
            dates = [bar.date for bar in snapshot.history]
            if dates != sorted(set(dates)) or any(day >= as_of.date() for day in dates):
                raise ValueError(f'{symbol}: 历史价格日期重复、乱序或包含当日/未来数据')
            if snapshot.quote_time.date() != as_of.date() or snapshot.quote_time > as_of:
                raise ValueError(f'{symbol}: 行情时间不匹配')
            prices = np.array([bar.close for bar in snapshot.history] + [snapshot.price], dtype=float)
            if not np.all(np.isfinite(prices)) or np.any(prices <= 0):
                raise ValueError(f'{symbol}: 价格必须为正有限数')
            y = np.log(prices[-self.momentum_points:])
            x = np.arange(len(y))
            weights = np.linspace(1, 2, len(y))
            slope, intercept = np.polyfit(x, y, 1, w=weights)
            annual_return = math.expm1(float(slope) * self.annual_days)
            # 保留原聚宽口径：拟合使用w，R²使用w乘平方残差，不改为w²。
            denominator = float(np.sum(weights * (y - np.mean(y)) ** 2))
            r_squared = 0.0 if denominator < 1e-24 else 1 - float(
                np.sum(weights * (y - (slope * x + intercept)) ** 2)
            ) / denominator
            score = annual_return * r_squared
            returns = np.diff(np.log(prices[-self.volatility_points:]))
            vol = float(np.std(returns, ddof=1) * np.sqrt(self.annual_days))
            if not math.isfinite(score) or not math.isfinite(vol):
                raise ValueError(f'{symbol}: 策略计算得到非有限结果')
            ranking.append(RankedETF(symbol, score, vol, snapshot.price, snapshot.quote_time,
                                     snapshot.history[-self.momentum_points + 1].date,
                                     snapshot.history[-1].date))
        ranking.sort(key=lambda item: item.score, reverse=True)
        winner = ranking[0]
        weight = 1.0 if winner.annual_volatility < 1e-6 else min(1.0, self.target_vol / winner.annual_volatility)
        targets = {symbol: weight if symbol == winner.symbol else 0.0 for symbol in self.symbols}
        return StrategyResult(as_of, tuple(ranking), targets)
