"""独立于账户账本的交易线程心跳。"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class RuntimeStore:
    def __init__(self, root: Path):
        settings = json.loads((root / 'config/global.json').read_text())
        self.database = root / settings['storage']['data_dir'] / 'runtime.sqlite3'

    def write(self, status: dict):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database, timeout=10)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS runtime (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            connection.execute('INSERT OR REPLACE INTO runtime VALUES (1, ?)', (json.dumps(status),))

    def read(self) -> dict | None:
        if not self.database.exists():
            return None
        with closing(sqlite3.connect(f'{self.database.as_uri()}?mode=ro', uri=True)) as connection:
            if not connection.execute("SELECT name FROM sqlite_master WHERE name='runtime'").fetchone():
                return None
            row = connection.execute('SELECT payload FROM runtime WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None

    def claim_schedule(self, day: str, instance_id: str, result: dict) -> bool:
        """先持久化领取记录；失败、崩溃或重启都不自动重试当日任务。"""
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database, timeout=10)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS scheduled_runs '
                               '(day TEXT, instance_id TEXT, payload TEXT NOT NULL, PRIMARY KEY(day, instance_id))')
            cursor = connection.execute('INSERT OR IGNORE INTO scheduled_runs VALUES (?, ?, ?)',
                                        (day, instance_id, json.dumps(result)))
            return cursor.rowcount == 1

    def finish_schedule(self, day: str, instance_id: str, result: dict):
        with closing(sqlite3.connect(self.database, timeout=10)) as connection, connection:
            connection.execute('UPDATE scheduled_runs SET payload=? WHERE day=? AND instance_id=?',
                               (json.dumps(result), day, instance_id))

    def schedule_results(self) -> dict:
        """读取各实例最近一次调度记录。"""
        if not self.database.exists():
            return {}
        with closing(sqlite3.connect(self.database, timeout=10)) as connection:
            if not connection.execute("SELECT name FROM sqlite_master WHERE name='scheduled_runs'").fetchone():
                return {}
            rows = connection.execute('SELECT instance_id, payload FROM scheduled_runs '
                                      'WHERE (instance_id, day) IN '
                                      '(SELECT instance_id, MAX(day) FROM scheduled_runs GROUP BY instance_id)')
            return {row[0]: json.loads(row[1]) for row in rows}
