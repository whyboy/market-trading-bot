"""模拟账户核心测试使用的共同数据。"""
from datetime import datetime, timedelta
from market_trading_bot.repository.PapAccountRepository import PapAccountRepository
from market_trading_bot.strategy.StrategyData import RankedETF, StrategyData
from market_trading_bot.task.TradingTask import TradingTask
from market_trading_bot.trader.PapTrader import PapTrader
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from zoneinfo import ZoneInfo


class PapFixture:
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rules = {'A': {'lot_size': 100, 't_plus_one': True},
                      'B': {'lot_size': 100, 't_plus_one': False}}
        self.executor = PapTrader({'commission_rate': .0002, 'minimum_commission': 5,
                                       'slippage_rate': 0}, self.rules)
        self.account_store = PapAccountRepository(self.root, 'pap')

    def signal(self, day=20, winner='A', weight=1, prices=(10, 20)):
        now = datetime(2026, 11, day, 14, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
        ranking = tuple(RankedETF(symbol, 1, .2, price, now, now.date() - timedelta(days=30),
                                  now.date() - timedelta(days=1)) for symbol, price in zip(self.rules, prices))
        return StrategyData(now, ranking, {symbol: weight if symbol == winner else 0 for symbol in self.rules})

    def make_task(self, signal, executor=None, initial_cash=10000, repository=None):
        strategy = Mock()
        strategy.calculate.return_value = signal
        market = Mock()
        market.load.return_value = (signal.as_of, {})
        return TradingTask(strategy, market, executor or self.executor, repository or self.account_store, Mock(),
                           strategy_id='etf_rotation', instance_id='pap', initial_cash=initial_cash,
                           scheduled_at=signal.as_of, notifier=Mock())

    def rebalance(self, signal, executor, initial_cash, repository=None):
        return self.make_task(signal, executor, initial_cash, repository)._rebalance(signal)
