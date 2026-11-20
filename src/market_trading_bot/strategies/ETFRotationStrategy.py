"""复现聚宽加权动量评分与目标波动率仓位。"""
import math
import numpy as np
from ..models import MarketSnapshot, RankedETF, StrategyResult
from datetime import datetime


class ETFRotationStrategy:
    """按加权动量选取一只 ETF，再按目标年化波动率计算仓位。"""

    def __init__(self, parameters: dict):
        """读取 ETF 池、价格窗口和年化参数；配置无效时抛出 ValueError。

        :param parameters: 策略参数字典；当前仅支持 select_num=1。
        """
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
        """所需历史收盘价数量；两个计算窗口均另含一笔当日价格。"""
        return max(self.momentum_points, self.volatility_points) - 1

    def calculate(self, snapshots: dict[str, MarketSnapshot], as_of: datetime) -> StrategyResult:
        """计算动量排名，将最高分标的按目标波动率分配仓位。

        得分为年化回归收益乘 R²；全部为负仍选最高分，同分按 ETF 池顺序。
        仓位上限为 100%，年化波动率低于 1e-6 时满仓，其余标的权重为零。

        :param snapshots: 经行情层校验的快照，价格有效、历史连续且报价未过期。
        :param as_of: 本次计算时刻，须与报价使用相同的时区口径。
        :return: 计算时刻、按得分降序的排名和目标权重（0 至 1）。
        :raises KeyError: 缺少 ETF 池中的标的。
        :raises ValueError: 历史不足或计算结果非有限数。
        """
        ranking = []
        for symbol in self.symbols:
            snapshot = snapshots[symbol]
            if len(snapshot.history) < self.history_points:
                raise ValueError(f'{symbol}: 历史价格不足')
            prices = np.array([bar.close for bar in snapshot.history] + [snapshot.price], dtype=float)
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
