"""交易日定时调仓：同一线程检查、领取并执行，错过窗口不补单。"""
import json
import re
from .RuntimeStore import RuntimeStore
from datetime import datetime, time, timedelta
from market_trading_bot.AKShareMarket import AKShareMarket, SHANGHAI
from market_trading_bot.StrategyLogger import strategy_logging
from pathlib import Path


class TradeScheduler:
    def __init__(self, root: Path):
        self.root = root
        self.store = RuntimeStore(root)
        self.settings = json.loads((root / 'config/global.json').read_text())
        if self.settings.get('timezone', 'Asia/Shanghai') != 'Asia/Shanghai':
            raise ValueError('调度时区必须为Asia/Shanghai')
        self.tasks = []
        ids = set()
        for path in sorted((root / 'config').glob('*_strategy.json')):
            config = json.loads(path.read_text())
            instances = [item for item in config.get('instances', []) if item['enabled'] is True]
            if not instances:
                continue
            schedule = config.get('schedule')
            if not schedule:
                raise ValueError(f'{path.name}: 启用的策略必须配置schedule')
            if not re.fullmatch(r'\d{2}:\d{2}', schedule['trade_time']):
                raise ValueError('调仓时间格式必须为HH:MM')
            trade_time = time.fromisoformat(schedule['trade_time'])
            if trade_time.tzinfo or trade_time.second or trade_time.microsecond or not (
                    time(9, 30) <= trade_time < time(11, 30) or time(13) <= trade_time < time(15)):
                raise ValueError('调仓时间必须为北京时间A股连续交易时段内的HH:MM')
            window = schedule.get('trade_window_seconds', 60)
            if type(window) is not int or not 1 <= window <= 300:
                raise ValueError('调仓触发窗口必须为1至300秒')
            for instance in instances:
                if instance['mode'] != 'pap' or config['strategy'] != 'etf_rotation':
                    raise ValueError('定时执行当前仅支持etf_rotation模拟账户')
                if not re.fullmatch(r'[A-Za-z0-9_-]+', instance['id']) or instance['id'] in ids:
                    raise ValueError('账户实例ID必须合法且全局唯一')
                ids.add(instance['id'])
                self.tasks.append({'path': path, 'strategy': config['strategy'],
                                   'instance_id': instance['id'], 'time': trade_time, 'window': window})
        self.calendar = []
        self.calendar_day = None
        self.retry_at = None
        self.error = None
        self.recovered = False

    @staticmethod
    def now():
        return datetime.now(SHANGHAI)

    def _refresh_calendar(self, now):
        if self.calendar_day == now.date() or self.retry_at and now < self.retry_at:
            return
        try:
            calendar = AKShareMarket(self.settings['market']).trading_calendar()
            if not calendar or not calendar[0] <= now.date() <= calendar[-1]:
                raise ValueError('交易日历未覆盖当前日期，暂停调度')
            self.calendar = calendar
            self.calendar_day = now.date()
            self.error = None
        except Exception as exc:
            self.error = str(exc)
            self.retry_at = now + timedelta(seconds=60)

    def tick(self, execute, stop_event):
        """每次执行前重新检查时间；一项失败不影响其他账户。"""
        if not self.tasks:
            return
        if not self.recovered:
            for instance_id, result in self.store.schedule_results().items():
                if result['state'] == 'running':
                    self.store.finish_schedule(result['date'], instance_id,
                                               {**result, 'state': 'interrupted', 'error': '上次执行中断，不自动重试'})
            self.recovered = True
        self._refresh_calendar(self.now())
        if self.error:
            return
        for task in self.tasks:
            if stop_event.is_set():
                break
            now = self.now()
            if self.calendar_day != now.date() or now.date() not in self.calendar:
                continue
            due = datetime.combine(now.date(), task['time'], SHANGHAI)
            if now < due:
                continue
            end = min(due + timedelta(seconds=task['window']), datetime.combine(
                now.date(), time(11, 30) if task['time'] < time(12) else time(15), SHANGHAI))
            result = {'date': now.date().isoformat(), 'instance_id': task['instance_id'],
                      'scheduled_at': due.isoformat(), 'state': 'missed' if now >= end else 'running'}
            if not self.store.claim_schedule(result['date'], task['instance_id'], result):
                continue
            with strategy_logging(self.root, task['strategy'], self.settings['logging']) as logger:
                if result['state'] == 'missed':
                    logger.warning('错过调仓窗口 instance=%s scheduled_at=%s，不补单', task['instance_id'], due.isoformat())
                    continue
                logger.info('定时调仓开始 instance=%s scheduled_at=%s', task['instance_id'], due.isoformat())
                try:
                    execution = execute(task['path'], task['instance_id'])
                    result.update(execution, state='success' if execution['success'] else 'failed')
                except Exception as exc:
                    result.update(state='failed', success=False, error=str(exc), finished_at=self.now().isoformat())
                self.store.finish_schedule(result['date'], task['instance_id'], result)
                logger.info('定时调仓结束 instance=%s state=%s', task['instance_id'], result['state'])

    def snapshot(self):
        now = self.now()
        results = self.store.schedule_results()
        tasks = []
        for task in self.tasks:
            last = results.get(task['instance_id'])
            next_at = None
            if not self.error and self.calendar_day == now.date():
                for day in self.calendar:
                    due = datetime.combine(day, task['time'], SHANGHAI)
                    end = due + timedelta(seconds=task['window'])
                    if end > now and not (last and last['date'] == day.isoformat()):
                        next_at = due.isoformat()
                        break
            tasks.append({'strategy': task['strategy'], 'instance_id': task['instance_id'],
                          'next_execution_at': next_at, 'last_execution': last})
        return {'scheduler_enabled': bool(self.tasks), 'scheduler_error': self.error, 'scheduled_tasks': tasks}
