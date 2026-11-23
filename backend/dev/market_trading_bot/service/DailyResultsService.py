"""每日交易结果与估值的数据访问服务。"""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from market_trading_bot.repository.DailyResultsRepository import DailyResultsRepository


class DailyResultsService:
    def __init__(self, directory, instance_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', instance_id):
            raise ValueError('实例ID只能包含字母、数字、下划线及连字符')
        self.database = directory / instance_id / 'ledger.sqlite3'

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
    def get_all_daily_results(directory, instance_id):
        """只读查询账户每日结果表的所有记录。"""
        if not re.fullmatch(r'[A-Za-z0-9_-]+', instance_id):
            raise ValueError('实例ID只能包含字母、数字、下划线及连字符')
        database = directory / instance_id / 'ledger.sqlite3'
        if not database.exists():
            return []
        with closing(sqlite3.connect(f'{database.resolve().as_uri()}?mode=ro', uri=True)) as connection:
            if not DailyResultsRepository.table_exists(connection).fetchone():
                return []
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in DailyResultsRepository.all_daily_results(connection)]

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

