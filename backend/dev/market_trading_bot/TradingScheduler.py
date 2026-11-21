"""每天按配置创建一次 TradingTask，定时重复由 APScheduler 负责。"""
import fcntl
import json
import logging
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, time, timezone
from market_trading_bot.market.AKShareMarket import AKShareMarket, SHANGHAI
from market_trading_bot.TradingTask import TradingTask
from pathlib import Path


class TradingScheduler:
    """后端的统一调度入口，决定各策略何时创建并执行 TradingTask。

    由 app.py 随服务启动和关闭，使用 APScheduler 管理定时回调：
    1. 加载 enable=true（默认值）的策略配置，每天北京时间9:00确认交易日。
    2. 日历加载失败时每5秒重试，直到成功、停止服务或当天15:00。
    3. 仅为交易日中尚未到执行时间的策略注册当天任务；9:00后启动会补做判断。
    4. 到点创建一次性 TradingTask 线程；同策略不重叠，不同策略可并行执行。

    策略计算、模拟成交及账户记账由 TradingTask 协调，本类负责调度与生命周期。
    对外提供调度服务心跳、交易日判断结果、策略计划和最近任务状态，供网页监控。
    文件锁防止同一数据目录启动多个调度服务；关闭时通知并等待已创建的任务退出。
    """
    def __init__(self, root: Path):
        """准备调度器与运行状态，root为backend/dev目录；调用start才启动服务。"""
        self.root = root
        self.tasks = {}  # 策略ID -> 最近一次创建的TradingTask（可能已执行完成）
        self.configs = {}
        self.paths = {}
        self.calendar_day = None
        self.is_trading_day = None
        self.calendar_error = None
        self.calendar_guard = threading.Lock()
        self.jobs = {}  # 策略ID -> 当天注册的定时回调，与实际交易线程区分
        self.timer = BackgroundScheduler(timezone=SHANGHAI)
        self.stop_event = threading.Event()
        self.guard = threading.Lock()
        self.lock_file = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.heartbeat_at = self.started_at

    def start(self):
        """取得部署锁，加载启用策略，注册每日9:00判断及每5秒心跳并启动调度器。

        9:00后启动时立即安排一次日历判断，只注册未来的执行时间，不补执行已过期策略。
        启动失败会清理已创建的调度资源并向调用方抛出异常。
        """
        self.settings = json.loads((self.root / 'market_trading_bot/config/global.json').read_text())
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
            for path in sorted((self.root / 'market_trading_bot/config').glob('*_strategy.json')):
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
        """返回北京时间，统一交易日期与策略执行时间的判断口径。"""
        return datetime.now(SHANGHAI)

    def prepare_day(self):
        """确认当天是否交易，并为各策略注册一次性定时回调，不在此处执行策略。

        日历成功判断后当天不再重复请求；锁避免启动补判断与9:00回调同时加载。
        休市时不创建任务；请求失败等待5秒重试，停止、跨日或到15:00时结束。
        """
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
        """更新调度服务心跳；策略执行期间的任务心跳由TradingTask自行维护。"""
        self.heartbeat_at = datetime.now(timezone.utc).isoformat()

    def _start_task(self, path, day=None):
        """定时回调：确认当天交易日后创建任务线程，并拦截跨日或同策略重叠执行。"""
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
        """停止调度，通知全部任务退出，等待正在执行的工作结束后释放部署锁。"""
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
        """汇总指定策略的监控数据，不触发交易。

        等待执行时展示调度服务心跳，执行中展示任务心跳；超时仅标记异常。
        返回启动时间、运行时长、日历错误、当日计划和各账户最近执行结果。
        """
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
