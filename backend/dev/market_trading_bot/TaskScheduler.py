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
from market_trading_bot.service.AccountService import AccountService
from market_trading_bot.strategy.ETFRotationStrategyImpl import ETFRotationStrategyImpl
from market_trading_bot.task.DailySummaryTask import DailySummaryTask
from market_trading_bot.task.DailyValuationTask import DailyValuationTask
from market_trading_bot.task.TradingTask import TradingTask
from market_trading_bot.trading.TradeExecutor import TradeExecutor
from pathlib import Path


class TaskScheduler:
    """统一管理交易、估值、日报任务和服务心跳。"""

    def __init__(self, root: Path):
        """准备调度器；root 为 backend/dev 目录。"""
        self.root = root
        self.tasks = {}  # (策略ID, 账户ID) -> 最近一次任务（可能已完成）
        self.configs = {}
        self.calendar_error = None
        self.calendar_lock = threading.RLock()
        self.summary_task = None
        self.valuation_task = None
        self.valuation_day = None
        self.valuation_lock = threading.Lock()
        # 已准备日历的结构（日期均为 date）：
        # {
        #     'day': today,
        #     'is_trading_day': True,  # 或 False
        #     'previous_trading_day': 上一个交易日,
        # }
        self.calendar_context = None
        self.summary_lock = threading.Lock()
        self.reported_day = None
        self.timer = BackgroundScheduler(timezone=SHANGHAI)
        self.stop_event = threading.Event()
        self.tasks_lock = threading.Lock()
        self.lock_file = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.heartbeat_at = self.started_at

    @staticmethod
    def now():
        """返回北京时间，统一交易日期与策略执行时间的判断口径。"""
        return datetime.now(SHANGHAI)

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
            for strategy_id, config in self.configs.items():
                trade_time = time.fromisoformat(config['schedule']['trade_time'])
                self.timer.add_job(self._start_trades, 'cron', hour=trade_time.hour, minute=trade_time.minute,
                                   second=trade_time.second, args=[strategy_id], id=strategy_id,
                                   coalesce=True, max_instances=1, misfire_grace_time=5)
            # 每天北京时间 9:00 确认交易日。
            self.timer.add_job(self.prepare_calender_context, 'cron', hour=9, minute=0, id='_calendar',
                               coalesce=True, max_instances=1, misfire_grace_time=300)
            # 每天北京时间 15:05 更新收盘估值。
            self.timer.add_job(self._start_valuation, 'cron', hour=15, minute=5, id='_daily_valuation',
                               coalesce=True, max_instances=1, misfire_grace_time=None)
            # 每天北京时间 15:10 汇总并推送日报。
            self.timer.add_job(self._start_summary, 'cron', hour=15, minute=10, id='_daily_report',
                               coalesce=True, max_instances=1, misfire_grace_time=None)
            # 每 5 秒更新服务心跳。
            self.timer.add_job(self._heartbeat, 'interval', seconds=5, id='_heartbeat')
            self.timer.start()
            # 启动时先准备当天日历；晚于收盘估值时间启动时补做估值和日报。
            self.prepare_calender_context()
            if self.now().time() >= time(15, 5):
                self.timer.add_job(self._recover_daily_tasks, args=[self.now().date()], id='_startup_daily_tasks')
        except BaseException:
            self.stop()
            raise

    def prepare_calender_context(self):
        """获取当天交易日历并保存状态；收盘前每 5 秒重试，收盘后最多尝试 3 次。"""
        if not self.configs or not self.calendar_lock.acquire(blocking=False):
            return
        try:
            today = self.now().date()
            if self.calendar_context is not None and self.calendar_context['day'] == today:
                return
            market = AKShareMarket(self.settings['market'])
            attempts = 0
            started_after_close = self.now().time() >= time(15)
            while not self.stop_event.is_set() and self.now().date() == today:
                try:
                    context = self._load_calendar(today, market)
                except Exception as exc:
                    attempts += 1
                    self.calendar_error = str(exc)
                    logging.getLogger(__name__).error('交易日历加载失败：%s', exc)
                    if (started_after_close and attempts >= 3) or self.stop_event.wait(5):
                        return
                    if not started_after_close and self.now().time() >= time(15):
                        return
                    continue
                if self.stop_event.is_set() or self.now().date() != today:
                    return
                self.calendar_context = context
                if not context['is_trading_day']:
                    logging.getLogger(__name__).info('%s 非交易日，跳过交易、估值和日报', today)
                return
        finally:
            self.calendar_lock.release()

    def _load_calendar(self, day, market):
        """读取交易日历，返回当天是否交易及上一交易日。"""
        calendar = market.trading_calendar()
        if not calendar or not calendar[0] < day <= calendar[-1]:
            raise ValueError('交易日历未覆盖当前日期及上一交易日')
        self.calendar_error = None
        return {'day': day, 'is_trading_day': day in calendar,
                'previous_trading_day': max(value for value in calendar if value < day)}

    def _start_trades(self, strategy_id, day=None):
        """为启用账户启动独立交易任务，跳过仍在运行的账户。"""
        today = self.now().date()
        if self.stop_event.is_set() or (day is not None and day != today) or self._trading_day_context(today) is None:
            return
        with self.tasks_lock:
            if self.stop_event.is_set() or self.now().date() != today:
                return
            config = self.configs[strategy_id]
            scheduled_at = datetime.combine(today, time.fromisoformat(config['schedule']['trade_time']), SHANGHAI)
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
            executor=TradeExecutor(instance['execution'], config['instruments']),
            account_store=AccountService(data_dir, instance['id'], strategy_id),
            strategy_id=strategy_id, instance_id=instance['id'], initial_cash=instance['initial_cash'],
            scheduled_at=scheduled_at, notifier=self.notifier,
            trade_timeout_seconds=self.settings.get('runtime', {}).get('trade_timeout_seconds', 300),
            log_context=strategy_logging(self.root, strategy_id, self.settings['logging']))

    def _recover_daily_tasks(self, scheduled_day=None):
        """晚启动先补估值；已过15:10时等待补估值结束再发送当天日报。"""
        if scheduled_day is not None and scheduled_day != self.now().date():
            return
        self._start_valuation()
        if self.now().time() >= time(15, 10):
            if self.valuation_task is not None:
                self.valuation_task.thread.join()
            self._start_summary()

    def _start_valuation(self, scheduled_day=None):
        """每天启动一次收盘估值，前一天任务未结束时不重叠执行。"""
        if not self.valuation_lock.acquire(blocking=False):
            return
        try:
            day = self.now().date()
            if (self.stop_event.is_set() or (scheduled_day is not None and scheduled_day != day)
                    or self.now().time() < time(15, 5) or self.valuation_day == day):
                return
            if self.valuation_task is not None and self.valuation_task.thread.is_alive():
                return
            context = self._trading_day_context(day)
            if (self.stop_event.is_set() or self.now().date() != day
                    or context is None):
                return
            task = DailyValuationTask(self.root, self.settings, self.configs, context,
                                      self.stop_event, self._is_trading, self.now)
            self.valuation_task = task
            task.start()
            self.valuation_day = day
        finally:
            self.valuation_lock.release()

    def _start_summary(self, scheduled_day=None):
        """每天启动一次日报，前一天任务未结束时不重叠执行。"""
        if not self.summary_lock.acquire(blocking=False):
            return
        try:
            day = self.now().date()
            if (self.stop_event.is_set() or (scheduled_day is not None and scheduled_day != day)
                    or self.now().time() < time(15, 10) or self.reported_day == day):
                return
            if self.summary_task is not None and self.summary_task.thread.is_alive():
                return
            context = self._trading_day_context(day)
            if (self.stop_event.is_set() or self.now().date() != day
                    or context is None):
                return
            task = DailySummaryTask(self.root, self.settings, self.configs, self.notifier, context,
                                    self.stop_event, self.now)
            self.summary_task = task
            task.start()
            self.reported_day = day
        finally:
            self.summary_lock.release()

    def _trading_day_context(self, day):
        """仅允许执行使用当天已准备完成的交易日日历。"""
        context = self.calendar_context
        if context is None or context['day'] != day:
            logging.getLogger(__name__).warning('%s 交易日历未准备成功，跳过任务', day)
            return None
        if not context['is_trading_day']:
            logging.getLogger(__name__).info('%s 非交易日，跳过任务', day)
            return None
        return context

    def _is_trading(self, strategy_id, instance_id):
        """判断指定策略账户的交易线程是否仍在运行。"""
        with self.tasks_lock:
            task = self.tasks.get((strategy_id, instance_id))
        return task is not None and task.thread.is_alive()

    def _heartbeat(self):
        """更新调度服务心跳。"""
        self.heartbeat_at = datetime.now(timezone.utc).isoformat()

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
        context = self.calendar_context
        return {**status, 'strategy': strategy_id, 'state': 'running' if active_tasks else 'idle',
                'online': healthy and not timed_out,
                'health': 'trade_timeout' if timed_out else 'idle' if healthy else 'stopped',
                'heartbeat_scope': 'task' if active_tasks else 'scheduler',
                'heartbeat_at': status.get('heartbeat_at') if active_tasks else self.heartbeat_at,
                'started_at': self.started_at,
                'uptime_seconds': (now - datetime.fromisoformat(self.started_at)).total_seconds(),
                'scheduler_enabled': strategy_id in self.configs, 'scheduler_error': self.calendar_error,
                'calendar_day': context['day'].isoformat() if context else None,
                'is_trading_day': context['is_trading_day'] if context and context['day'] == self.now().date() else None,
                'scheduled_tasks': [{'strategy': strategy_id, 'instance_id': instance['id'],
                                     'next_execution_at': job.next_run_time.isoformat() if job and job.next_run_time else None,
                                     'last_execution': task_map[(strategy_id, instance['id'])].result
                                     if (strategy_id, instance['id']) in task_map else None}
                                    for instance in config.get('instances', []) if instance['enabled']]}
