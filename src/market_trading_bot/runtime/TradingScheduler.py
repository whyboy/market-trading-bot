"""使用标准库 sched 注册每日任务，在交易线程中同步执行。"""
import json
import re
import sched
from datetime import datetime, time, timedelta
from market_trading_bot.AKShareMarket import AKShareMarket, SHANGHAI
from market_trading_bot.StrategyLogger import strategy_logging
from pathlib import Path


class TradingScheduler:
    def __init__(self, root: Path):
        self.root = root
        self.settings = json.loads((root / 'config/global.json').read_text())
        if self.settings.get('timezone', 'Asia/Shanghai') != 'Asia/Shanghai':
            raise ValueError('调度时区必须为Asia/Shanghai')
        self.scheduler = sched.scheduler(timefunc=lambda: self.now().timestamp())
        self.tasks = []
        self.error = None
        ids = set()
        for path in sorted((root / 'config').glob('*_strategy.json')):
            config = json.loads(path.read_text())
            instances = [item for item in config.get('instances', []) if item['enabled'] is True]
            if not instances:
                continue
            value = config.get('schedule', {}).get('trade_time', '')
            if not re.fullmatch(r'\d{2}:\d{2}', value):
                raise ValueError('调仓时间格式必须为HH:MM')
            trade_time = time.fromisoformat(value)
            if not (time(9, 30) <= trade_time < time(11, 30) or time(13) <= trade_time < time(15)):
                raise ValueError('调仓时间必须在A股连续交易时段内')
            for instance in instances:
                if instance['mode'] != 'pap' or config['strategy'] != 'etf_rotation':
                    raise ValueError('定时执行当前仅支持etf_rotation模拟账户')
                if not re.fullmatch(r'[A-Za-z0-9_-]+', instance['id']) or instance['id'] in ids:
                    raise ValueError('账户实例ID必须合法且全局唯一')
                ids.add(instance['id'])
                self.tasks.append({'path': path, 'strategy': config['strategy'],
                                   'instance_id': instance['id'], 'time': trade_time,
                                   'next_execution_at': None, 'last_execution': None})

    @staticmethod
    def now():
        return datetime.now(SHANGHAI)

    def register_jobs(self, execute, stop_event):
        """启动时注册每日任务；已经过去的时间安排到明天。"""
        for task in self.tasks:
            self._register_next(task, execute, stop_event)

    def _register_next(self, task, execute, stop_event):
        now = self.now()
        due = datetime.combine(now.date(), task['time'], SHANGHAI)
        if due <= now:
            due += timedelta(days=1)
        task['next_execution_at'] = due.isoformat()
        self.scheduler.enterabs(due.timestamp(), 1, self.run_strategy, (task, due, execute, stop_event))

    def run_pending(self):
        """执行已经到点的回调，不阻塞等待下个任务。"""
        self.scheduler.run(blocking=False)

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
        return {'scheduler_enabled': bool(self.tasks), 'scheduler_error': self.error,
                'scheduled_tasks': [{'strategy': task['strategy'], 'instance_id': task['instance_id'],
                                     'next_execution_at': task['next_execution_at'],
                                     'last_execution': task['last_execution']} for task in self.tasks]}
