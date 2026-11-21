"""一次性日报任务：依据调度器提供的交易日期汇总所有策略和账户并发送。"""
import logging
import threading
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from market_trading_bot.market.AKShareMarket import AKShareMarket, SHANGHAI
from market_trading_bot.repository.DailyValuationRepository import DailyValuationRepository
from market_trading_bot.repository.PapAccountRepository import PapAccountRepository


class DailySummaryTask:
    class Stopped(RuntimeError):
        pass

    def __init__(self, root, settings, configs, notifier, context, stop_event, is_running, now):
        self.context = context
        self.thread = threading.Thread(target=self._run, name='daily-summary-task', daemon=True)
        self.completed = False
        self.directory = root / settings['storage']['data_dir']
        self.settings = settings
        self.configs = configs
        self.notifier = notifier
        self.stop_event = stop_event
        self.is_running = is_running
        self.now = now
        self.valuations = DailyValuationRepository(self.directory / 'runtime')
        self.logger = logging.getLogger(__name__)

    def start(self):
        """由调度器在15:05创建并启动一次性汇总线程。"""
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
        """汇总所有启用策略及账户，单个账户失败不影响其他账户，最终只发送一份报告。"""
        day = self.context['day']
        accounts = [(strategy_id, instance) for strategy_id, config in sorted(self.configs.items())
                    for instance in config.get('instances', []) if instance['enabled'] and instance['mode'] == 'pap']
        summaries = []
        if self.context.get('notice'):
            summaries = [{'strategy_id': strategy_id, 'instance_id': instance['id'],
                          'summary': {'notice': self.context['notice']}} for strategy_id, instance in accounts]
        elif accounts:
            market = AKShareMarket(self.settings['market'])
            for strategy_id, instance in accounts:
                try:
                    summary = self._retry(lambda: self._summary(market, strategy_id, instance['id']), day)
                except self.Stopped:
                    return False
                except Exception:
                    self.logger.exception('日报估值失败 strategy=%s instance=%s', strategy_id, instance['id'])
                    summary = {'notice': '收盘估值失败或交易任务仍在运行，未生成收益，请查看日志。'}
                summaries.append({'strategy_id': strategy_id, 'instance_id': instance['id'], 'summary': summary})
        if self.stop_event.is_set() or self.now().date() != day:
            return False
        self.notifier.send_daily_summary_report(day, summaries)
        return True

    def _summary(self, market, strategy_id, instance_id):
        day = self.context['day']
        if self.is_running(strategy_id, instance_id):
            raise RuntimeError('交易任务仍在运行')
        account = PapAccountRepository.get_account_snapshot_on_or_before(self.directory, instance_id, day.isoformat())
        if not account['initialized']:
            return {'date': day.isoformat(), 'notice': '账户尚未开户，暂无持仓和收益数据。'}
        trading = self.context['is_trading_day']
        if not trading:
            previous = self.valuations.find_latest_on_or_before(instance_id, day.isoformat())
            if previous is None:
                close_day = self.context['previous_trading_day']
                account = PapAccountRepository.get_account_snapshot_on_or_before(self.directory, instance_id, close_day.isoformat())
                if not account['initialized']:
                    return {'date': day.isoformat(), 'notice': '今日休市，尚无有效收盘估值。'}
                previous = self.valuations.save_if_absent(instance_id, self._valuation(market, account, close_day))
            return {**previous, 'date': day.isoformat(), 'is_trading_day': False}
        snapshot = self.valuations.find_latest_on_or_before(instance_id, day.isoformat())
        if snapshot is None or snapshot['date'] != day.isoformat():
            snapshot = self._valuation(market, account, day)
            if self.is_running(strategy_id, instance_id):
                raise RuntimeError('交易任务仍在运行')
            snapshot = self.valuations.save_if_absent(instance_id, snapshot)
        previous_day = self.context['previous_trading_day']
        previous = self.valuations.find_latest_on_or_before(instance_id, previous_day.isoformat())
        if previous is None or previous['date'] != previous_day.isoformat():
            previous_account = PapAccountRepository.get_account_snapshot_on_or_before(self.directory, instance_id, previous_day.isoformat())
            if previous_account['initialized']:
                previous = self.valuations.save_if_absent(instance_id, self._valuation(market, previous_account, previous_day))
            else:
                previous = {'equity': account['initial_cash'], 'date': '开户资金'}
        baseline = Decimal(previous['equity'])
        equity = Decimal(snapshot['equity'])
        if baseline <= 0:
            raise ValueError('收益比较基准必须大于零')
        fills = account['fills'] if account['date'] == day.isoformat() else []
        return {**snapshot, 'is_trading_day': True, 'comparison_date': previous['date'],
                'daily_pnl': str(equity - baseline), 'daily_return_pct': str((equity / baseline - 1) * 100),
                'buy_count': sum(fill['side'] == 'buy' for fill in fills),
                'sell_count': sum(fill['side'] == 'sell' for fill in fills),
                'execution_note': '' if account['date'] == day.isoformat() else '今日策略未完成记账，按现有持仓估值。'}

    def _valuation(self, market, account, day):
        prices = market.closing_prices(account['positions'], day)
        equity = Decimal(account['cash']) + sum((prices[symbol] * position['quantity']
                                               for symbol, position in account['positions'].items()), Decimal('0'))
        equity = equity.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        initial = Decimal(account['initial_cash'])
        if initial <= 0:
            raise ValueError('实际开户资金必须大于零')
        return {'date': day.isoformat(), 'as_of': datetime.combine(day, time(15), SHANGHAI).isoformat(),
                'equity': str(equity), 'cash': account['cash'], 'positions': account['positions'],
                'cumulative_pnl': str(equity - initial),
                'cumulative_return_pct': str((equity / initial - 1) * 100)}

    def _retry(self, function, day):
        for attempt in range(3):
            if self.stop_event.is_set() or self.now().date() != day:
                raise self.Stopped()
            try:
                result = function()
                if self.stop_event.is_set() or self.now().date() != day:
                    raise self.Stopped()
                return result
            except self.Stopped:
                raise
            except Exception:
                if attempt == 2:
                    raise
                if self.stop_event.wait(30):
                    raise self.Stopped() from None
