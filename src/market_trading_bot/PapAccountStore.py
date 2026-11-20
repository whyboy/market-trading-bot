"""SQLite 原子记账，CSV 是可重建的查看副本。"""
import csv
import json
import os
import re
import sqlite3
from .models import StrategyResult
from .PapExecutor import PapExecutor
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path


class PapAccountStore:
    """按实例保存完整调仓快照，同日唯一约束防止并发及重跑重复成交。"""

    def __init__(self, directory: Path, instance_id: str):
        """在数据根目录下创建独立实例账本，禁止路径穿越。"""
        if not re.fullmatch(r'[A-Za-z0-9_-]+', instance_id):
            raise ValueError('实例ID只能包含字母、数字、下划线及连字符')
        self.directory = directory / instance_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'ledger.sqlite3'
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS runs (day TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def completed(self, day: str) -> dict | None:
        """读取指定日期已提交的结果；未运行返回 None。"""
        with closing(sqlite3.connect(self.database)) as connection, connection:
            row = connection.execute('SELECT payload FROM runs WHERE day = ?', (day,)).fetchone()
        return json.loads(row[0]) if row else None

    def rebalance(self, signal: StrategyResult, executor: PapExecutor, initial_cash: float) -> dict:
        """恢复账户并在同一事务中提交调仓结果；失败回滚，同日重跑返回首次结果。

        :param signal: 本次策略信号，日期用于同日去重。
        :param executor: 根据目标仓位计算订单、成交及新账户的模拟执行器。
        :param initial_cash: 仅首次开户使用，按分取整后须大于零。
        :return: 已提交的账户、订单、成交和信号。
        """
        day = signal.as_of.date().isoformat()
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT payload FROM runs WHERE day = ?', (day,)).fetchone()
            if row:
                return json.loads(row[0])
            previous = connection.execute('SELECT payload FROM runs ORDER BY day DESC LIMIT 1').fetchone()
            if previous:
                account = json.loads(previous[0])
            else:
                cash = Decimal(str(initial_cash))
                if not cash.is_finite():
                    raise ValueError('初始资金必须为有限数')
                cash = executor.money(cash)
                if cash <= 0:
                    raise ValueError('初始资金按分取整后必须大于零')
                account = {'date': None, 'cash': str(cash), 'positions': {}}
            result = executor.rebalance(signal, account)
            result['signal'] = asdict(signal)
            payload = json.dumps(result, ensure_ascii=False, default=str, allow_nan=False)
            connection.execute('INSERT INTO runs VALUES (?, ?)', (day, payload))
        return json.loads(payload)

    def export_csv(self):
        """重建账户交易汇总，每个有成交的日期一行；无成交只保留在 SQLite。

        总资产采用调仓时估值；累计盈亏包含费用，收益率相对实际开户资金。
        多笔成交或多只持仓按分号对齐列出，完整明细保留在 SQLite。
        """
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            runs = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM runs ORDER BY day')]
            # 从首次记账的现金流还原开户资金，兼容原有账本和首次无成交的情况。
            initial_cash = Decimal(runs[0]['cash']) if runs else Decimal('0')
            if runs:
                for fill in runs[0]['fills']:
                    amount, fee = Decimal(fill['amount']), Decimal(fill['commission'])
                    initial_cash += amount + fee if fill['side'] == 'buy' else fee - amount
            fields = ['date', 'buy_etf', 'buy_quantity', 'buy_price',
                      'sell_etf', 'sell_quantity', 'sell_price', 'commission',
                      'holding_etf', 'holding_quantity', 'holding_price', 'market_value',
                      'cash', 'total_assets', 'initial_cash', 'cumulative_pnl',
                      'cumulative_return_pct', 'net_value']
            temporary = self.directory / 'account_trades.csv.tmp'
            with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for run in runs:
                    if not run['fills']:
                        continue
                    equity, cash = Decimal(run['equity']), Decimal(run['cash'])
                    row = {'date': run['date'], 'cash': str(cash), 'total_assets': str(equity),
                           'initial_cash': str(initial_cash), 'market_value': str(equity - cash),
                           'commission': str(sum((Decimal(fill['commission']) for fill in run['fills']), Decimal('0.00'))),
                           'cumulative_pnl': str(equity - initial_cash),
                           'cumulative_return_pct': format((equity / initial_cash - 1) * 100, '.6f'),
                           'net_value': format(equity / initial_cash, '.8f')}
                    for side in ('buy', 'sell'):
                        fills = [fill for fill in run['fills'] if fill['side'] == side]
                        for column, key in (('etf', 'symbol'), ('quantity', 'quantity'), ('price', 'price')):
                            row[f'{side}_{column}'] = ';'.join(str(fill[key]) for fill in fills)
                    holdings = sorted(run['positions'])
                    prices = {item['symbol']: item['price'] for item in run['signal']['ranking']}
                    row['holding_etf'] = ';'.join(holdings)
                    row['holding_quantity'] = ';'.join(str(run['positions'][symbol]['quantity']) for symbol in holdings)
                    row['holding_price'] = ';'.join(str(prices[symbol]) for symbol in holdings)
                    writer.writerow(row)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.directory / 'account_trades.csv')
            # 新汇总成功后清理旧版自动导出的明细文件，SQLite 明细始终保留。
            for name in ('runs', 'equity', 'orders', 'fills', 'positions', 'signals'):
                (self.directory / f'{name}.csv').unlink(missing_ok=True)
