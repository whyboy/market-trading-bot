"""SQLite 原子记账，CSV 是可重建的查看副本。"""
import csv
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


class PapAccountRepository:
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
            connection.execute('''CREATE TABLE IF NOT EXISTS account (
                                  id INTEGER PRIMARY KEY CHECK (id = 1),
                                  initial_cash TEXT NOT NULL)''')

    @staticmethod
    def get_latest_account(directory: Path, instance_id: str) -> dict:
        """只读查询最近账户快照；没有账本时不创建目录或账户。"""
        snapshot = PapAccountRepository.get_account_snapshot_on_or_before(directory, instance_id, '9999-12-31')
        if not snapshot['initialized']:
            return snapshot
        return {'initialized': True, 'date': snapshot['date'],
                'as_of': snapshot.get('signal', {}).get('as_of'),
                'cash': snapshot['cash'], 'equity': snapshot['equity'], 'positions': snapshot['positions'],
                'initial_cash': snapshot['initial_cash']}

    @staticmethod
    def get_account_snapshot_on_or_before(directory: Path, instance_id: str, day: str) -> dict:
        """只读取得指定日期及之前的最新完整账本，包含成交和实际开户资金。"""
        if not re.fullmatch(r'[A-Za-z0-9_-]+', instance_id):
            raise ValueError('实例ID只能包含字母、数字、下划线及连字符')
        database = (directory / instance_id / 'ledger.sqlite3').resolve()
        if not database.exists():
            return {'initialized': False}
        with closing(sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)) as connection, connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT payload FROM runs WHERE day<=? ORDER BY day DESC LIMIT 1', (day,)).fetchone()
            if row is None:
                return {'initialized': False}
            snapshot = json.loads(row[0])
            initial = connection.execute('SELECT initial_cash FROM account WHERE id=1').fetchone()
            if initial:
                initial_cash = initial[0]
            else:
                first = json.loads(connection.execute('SELECT payload FROM runs ORDER BY day LIMIT 1').fetchone()[0])
                initial_cash = str(PapAccountRepository._infer_initial_cash_from_first_execution([first]))
        return {**snapshot, 'initialized': True, 'initial_cash': initial_cash}

    def find_execution_by_date(self, day: str) -> dict | None:
        """读取指定日期已提交的结果；未运行返回 None。"""
        with closing(sqlite3.connect(self.database)) as connection, connection:
            row = connection.execute('SELECT payload FROM runs WHERE day = ?', (day,)).fetchone()
        return json.loads(row[0]) if row else None

    @contextmanager
    def begin_rebalance_transaction(self, day: str, initial_cash: float):
        """锁定最新账户并检查当日结果，由调用方计算后保存，整体提交或回滚。"""
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT payload FROM runs WHERE day = ?', (day,)).fetchone()
            completed = json.loads(row[0]) if row else None
            previous = connection.execute('SELECT payload FROM runs ORDER BY day DESC LIMIT 1').fetchone()
            account = json.loads(previous[0]) if previous else None
            if completed is None and account is None:
                cash = Decimal(str(initial_cash))
                if not cash.is_finite():
                    raise ValueError('初始资金必须为有限数')
                cash = cash.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                if cash <= 0:
                    raise ValueError('初始资金按分取整后必须大于零')
                account = {'date': None, 'cash': str(cash), 'positions': {}}
                connection.execute('INSERT INTO account VALUES (1, ?)', (str(cash),))
            transaction = self.Transaction(connection, day, account, completed)
            yield transaction
            if completed is None and not transaction.saved:
                raise RuntimeError('账户事务必须保存执行结果')

    class Transaction:
        """仅供事务内使用的账户快照与保存入口，不依赖策略或执行器。"""

        def __init__(self, connection, day, account, completed):
            self.connection = connection
            self.day = day
            self.account = account
            self.completed = completed
            self.saved = False

        def save_execution_result(self, result: dict) -> dict:
            if self.completed is not None or result['date'] != self.day:
                raise ValueError('不能覆盖已完成结果或保存其他日期的结果')
            payload = json.dumps(result, ensure_ascii=False, default=str, allow_nan=False)
            self.connection.execute('INSERT INTO runs VALUES (?, ?)', (self.day, payload))
            self.saved = True
            return json.loads(payload)

    def export_trade_summary_csv(self):
        """重建账户交易汇总，每个有成交的日期一行；无成交只保留在 SQLite。

        总资产采用调仓时估值；累计盈亏包含费用，收益率相对实际开户资金。
        多笔成交或多只持仓按分号对齐列出，完整明细保留在 SQLite。
        """
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            runs = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM runs ORDER BY day')]
            initial_cash = self._get_or_restore_initial_cash(connection, runs)
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

    @classmethod
    def _get_or_restore_initial_cash(cls, connection: sqlite3.Connection, runs: list[dict]) -> Decimal:
        """读取开户资金；旧账本仅首次读取时从首笔记账反推并补存。"""
        row = connection.execute('SELECT initial_cash FROM account WHERE id = 1').fetchone()
        if row:
            return Decimal(row[0])
        initial_cash = cls._infer_initial_cash_from_first_execution(runs)
        if runs:
            connection.execute('INSERT INTO account VALUES (1, ?)', (str(initial_cash),))
        return initial_cash

    @staticmethod
    def _infer_initial_cash_from_first_execution(runs: list[dict]) -> Decimal:
        """从旧账本首笔记账的现金流还原开户资金。"""
        initial_cash = Decimal(runs[0]['cash']) if runs else Decimal('0')
        if runs:
            for fill in runs[0]['fills']:
                amount, fee = Decimal(fill['amount']), Decimal(fill['commission'])
                initial_cash += amount + fee if fill['side'] == 'buy' else fee - amount
        return initial_cash
