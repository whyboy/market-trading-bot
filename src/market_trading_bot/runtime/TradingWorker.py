"""由 Web 服务管理的后台交易线程，按交易日定时调仓。"""
import fcntl
import json
import os
import threading
import uuid
from .RuntimeStore import RuntimeStore
from .TradeScheduler import TradeScheduler
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue


class TradingWorker:
    """同一线程串行执行交易与心跳；文件锁避免重复启动。"""

    def __init__(self, root: Path):
        self.root = root
        self.scheduler = TradeScheduler(root)
        self.store = RuntimeStore(root)
        self.tasks = Queue()
        self.ready = threading.Event()
        settings = json.loads((root / 'config/global.json').read_text())
        timeout = settings.get('runtime', {}).get('trade_timeout_seconds', 300)
        if not isinstance(timeout, (int, float)) or not 0 < timeout < float('inf'):
            raise ValueError('交易超时时间必须为有限正数')
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='trading-worker', daemon=True)
        self.lock = None
        self.status = {'run_id': str(uuid.uuid4()), 'pid': os.getpid(),
                       'started_at': datetime.now(timezone.utc).isoformat(), 'state': 'idle',
                       'last_execution': None, 'current_execution': None,
                       'trade_timeout_seconds': timeout, 'scheduler_enabled': False}

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
            self.thread.start()
            self.ready.wait()
            if not self.thread.is_alive():
                raise RuntimeError('交易线程启动失败')
        except BaseException:
            self.lock.close()
            self.lock = None
            raise

    def stop(self):
        self.stop_event.set()
        self.tasks.put(None)
        self.thread.join()
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def _heartbeat(self):
        self.store.write({**self.status, 'heartbeat_at': datetime.now(timezone.utc).isoformat()})

    def submit_trade(self, strategy_path: Path, instance_id: str):
        """内部任务入口，由后台线程执行；不提供网页交易接口。"""
        if self.stop_event.is_set() or not self.thread.is_alive():
            raise RuntimeError('交易线程未运行')
        self.tasks.put((strategy_path, instance_id))

    def _execute_trade(self, strategy_path: Path, instance_id: str):
        from market_trading_bot.runner import run_pap
        execution = {'instance_id': instance_id, 'started_at': datetime.now(timezone.utc).isoformat()}
        self.status.update(state='running', current_execution=execution)
        self._heartbeat()
        try:
            run_pap(self.root, strategy_path, instance_id)
            execution = {**execution, 'success': True}
        except Exception as exc:
            execution = {**execution, 'success': False, 'error': str(exc)}
        execution['finished_at'] = datetime.now(timezone.utc).isoformat()
        self.status.update(state='idle', current_execution=None, last_execution=execution)
        self._heartbeat()
        return execution

    def _run(self):
        try:
            self._heartbeat()
            self.ready.set()
            while not self.stop_event.is_set():
                self.scheduler.tick(self._execute_trade, self.stop_event)
                self.status.update(self.scheduler.snapshot())
                self._heartbeat()
                try:
                    task = self.tasks.get(timeout=5)
                except Empty:
                    self._heartbeat()
                    continue
                if task is None or self.stop_event.is_set():
                    break
                self._execute_trade(*task)
        finally:
            self.ready.set()
            self.status['state'] = 'stopped'
            self._heartbeat()
