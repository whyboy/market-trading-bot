"""15:10只读每日结果，汇总所有策略和账户发送一份日报。"""
import logging
import threading
from market_trading_bot.repository.PapAccountRepository import PapAccountRepository


class DailySummaryTask:
    def __init__(self, root, settings, configs, notifier, context, stop_event, now):
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
        """由调度器在15:10启动汇总线程；不获取行情、不修改账户数据库。"""
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()

    def _run(self):
        try:
            self.completed = self.run()
        except Exception:
            self.logger.exception('每日汇总任务失败')

    def run(self):
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
        self.notifier.send_daily_summary_report(day, summaries)
        return True

    def _read_summary(self, instance_id):
        day = self.context['day']
        if self.context.get('notice'):
            return {'notice': self.context['notice']}
        account = PapAccountRepository.get_account_snapshot_on_or_before(self.directory, instance_id, day.isoformat())
        if not account['initialized']:
            return {'notice': '账户尚未开户，暂无持仓和收益数据。'}
        if not self.context['is_trading_day']:
            snapshot = PapAccountRepository.find_latest_valuation_on_or_before(self.directory, instance_id, day.isoformat())
            if snapshot is None:
                return {'notice': '今日休市，暂无有效收盘估值。'}
            return {**snapshot, 'is_trading_day': False}
        result = PapAccountRepository.get_daily_result(self.directory, instance_id, day.isoformat())
        if result is None or result['valuation_status'] == 'pending':
            return {'notice': '当天收盘估值尚未执行或尚未完成，暂无当天收益。'}
        if result['valuation_status'] == 'running':
            return {'notice': '收盘估值仍在执行，暂无当天收益。'}
        if result['valuation_status'] == 'failed':
            return {'notice': result['valuation_error']}
        return {**result['valuation'], 'is_trading_day': True}
