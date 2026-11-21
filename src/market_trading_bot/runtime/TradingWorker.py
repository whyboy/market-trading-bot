"""由 Web 服务管理的后台交易线程，暂不包含定时调度。"""
import fcntl
import os
import threading
import uuid
from .RuntimeStore import RuntimeStore
from datetime import datetime, timezone
from pathlib import Path


class TradingWorker:
    """线程自行更新心跳；文件锁避免多实例重复启动。"""

    def __init__(self, root: Path):
        self.store = RuntimeStore(root)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='trading-worker', daemon=True)
        self.lock = None
        self.status = {'run_id': str(uuid.uuid4()), 'pid': os.getpid(),
                       'started_at': datetime.now(timezone.utc).isoformat(), 'state': 'idle',
                       'last_execution': None, 'scheduler_enabled': False}

    def start(self):
        self.store.database.parent.mkdir(parents=True, exist_ok=True)
        self.lock = (self.store.database.parent / 'worker.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RuntimeError('已有交易线程运行，请只启动一个后端实例') from None
        try:
            self._heartbeat()
            self.thread.start()
        except BaseException:
            self.lock.close()
            self.lock = None
            raise

    def stop(self):
        self.stop_event.set()
        self.thread.join()
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def _heartbeat(self):
        self.store.write({**self.status, 'heartbeat_at': datetime.now(timezone.utc).isoformat()})

    def _run(self):
        try:
            while not self.stop_event.wait(5):
                self._heartbeat()
        finally:
            self.status['state'] = 'stopped'
            self._heartbeat()
