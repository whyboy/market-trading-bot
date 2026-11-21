"""由 TradingScheduler 管理的策略任务，每个任务拥有独立线程。"""
import fcntl
import json
import os
import sched
import threading
import uuid
from .RuntimeStore import RuntimeStore
from datetime import datetime, time, timedelta, timezone
from market_trading_bot.AKShareMarket import AKShareMarket, SHANGHAI
from market_trading_bot.StrategyLogger import strategy_logging
from pathlib import Path
from queue import Empty, Queue


class TradingTask:
    """同一线程串行执行交易与心跳；文件锁避免重复启动。"""

    def __init__(self, root: Path, strategy_path: Path):
        self.root = root
        config = json.loads(strategy_path.read_text())
        self.strategy_id = config['strategy']
        self.settings = json.loads((root / 'config/global.json').read_text())
        self.timer = sched.scheduler(timefunc=lambda: self.now().timestamp())
        self.error = None
        self.jobs = [{'path': strategy_path, 'strategy': self.strategy_id,
                      'instance_id': instance['id'], 'time': time.fromisoformat(config['schedule']['trade_time']),
                      'next_execution_at': None, 'last_execution': None}
                     for instance in config.get('instances', []) if instance['enabled']]
        self.store = RuntimeStore(root, self.strategy_id)
        self.commands = Queue()
        self.ready = threading.Event()
        timeout = self.settings.get('runtime', {}).get('trade_timeout_seconds', 300)
        if not isinstance(timeout, (int, float)) or not 0 < timeout < float('inf'):
            raise ValueError('交易超时时间必须为有限正数')
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f'trading-task-{self.strategy_id}', daemon=True)
        self.lock = None
        self.status = {'strategy': self.strategy_id, 'run_id': str(uuid.uuid4()), 'pid': os.getpid(),
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
        self.commands.put(None)
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
        self.commands.put((strategy_path, instance_id))

    def _execute_trade(self, strategy_path: Path, instance_id: str, scheduled_at=None, logger=None):
        from market_trading_bot.runner import run_pap
        execution = {'instance_id': instance_id, 'started_at': datetime.now(timezone.utc).isoformat()}
        self.status.update(state='running', current_execution=execution)
        self._heartbeat()
        try:
            if scheduled_at is not None and not self.load_calendar_with_retry(scheduled_at, self.stop_event, logger):
                self.status.update(state='idle', current_execution=None)
                self._heartbeat()
                return None
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
            self.register_jobs(self._execute_trade, self.stop_event)
            self.status.update(self.snapshot())
            self._heartbeat()
            self.ready.set()
            while not self.stop_event.is_set():
                self.run_pending()
                self.status.update(self.snapshot())
                self._heartbeat()
                try:
                    task = self.commands.get(timeout=5)
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

    @staticmethod
    def now():
        return datetime.now(SHANGHAI)

    def register_jobs(self, execute, stop_event):
        """启动时注册每日任务；已经过去的时间安排到明天。"""
        for task in self.jobs:
            self._register_next(task, execute, stop_event)

    def _register_next(self, task, execute, stop_event):
        now = self.now()
        due = datetime.combine(now.date(), task['time'], SHANGHAI)
        if due <= now:
            due += timedelta(days=1)
        task['next_execution_at'] = due.isoformat()
        self.timer.enterabs(due.timestamp(), 1, self.run_strategy, (task, due, execute, stop_event))

    def run_pending(self):
        """执行已经到点的回调，不阻塞等待下个任务。"""
        self.timer.run(blocking=False)

    def run_strategy(self, task, due, execute, stop_event):
        """到点执行，并注册下一天任务；单账户失败不影响其他账户。"""
        try:
            if stop_event.is_set():
                return
            with strategy_logging(self.root, task['strategy'], self.settings['logging']) as logger:
                logger.info('定时任务开始 instance=%s', task['instance_id'])
                try:
                    result = execute(task['path'], task['instance_id'], due, logger)
                except Exception as exc:
                    result = {'success': False, 'error': str(exc), 'finished_at': self.now().isoformat()}
                if result is not None:
                    task['last_execution'] = {**result, 'scheduled_at': due.isoformat(),
                                              'state': 'success' if result['success'] else 'failed'}
                    logger.info('定时任务结束 instance=%s state=%s', task['instance_id'], task['last_execution']['state'])
                else:
                    logger.info('定时任务结束 instance=%s，本次不执行交易', task['instance_id'])
        finally:
            if not stop_event.is_set():
                self._register_next(task, execute, stop_event)

    def load_calendar_with_retry(self, due, stop_event, logger=None):
        """日历成功加载后判断交易日；失败等待5秒，休市或跨日则结束。"""
        end = datetime.combine(due.date(), time(11, 30) if due.time() < time(12) else time(15), SHANGHAI)
        market = AKShareMarket(self.settings['market'])
        while not stop_event.is_set() and self.now().date() == due.date() and self.now() < end:
            try:
                calendar = market.trading_calendar()
                now = self.now()
                if not calendar or not calendar[0] <= now.date() <= calendar[-1]:
                    raise ValueError('交易日历未覆盖当前日期')
                self.error = None
                return not stop_event.is_set() and now.date() == due.date() and now < end and now.date() in calendar
            except Exception as exc:
                self.error = str(exc)
                if logger is not None:
                    logger.warning('交易日历加载失败，5秒后重试：%s', exc)
                if stop_event.wait(5):
                    break
        return False

    def snapshot(self):
        return {'scheduler_enabled': bool(self.jobs), 'scheduler_error': self.error,
                'scheduled_tasks': [{'strategy': task['strategy'], 'instance_id': task['instance_id'],
                                     'next_execution_at': task['next_execution_at'],
                                     'last_execution': task['last_execution']} for task in self.jobs]}
