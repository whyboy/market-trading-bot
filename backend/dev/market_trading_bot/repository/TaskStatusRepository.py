"""独立于账户账本的交易线程心跳。"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class TaskStatusRepository:
    def __init__(self, directory: Path, strategy_id: str, instance_id: str):
        self.database = directory / 'runtime' / strategy_id / instance_id / 'runtime.sqlite3'

    def write(self, status: dict):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database, timeout=10)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS runtime (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            connection.execute('INSERT OR REPLACE INTO runtime VALUES (1, ?)', (json.dumps(status),))
