"""SQLite 原子记账，CSV 是可重建的查看副本。"""
import csv
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from market_trading_bot.repository.AccountRepository import AccountRepository
from market_trading_bot.repository.DailyResultsRepository import DailyResultsRepository
from pathlib import Path


class AccountLedger:
    """按账户保存基础信息、每日交易与收盘估值，同日交易唯一约束防止重复成交。"""

    def __init__(self, directory: Path, instance_id: str, strategy_id: str = ''):
        """打开账户账本并校验账户归属。"""
        if not re.fullmatch(r'[A-Za-z0-9_-]+', instance_id):
            raise ValueError('实例ID只能包含字母、数字、下划线及连字符')
        self.instance_id = instance_id
        self.strategy_id = strategy_id
        self.directory = directory / instance_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'ledger.sqlite3'
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            AccountRepository.create_table(connection)
            AccountRepository.assign_instance(connection, (instance_id,))
            if strategy_id:
                owner = AccountRepository.get_strategy(connection).fetchone()
                if owner and owner[0] and owner[0] != strategy_id:
                    raise ValueError('账户已经属于其他策略')
                AccountRepository.assign_strategy(connection, (strategy_id,))
            DailyResultsRepository.create_table(connection)

    @staticmethod
    def get_latest_account(directory: Path, instance_id: str) -> dict:
        """只读查询最近交易资产；未开户返回 initialized=False。"""
        snapshot = AccountLedger.get_snapshot(directory, instance_id, '9999-12-31')
        if not snapshot['initialized']:
            return snapshot
        return {'initialized': True, 'date': snapshot['date'],
                'as_of': snapshot.get('signal', {}).get('as_of'),
                'cash': snapshot['cash'], 'equity': snapshot['equity'], 'positions': snapshot['positions'],
                'initial_cash': snapshot['initial_cash']}

    @staticmethod
    def get_snapshot(directory: Path, instance_id: str, day: str) -> dict:
        """只读查询 day 当天及之前的最近交易快照；未开户返回 initialized=False。"""
        if not re.fullmatch(r'[A-Za-z0-9_-]+', instance_id):
            raise ValueError('实例ID只能包含字母、数字、下划线及连字符')
        database = (directory / instance_id / 'ledger.sqlite3').resolve()
        if not database.exists():
            return {'initialized': False}
        with closing(sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)) as connection, connection:
            connection.execute('BEGIN')
            row = DailyResultsRepository.latest_execution_before(connection, (day,)).fetchone()
            if row is None:
                return {'initialized': False}
            snapshot = json.loads(row[0])
            initial_row = AccountRepository.get_initial_cash(connection).fetchone()
            if initial_row:
                initial_cash = initial_row[0]
            else:
                first = json.loads(DailyResultsRepository.first_execution(connection).fetchone()[0])
                initial_cash = str(AccountLedger._infer_initial_cash(first))
        return {**snapshot, 'initialized': True, 'initial_cash': initial_cash}

    def get_execution(self, day: str) -> dict | None:
        """查询 day 当天的交易结果；未记账返回 None。"""
        with closing(sqlite3.connect(self.database)) as connection, connection:
            row = DailyResultsRepository.execution_for_day(connection, (day,)).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    @contextmanager
    def transaction(self, day: str, initial_cash: float):
        """锁定账户并去重；保存结果后提交，异常或未保存则回滚。"""
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = DailyResultsRepository.execution_for_day(connection, (day,)).fetchone()
            completed = json.loads(row[0]) if row and row[0] is not None else None
            previous = DailyResultsRepository.latest_execution(connection).fetchone()
            account = json.loads(previous[0]) if previous else None
            if account is None:
                cash = Decimal(str(initial_cash))
                if not cash.is_finite():
                    raise ValueError('初始资金必须为有限数')
                cash = cash.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                if cash <= 0:
                    raise ValueError('初始资金按分取整后必须大于零')
                account = {'date': None, 'cash': str(cash), 'positions': {}}
                self._insert_account(connection, cash)
            transaction = self.Transaction(connection, day, account, completed)
            yield transaction
            if completed is None and not transaction.saved:
                raise RuntimeError('账户事务必须保存执行结果')

    class Transaction:
        """当前事务的账户快照和保存入口。"""

        def __init__(self, connection, day, account, completed):
            """保存事务快照。"""
            self.connection = connection
            self.day = day
            self.account = account
            self.completed = completed
            self.saved = False

        def save_execution(self, result: dict) -> dict:
            """保存交易并使旧估值失效；禁止覆盖已记账结果。"""
            if self.completed is not None or result['date'] != self.day:
                raise ValueError('不能覆盖已完成结果或保存其他日期的结果')
            payload = json.dumps(result, ensure_ascii=False, default=str, allow_nan=False)
            cursor = DailyResultsRepository.insert_execution(self.connection, (self.day, payload))
            if cursor.rowcount != 1:
                raise ValueError('不能覆盖已完成的交易结果')
            self.saved = True
            return json.loads(payload)

    def export_trades(self):
        """重建交易汇总 CSV，仅包含有成交的日期，资产采用执行时估值。"""
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            executions = [json.loads(row[0]) for row in DailyResultsRepository.all_executions(connection)]
            initial_cash = self._load_initial_cash(connection, executions)
            fields = ['date', 'buy_etf', 'buy_quantity', 'buy_price',
                      'sell_etf', 'sell_quantity', 'sell_price', 'commission',
                      'holding_etf', 'holding_quantity', 'holding_price', 'market_value',
                      'cash', 'total_assets', 'initial_cash', 'cumulative_pnl',
                      'cumulative_return_pct', 'net_value']
            temp_path = self.directory / 'account_trades.csv.tmp'
            with temp_path.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for execution in executions:
                    if not execution['fills']:
                        continue
                    equity, cash = Decimal(execution['equity']), Decimal(execution['cash'])
                    row = {'date': execution['date'], 'cash': str(cash), 'total_assets': str(equity),
                           'initial_cash': str(initial_cash), 'market_value': str(equity - cash),
                           'commission': str(sum((Decimal(fill['commission']) for fill in execution['fills']), Decimal('0.00'))),
                           'cumulative_pnl': str(equity - initial_cash),
                           'cumulative_return_pct': format((equity / initial_cash - 1) * 100, '.6f'),
                           'net_value': format(equity / initial_cash, '.8f')}
                    for side in ('buy', 'sell'):
                        fills = [fill for fill in execution['fills'] if fill['side'] == side]
                        for column, key in (('etf', 'symbol'), ('quantity', 'quantity'), ('price', 'price')):
                            row[f'{side}_{column}'] = ';'.join(str(fill[key]) for fill in fills)
                    holdings = sorted(execution['positions'])
                    prices = {item['symbol']: item['price'] for item in execution['signal']['ranking']}
                    row['holding_etf'] = ';'.join(holdings)
                    row['holding_quantity'] = ';'.join(str(execution['positions'][symbol]['quantity']) for symbol in holdings)
                    row['holding_price'] = ';'.join(str(prices[symbol]) for symbol in holdings)
                    writer.writerow(row)
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(self.directory / 'account_trades.csv')
            # 汇总替换成功后再清理旧版 CSV。
            for name in ('runs', 'equity', 'orders', 'fills', 'positions', 'signals'):
                (self.directory / f'{name}.csv').unlink(missing_ok=True)

    def _load_initial_cash(self, connection: sqlite3.Connection, executions: list[dict]) -> Decimal:
        """读取开户资金；旧账本缺失时反推并补存。"""
        row = AccountRepository.get_initial_cash(connection).fetchone()
        if row:
            return Decimal(row[0])
        if not executions:
            return Decimal('0')
        initial_cash = self._infer_initial_cash(executions[0])
        self._insert_account(connection, initial_cash)
        return initial_cash

    @staticmethod
    def _infer_initial_cash(execution: dict) -> Decimal:
        """从首次交易现金流还原开户资金。"""
        initial_cash = Decimal(execution['cash'])
        for fill in execution['fills']:
            amount, fee = Decimal(fill['amount']), Decimal(fill['commission'])
            initial_cash += amount + fee if fill['side'] == 'buy' else fee - amount
        return initial_cash

    @staticmethod
    def get_valuation(directory, instance_id, day):
        """只读查询 day 当天及之前的最近成功估值；无记录返回 None。"""
        if not re.fullmatch(r'[A-Za-z0-9_-]+', instance_id):
            raise ValueError('实例ID只能包含字母、数字、下划线及连字符')
        database = directory / instance_id / 'ledger.sqlite3'
        if not database.exists():
            return None
        with closing(sqlite3.connect(f'{database.resolve().as_uri()}?mode=ro', uri=True)) as connection:
            if not DailyResultsRepository.table_exists(connection).fetchone():
                return None
            row = DailyResultsRepository.latest_valuation(connection, (day,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_valuation(self, snapshot):
        """保存收盘估值并标记 success，保留交易结果。"""
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            DailyResultsRepository.save_valuation(connection, (snapshot['date'], payload, datetime.now(timezone.utc).isoformat()))
        return snapshot

    def set_valuation_status(self, day, status, error=''):
        """更新估值状态和错误，保留交易及估值数据。"""
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            DailyResultsRepository.set_valuation_status(connection, (day, status, error, datetime.now(timezone.utc).isoformat()))

    @staticmethod
    def get_daily_result(directory, instance_id, day):
        """只读查询 day 当天的交易、估值和状态；无记录返回 None。"""
        if not re.fullmatch(r'[A-Za-z0-9_-]+', instance_id):
            raise ValueError('实例ID只能包含字母、数字、下划线及连字符')
        database = directory / instance_id / 'ledger.sqlite3'
        if not database.exists():
            return None
        with closing(sqlite3.connect(f'{database.resolve().as_uri()}?mode=ro', uri=True)) as connection:
            if not DailyResultsRepository.table_exists(connection).fetchone():
                return None
            connection.row_factory = sqlite3.Row
            row = DailyResultsRepository.result_for_day(connection, (day,)).fetchone()
        if row is None:
            return None
        return {'date': row['day'], 'execution': json.loads(row['execution_payload']) if row['execution_payload'] else None,
                'valuation': json.loads(row['valuation_payload']) if row['valuation_payload'] else None,
                'valuation_status': row['valuation_status'], 'valuation_error': row['valuation_error']}

    def _insert_account(self, connection, initial_cash):
        """写入开户资金、账户归属和 UTC 开户时间。"""
        AccountRepository.insert_account(connection, (str(initial_cash), self.instance_id, self.strategy_id,
                                                datetime.now(timezone.utc).isoformat()))

