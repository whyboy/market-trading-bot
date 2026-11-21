"""统一注册和管理交易、收盘估值、日报任务及服务心跳。"""
import fcntl
import json
import logging
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, time, timezone
from market_trading_bot.log.StrategyLogger import strategy_logging
from market_trading_bot.market.AKShareMarket import AKShareMarket, SHANGHAI
from market_trading_bot.notification.PushPlusNotifier import PushPlusNotifier
from market_trading_bot.repository.PapAccountRepository import PapAccountRepository
from market_trading_bot.repository.TaskStatusRepository import TaskStatusRepository
from market_trading_bot.strategy.ETFRotationStrategyImpl import ETFRotationStrategyImpl
from market_trading_bot.task.DailySummaryTask import DailySummaryTask
from market_trading_bot.task.DailyValuationTask import DailyValuationTask
from market_trading_bot.task.TradingTask import TradingTask
from market_trading_bot.trader.PapTrader import PapTrader
from pathlib import Path


class TaskScheduler:
    """统一管理交易、估值、日报任务和服务心跳。"""

    def __init__(self, root: Path):
        """准备调度器；root 为 backend/dev 目录。"""
        self.root = root
        self.tasks = {}  # (策略ID, 账户ID) -> 最近一次任务（可能已完成）
        self.configs = {}
        self.calendar_day = None
        self.prepared_day = None
        self.is_trading_day = None
        self.calendar_error = None
        self.calendar_lock = threading.RLock()
        self.calendar = ()
        self.calendar_loaded_day = None
        self.summary_task = None
        self.valuation_task = None
        self.valuation_day = None
        self.valuation_lock = threading.Lock()
        self.daily_context = None
        self.summary_lock = threading.Lock()
        self.reported_day = None
        self.timer = BackgroundScheduler(timezone=SHANGHAI)
        self.stop_event = threading.Event()
        self.tasks_lock = threading.Lock()
        self.lock_file = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.heartbeat_at = self.started_at

    def start(self):
        """取得部署锁并注册定时任务；晚启动补做当天日历、估值和日报。"""
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
            self.notifier = PushPlusNotifier(self.settings)
            for path in sorted((self.root / 'market_trading_bot/config').glob('*_strategy.json')):
                config = json.loads(path.read_text())
                if config.get('enable', True):
                    strategy_id = config['strategy']
                    self.configs[strategy_id] = config
            # 每天北京时间 15:05 更新收盘估值。
            self.timer.add_job(self._start_valuation, 'cron', hour=15, minute=5, id='_daily_valuation',
                               coalesce=True, max_instances=1, misfire_grace_time=None)
            # 每天北京时间 15:10 汇总并推送日报。
            self.timer.add_job(self._start_summary, 'cron', hour=15, minute=10, id='_daily_report',
                               coalesce=True, max_instances=1, misfire_grace_time=None)
            # 每天北京时间 9:00 确认交易日并安排交易。
            self.timer.add_job(self.prepare_day, 'cron', hour=9, minute=0, id='_calendar',
                               coalesce=True, max_instances=1, misfire_grace_time=300)
            # 每 5 秒更新服务心跳。
            self.timer.add_job(self._heartbeat, 'interval', seconds=5, id='_heartbeat')
            self.timer.start()
            if self.now().time() >= time(9):
                self.timer.add_job(self.prepare_day, id='_startup_calendar')
            if self.now().time() >= time(15, 5):
                self.timer.add_job(self._recover_daily, id='_startup_daily_tasks')
        except BaseException:
            self.stop()
            raise

    def prepare_day(self):
        """确认交易日并安排当天尚未到点的交易；失败每 5 秒重试至 15:00。"""
        if not self.configs or not self.calendar_lock.acquire(blocking=False):
            return
        day = self.now().date()
        try:
            if self.prepared_day == day:
                return
            market = AKShareMarket(self.settings['market'])
            while not self.stop_event.is_set() and self.now().date() == day and self.now().time() < time(15):
                try:
                    context = self._load_calendar(day, market)
                    if self.stop_event.is_set() or self.now().date() != day:
                        return
                    self.prepared_day = day
                    self.calendar_day = day
                    self.is_trading_day = context['is_trading_day']
                    if self.is_trading_day:
                        for strategy_id, config in self.configs.items():
                            due = datetime.combine(day, time.fromisoformat(config['schedule']['trade_time']), SHANGHAI)
                            if due > self.now():
                                self.timer.add_job(
                                    self._start_trades, 'date', run_date=due, args=[strategy_id, day],
                                    id=strategy_id, replace_existing=True, misfire_grace_time=5)
                    return
                except Exception as exc:
                    self.calendar_error = str(exc)
                    logging.getLogger(__name__).warning('交易日历加载失败，5秒后重试：%s', exc)
                    if self.stop_event.wait(5):
                        return
        finally:
            self.calendar_lock.release()

    def _recover_daily(self):
        """晚启动先补估值；已过15:10时等待补估值结束再发送当天日报。"""
        self._start_valuation()
        if self.now().time() >= time(15, 10):
            if self.valuation_task is not None:
                self.valuation_task.thread.join()
            self._start_summary()

    def _start_valuation(self):
        """每天启动一次收盘估值，前一天任务未结束时不重叠执行。"""
        if not self.valuation_lock.acquire(blocking=False):
            return
        try:
            day = self.now().date()
            if self.stop_event.is_set() or self.now().time() < time(15, 5) or self.valuation_day == day:
                return
            if self.valuation_task is not None and self.valuation_task.thread.is_alive():
                return
            context = self._daily_calendar(day)
            if self.stop_event.is_set() or self.now().date() != day:
                return
            task = DailyValuationTask(self.root, self.settings, self.configs, context,
                                      self.stop_event, self._is_trading, self.now)
            self.valuation_task = task
            task.start()
            self.valuation_day = day
        finally:
            self.valuation_lock.release()

    def _start_summary(self):
        """每天启动一次日报，前一天任务未结束时不重叠执行。"""
        if not self.summary_lock.acquire(blocking=False):
            return
        try:
            day = self.now().date()
            if self.stop_event.is_set() or self.now().time() < time(15, 10) or self.reported_day == day:
                return
            if self.summary_task is not None and self.summary_task.thread.is_alive():
                return
            context = self._daily_calendar(day)
            if self.stop_event.is_set() or self.now().date() != day:
                return
            task = DailySummaryTask(self.root, self.settings, self.configs, self.notifier, context,
                                    self.stop_event, self.now)
            self.summary_task = task
            task.start()
            self.reported_day = day
        finally:
            self.summary_lock.release()

    def _daily_calendar(self, day):
        """获取当天日历，最多尝试 3 次；缓存结果或失败提示。"""
        if self.daily_context is not None and self.daily_context['day'] == day:
            return self.daily_context
        market = AKShareMarket(self.settings['market'])
        for attempt in range(3):
            try:
                context = self._load_calendar(day, market)
                break
            except Exception as exc:
                self.calendar_error = str(exc)
                logging.getLogger(__name__).warning('每日任务日历加载失败：%s', exc)
                if attempt == 2 or self.stop_event.wait(30):
                    context = {'day': day, 'notice': '交易日历获取失败，未生成收益估值，请查看日志。'}
                    break
        if 'notice' not in context:
            self.calendar_day = day
            self.is_trading_day = context['is_trading_day']
        self.daily_context = context
        return context

    def _load_calendar(self, day, market):
        """缓存交易日历，返回当天是否交易及上一交易日。"""
        with self.calendar_lock:
            if self.calendar_loaded_day != day:
                calendar = market.trading_calendar()
                if not calendar or not calendar[0] < day <= calendar[-1]:
                    raise ValueError('交易日历未覆盖当前日期及上一交易日')
                self.calendar = tuple(calendar)
                self.calendar_loaded_day = day
            self.calendar_error = None
            return {'day': day, 'is_trading_day': day in self.calendar,
                    'previous_trading_day': max(value for value in self.calendar if value < day)}

    def _is_trading(self, strategy_id, instance_id):
        """判断指定策略账户的交易线程是否仍在运行。"""
        with self.tasks_lock:
            task = self.tasks.get((strategy_id, instance_id))
        return task is not None and task.thread.is_alive()

    def _start_trades(self, strategy_id, day):
        """为启用账户启动独立交易任务，跳过仍在运行的账户。"""
        with self.tasks_lock:
            if self.stop_event.is_set() or not self.is_trading_day or day != self.now().date() or self.calendar_day != day:
                return
            config = self.configs[strategy_id]
            scheduled_at = datetime.combine(day, time.fromisoformat(config['schedule']['trade_time']), SHANGHAI)
            for instance in config.get('instances', []):
                if not instance['enabled']:
                    continue
                key = (strategy_id, instance['id'])
                previous = self.tasks.get(key)
                if previous is not None and previous.thread.is_alive():
                    continue
                try:
                    task = self._build_trade_task(strategy_id, instance, scheduled_at)
                    task.start()
                    self.tasks[key] = task
                except Exception:
                    logging.getLogger(__name__).exception('创建任务失败 strategy=%s instance=%s', strategy_id, instance['id'])

    def _build_trade_task(self, strategy_id, instance, scheduled_at):
        """为一个策略账户装配独立的交易依赖。"""
        config = self.configs[strategy_id]
        if instance['mode'] != 'pap' or config['strategy'] != 'etf_rotation':
            raise ValueError('当前仅支持ETF轮动模拟账户')
        data_dir = self.root / self.settings['storage']['data_dir']
        return TradingTask(
            strategy=ETFRotationStrategyImpl(config['parameters']),
            market=AKShareMarket(self.settings['market']),
            executor=PapTrader(instance['execution'], config['instruments']),
            account_store=PapAccountRepository(data_dir, instance['id'], strategy_id),
            status_store=TaskStatusRepository(data_dir, strategy_id, instance['id']),
            strategy_id=strategy_id, instance_id=instance['id'], initial_cash=instance['initial_cash'],
            scheduled_at=scheduled_at, notifier=self.notifier,
            trade_timeout_seconds=self.settings.get('runtime', {}).get('trade_timeout_seconds', 300),
            log_context=strategy_logging(self.root, strategy_id, self.settings['logging']))

    def stop(self):
        """通知任务退出，等待执行结束后释放部署锁。"""
        with self.tasks_lock:
            self.stop_event.set()
            for task in self.tasks.values():
                task.stop_event.set()
        if self.timer.running:
            self.timer.shutdown(wait=True)
        for task in self.tasks.values():
            task.stop()
        for task in (self.valuation_task, self.summary_task):
            if task is not None:
                task.stop()
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None

    def get_status(self, strategy_id: str, instance_id: str | None = None):
        """查询账户或策略状态；任一交易超时均标记异常。"""
        with self.tasks_lock:
            task_map = dict(self.tasks)
        tasks = [task for (strategy, instance), task in task_map.items()
                 if strategy == strategy_id and (instance_id is None or instance == instance_id)]
        active_tasks = [task for task in tasks if task.thread.is_alive()]
        if active_tasks:
            task = min(active_tasks, key=lambda task: (
                task.status['current_execution'] or task.status['last_execution'] or {}
            ).get('started_at', self.started_at))
        else:
            task = max(tasks, key=lambda task: (task.result or {}).get('finished_at', ''), default=None)
        status = dict(task.status) if task else {}
        now = datetime.now(timezone.utc)
        active_statuses = [dict(value.status) for value in active_tasks]
        timed_out = any((now - datetime.fromisoformat(value['current_execution']['started_at'])).total_seconds()
                        > value['trade_timeout_seconds'] for value in active_statuses if value.get('current_execution'))
        healthy = self.timer.running and (now - datetime.fromisoformat(self.heartbeat_at)).total_seconds() <= 20
        job = self.timer.get_job(strategy_id)
        config = self.configs.get(strategy_id, {})
        return {**status, 'strategy': strategy_id, 'state': 'running' if active_tasks else 'idle',
                'online': healthy and not timed_out,
                'health': 'trade_timeout' if timed_out else 'idle' if healthy else 'stopped',
                'heartbeat_scope': 'task' if active_tasks else 'scheduler',
                'heartbeat_at': status.get('heartbeat_at') if active_tasks else self.heartbeat_at,
                'started_at': self.started_at,
                'uptime_seconds': (now - datetime.fromisoformat(self.started_at)).total_seconds(),
                'scheduler_enabled': strategy_id in self.configs, 'scheduler_error': self.calendar_error,
                'calendar_day': self.calendar_day.isoformat() if self.calendar_day else None,
                'is_trading_day': self.is_trading_day if self.calendar_day == self.now().date() else None,
                'scheduled_tasks': [{'strategy': strategy_id, 'instance_id': instance['id'],
                                     'next_execution_at': job.next_run_time.isoformat() if job and job.next_run_time else None,
                                     'last_execution': task_map[(strategy_id, instance['id'])].result
                                     if (strategy_id, instance['id']) in task_map else None}
                                    for instance in config.get('instances', []) if instance['enabled']]}

    def _heartbeat(self):
        """更新调度服务心跳。"""
        self.heartbeat_at = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def now():
        """返回北京时间，统一交易日期与策略执行时间的判断口径。"""
        return datetime.now(SHANGHAI)
