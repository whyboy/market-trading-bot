"""保存每日收盘估值，用于收益比较；不保存推送消息或发送状态。"""
import json
import sqlite3
from contextlib import closing


class DailyValuationRepository:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.database = directory / 'daily_valuations.sqlite3'
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS valuations (
                instance_id TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (instance_id, day))''')

    def latest(self, instance_id, day):
        with closing(sqlite3.connect(self.database, timeout=10)) as connection:
            row = connection.execute('''SELECT payload FROM valuations
                WHERE instance_id=? AND day<=? ORDER BY day DESC LIMIT 1''', (instance_id, day)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, instance_id, snapshot):
        with closing(sqlite3.connect(self.database, timeout=10)) as connection, connection:
            connection.execute('INSERT OR IGNORE INTO valuations VALUES (?, ?, ?)',
                               (instance_id, snapshot['date'], json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))
        return self.latest(instance_id, snapshot['date'])
