"""每天按配置创建一次 TradingTask，定时重复由 APScheduler 负责。"""
import fcntl
import json
import logging
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, time, timezone
from market_trading_bot.market.AKShareMarket import AKShareMarket, SHANGHAI
from market_trading_bot.task.TradingTask import TradingTask
from pathlib import Path


class TradingScheduler:
    def __init__(self, root: Path):
        self.root = root
        self.tasks = {}
        self.configs = {}
        self.paths = {}
        self.calendar_day = None
        self.is_trading_day = None
        self.calendar_error = None
        self.calendar_guard = threading.Lock()
        self.jobs = {}
        self.timer = BackgroundScheduler(timezone=SHANGHAI)
        self.stop_event = threading.Event()
        self.guard = threading.Lock()
        self.lock_file = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.heartbeat_at = self.started_at

    def start(self):
        self.settings = json.loads((self.root / 'config/global.json').read_text())
        directory = self.root / self.settings['storage']['data_dir'] / 'runtime'
        directory.mkdir(parents=True, exist_ok=True)
        self.lock_file = (directory / 'scheduler.lock').open('a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError('已有调度服务运行，请只启动一个后端实例') from None
        try:
            for path in sorted((self.root / 'config').glob('*_strategy.json')):
                config = json.loads(path.read_text())
                if config.get('enable', True):
                    strategy_id = config['strategy']
                    self.configs[strategy_id] = config
                    self.paths[strategy_id] = path
            self.timer.add_job(self.prepare_day, 'cron', hour=9, minute=0, id='_calendar',
                               coalesce=True, max_instances=1, misfire_grace_time=300)
            self.timer.add_job(self._heartbeat, 'interval', seconds=5, id='_heartbeat')
            self.timer.start()
            if self.now().time() >= time(9):
                self.timer.add_job(self.prepare_day, id='_startup_calendar')
        except BaseException:
            self.stop()
            raise

    @staticmethod
    def now():
        return datetime.now(SHANGHAI)

    def prepare_day(self):
        """每天9点确认交易日；失败5秒重试，成功才注册当天策略任务。"""
        if not self.configs or not self.calendar_guard.acquire(blocking=False):
            return
        day = self.now().date()
        try:
            if self.calendar_day == day:
                return
            market = AKShareMarket(self.settings['market'])
            while not self.stop_event.is_set() and self.now().date() == day and self.now().time() < time(15):
                try:
                    calendar = market.trading_calendar()
                    if not calendar or not calendar[0] <= day <= calendar[-1]:
                        raise ValueError('交易日历未覆盖当前日期')
                    if self.stop_event.is_set() or self.now().date() != day:
                        return
                    self.calendar_day = day
                    self.is_trading_day = day in calendar
                    self.calendar_error = None
                    if self.is_trading_day:
                        for strategy_id, config in self.configs.items():
                            due = datetime.combine(day, time.fromisoformat(config['schedule']['trade_time']), SHANGHAI)
                            if due > self.now():
                                self.jobs[strategy_id] = self.timer.add_job(
                                    self._start_task, 'date', run_date=due, args=[self.paths[strategy_id], day],
                                    id=strategy_id, replace_existing=True, misfire_grace_time=5)
                    return
                except Exception as exc:
                    self.calendar_error = str(exc)
                    logging.getLogger(__name__).warning('交易日历加载失败，5秒后重试：%s', exc)
                    if self.stop_event.wait(5):
                        return
        finally:
            self.calendar_guard.release()

    def _heartbeat(self):
        self.heartbeat_at = datetime.now(timezone.utc).isoformat()

    def _start_task(self, path, day=None):
        with self.guard:
            if self.stop_event.is_set() or not self.is_trading_day or self.calendar_day != self.now().date():
                return
            if day is not None and day != self.now().date():
                return
            strategy_id = json.loads(path.read_text())['strategy']
            previous = self.tasks.get(strategy_id)
            if previous is not None and previous.thread.is_alive():
                return
            task = TradingTask(self.root, path)
            task.start()
            self.tasks[strategy_id] = task

    def stop(self):
        with self.guard:
            self.stop_event.set()
            for task in self.tasks.values():
                task.stop_event.set()
        if self.timer.running:
            self.timer.shutdown(wait=True)
        for task in self.tasks.values():
            task.stop()
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None

    def get_status(self, strategy_id: str):
        task = self.tasks.get(strategy_id)
        config = self.configs.get(strategy_id, {})
        active = task is not None and task.thread.is_alive()
        status = dict(task.status) if task is not None else {}
        execution = status.get('current_execution')
        now = datetime.now(timezone.utc)
        execution_age = (now - datetime.fromisoformat(execution['started_at'])).total_seconds() if execution else None
        timed_out = active and execution_age is not None and execution_age > status.get('trade_timeout_seconds', 300)
        healthy_scheduler = self.timer.running and (now - datetime.fromisoformat(self.heartbeat_at)).total_seconds() <= 20
        job = self.timer.get_job(strategy_id)
        results = task.results if task is not None else {}
        return {**status, 'strategy': strategy_id, 'state': 'running' if active else 'idle',
                'online': healthy_scheduler and not timed_out,
                'health': 'trade_timeout' if timed_out else 'idle' if healthy_scheduler else 'stopped',
                'heartbeat_scope': 'task' if active else 'scheduler',
                'heartbeat_at': status.get('heartbeat_at') if active else self.heartbeat_at,
                'started_at': self.started_at,
                'uptime_seconds': (now - datetime.fromisoformat(self.started_at)).total_seconds(),
                'scheduler_enabled': strategy_id in self.configs,
                'scheduler_error': self.calendar_error,
                'calendar_day': self.calendar_day.isoformat() if self.calendar_day else None,
                'is_trading_day': self.is_trading_day if self.calendar_day == self.now().date() else None,
                'scheduled_tasks': [{'strategy': strategy_id, 'instance_id': instance['id'],
                                     'next_execution_at': job.next_run_time.isoformat() if job and job.next_run_time else None,
                                     'last_execution': results.get(instance['id'])}
                                    for instance in config.get('instances', []) if instance['enabled']]}
