"""独立持久化执行报告，不参与交易账本事务。"""
import json
import sqlite3
import time
from contextlib import closing


class NotificationRepository:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.database = directory / 'notifications.sqlite3'
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS notifications (
                event_key TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, receipt TEXT, error TEXT)''')

    def enqueue(self, event_key, title, content):
        with closing(sqlite3.connect(self.database, timeout=10)) as connection, connection:
            connection.execute('INSERT OR IGNORE INTO notifications(event_key, title, content) VALUES (?, ?, ?)',
                               (json.dumps(event_key), title, content))

    def due(self):
        with closing(sqlite3.connect(self.database, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM notifications WHERE state='pending' AND next_attempt<=? ORDER BY rowid LIMIT 1",
                                     (time.time(),)).fetchone()
        return dict(row) if row else None

    def attempted(self, message):
        # 请求前持久化次数和延迟，重启不会重置重试预算。
        attempts = message['attempts'] + 1
        with closing(sqlite3.connect(self.database, timeout=10)) as connection, connection:
            connection.execute('UPDATE notifications SET attempts=?, next_attempt=? WHERE event_key=?',
                               (attempts, time.time() + 60 * attempts, message['event_key']))
        return attempts

    def finish(self, message, state, receipt=None, error=None):
        with closing(sqlite3.connect(self.database, timeout=10)) as connection, connection:
            connection.execute('UPDATE notifications SET state=?, receipt=?, error=? WHERE event_key=?',
                               (state, receipt, error, message['event_key']))
