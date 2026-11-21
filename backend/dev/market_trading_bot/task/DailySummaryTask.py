"""15:10只读每日结果，汇总所有策略和账户发送一份日报。"""
import logging
import threading
from market_trading_bot.service.AccountLedger import AccountLedger


class DailySummaryTask:
    def __init__(self, root, settings, configs, notifier, context, stop_event, now):
        """创建日报线程；context 为当天日历，stop_event 与调度器共享。"""
        self.directory = root / settings['storage']['data_dir']
        self.configs = configs
        self.notifier = notifier
        self.context = context
        self.stop_event = stop_event
        self.now = now
        self.thread = threading.Thread(target=self._run, name='daily-summary-task', daemon=True)
        self.completed = False
        self.logger = logging.getLogger(__name__)

    def start(self):
        """启动日报线程。"""
        self.thread.start()

    def stop(self):
        """设置共享停止信号并等待线程退出。"""
        self.stop_event.set()
        self.thread.join()

    def _run(self):
        """执行汇总并记录完成状态。"""
        try:
            self.completed = self.run()
        except Exception:
            self.logger.exception('每日汇总任务失败')

    def run(self):
        """读取启用模拟账户并发送一份日报；停止或跨日返回 False。"""
        day = self.context['day']
        summaries = []
        for strategy_id, config in sorted(self.configs.items()):
            for instance in config.get('instances', []):
                if not instance['enabled'] or instance['mode'] != 'pap':
                    continue
                if self.stop_event.is_set() or self.now().date() != day:
                    return False
                try:
                    summary = self._read_summary(instance['id'])
                except Exception:
                    self.logger.exception('读取日报失败 strategy=%s instance=%s', strategy_id, instance['id'])
                    summary = {'notice': '读取账户收益失败，请查看日志。'}
                summaries.append({'strategy_id': strategy_id, 'instance_id': instance['id'], 'summary': summary})
        if self.stop_event.is_set() or self.now().date() != day:
            return False
        self.notifier.send_daily_report(day, summaries)
        return True

    def _read_summary(self, instance_id):
        """读取估值或失败提示；休市日使用最近成功估值。"""
        day = self.context['day']
        if self.context.get('notice'):
            return {'notice': self.context['notice']}
        account = AccountLedger.get_snapshot(self.directory, instance_id, day.isoformat())
        if not account['initialized']:
            return {'notice': '账户尚未开户，暂无持仓和收益数据。'}
        if not self.context['is_trading_day']:
            snapshot = AccountLedger.get_valuation(self.directory, instance_id, day.isoformat())
            if snapshot is None:
                return {'notice': '今日休市，暂无有效收盘估值。'}
            return {**snapshot, 'is_trading_day': False}
        result = AccountLedger.get_daily_result(self.directory, instance_id, day.isoformat())
        if result is None or result['valuation_status'] == 'pending':
            return {'notice': '当天收盘估值尚未执行或尚未完成，暂无当天收益。'}
        if result['valuation_status'] == 'running':
            return {'notice': '收盘估值仍在执行，暂无当天收益。'}
        if result['valuation_status'] == 'failed':
            return {'notice': result['valuation_error']}
        return {**result['valuation'], 'is_trading_day': True}
