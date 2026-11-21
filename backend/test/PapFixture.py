"""模拟账户核心测试使用的共同数据。"""
from datetime import datetime, timedelta
from market_trading_bot.execution.PapExecutor import PapExecutor
from market_trading_bot.model.StrategyResult import RankedETF, StrategyResult
from market_trading_bot.repository.PapAccountRepository import PapAccountRepository
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo


class PapFixture:
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rules = {'A': {'lot_size': 100, 't_plus_one': True},
                      'B': {'lot_size': 100, 't_plus_one': False}}
        self.executor = PapExecutor({'commission_rate': .0002, 'minimum_commission': 5,
                                       'slippage_rate': 0}, self.rules)
        self.account_store = PapAccountRepository(self.root, 'pap')

    def signal(self, day=20, winner='A', weight=1, prices=(10, 20)):
        now = datetime(2026, 11, day, 14, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
        ranking = tuple(RankedETF(symbol, 1, .2, price, now, now.date() - timedelta(days=30),
                                  now.date() - timedelta(days=1)) for symbol, price in zip(self.rules, prices))
        return StrategyResult(now, ranking, {symbol: weight if symbol == winner else 0 for symbol in self.rules})
