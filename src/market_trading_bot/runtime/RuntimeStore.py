"""独立于账户账本的交易线程心跳。"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class RuntimeStore:
    def __init__(self, root: Path, strategy_id: str):
        settings = json.loads((root / 'config/global.json').read_text())
        self.database = root / settings['storage']['data_dir'] / 'runtime' / strategy_id / 'runtime.sqlite3'

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
