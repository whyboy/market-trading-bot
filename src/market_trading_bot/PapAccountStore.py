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
        """在单个事务内恢复账户、模拟调仓和提交完整结果；异常自动回滚。

        :return: 已提交结果；同日重跑直接返回首次结果。
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
                if not cash.is_finite() or cash <= 0:
                    raise ValueError('初始资金必须为正有限数')
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
        """从权威账本重建 CSV；导出中断可重试，不会重新执行交易。"""
        # 与记账使用同一写锁，避免并发导出用旧快照覆盖新文件。
        with closing(sqlite3.connect(self.database, timeout=30)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            runs = [json.loads(row[0]) for row in connection.execute('SELECT payload FROM runs ORDER BY day')]
            tables = {
                'runs': (['date', 'status'], [{'date': run['date'], 'status': 'completed'} for run in runs]),
                'equity': (['date', 'cash', 'equity'], [{key: run[key] for key in ('date', 'cash', 'equity')} for run in runs]),
                'orders': (['date', 'order_id', 'symbol', 'side', 'requested_quantity', 'filled_quantity', 'status', 'reason'],
                           [{'date': run['date'], **order} for run in runs for order in run['orders']]),
                'fills': (['date', 'fill_id', 'order_id', 'symbol', 'side', 'quantity', 'price', 'amount', 'commission'],
                          [{'date': run['date'], **fill} for run in runs for fill in run['fills']]),
                'positions': (['date', 'symbol', 'quantity', 'sellable'],
                              [{'date': run['date'], 'symbol': symbol, **position} for run in runs for symbol, position in run['positions'].items()]),
                'signals': (['date', 'symbol', 'target_weight', 'score', 'price', 'quote_time'],
                            [{'date': run['date'], 'symbol': item['symbol'],
                              'target_weight': run['signal']['target_weights'][item['symbol']],
                              **{key: item[key] for key in ('score', 'price', 'quote_time')}}
                             for run in runs for item in run['signal']['ranking']]),
            }
            for name, (fields, rows) in tables.items():
                temporary = self.directory / f'{name}.csv.tmp'
                with temporary.open('w', encoding='utf-8', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.directory / f'{name}.csv')
