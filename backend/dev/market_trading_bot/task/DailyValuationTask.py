"""更新账户每日资产及收益，不发送消息、不注册定时任务。"""
import logging
import threading
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from market_trading_bot.market.MarketProvider import MarketProvider, SHANGHAI
from market_trading_bot.service.AccountService import AccountService
from market_trading_bot.service.DailyResultsService import DailyResultsService


class DailyValuationTask:
    class Stopped(RuntimeError):
        pass

    def __init__(self, root, settings, configs, context, stop_event, is_trade_running, now):
        """创建估值线程；context 为当天日历，stop_event 与调度器共享。"""
        self.context = context
        self.thread = threading.Thread(target=self._run, name='daily-valuation-task', daemon=True)
        self.completed = False
        self.directory = root / settings['storage']['data_dir']
        self.settings = settings
        self.configs = configs
        self.stop_event = stop_event
        self.is_trade_running = is_trade_running
        self.now = now
        self.logger = logging.getLogger(__name__)

    def start(self):
        """启动估值线程。"""
        self.thread.start()

    def stop(self):
        """设置共享停止信号并等待线程退出。"""
        self.stop_event.set()
        self.thread.join()

    def _run(self):
        """执行估值并记录完成状态。"""
        try:
            self.completed = self.run()
        except Exception:
            self.logger.exception('每日估值任务失败')

    def run(self):
        """逐账户估值；单账户失败继续，停止或跨日返回 False。"""
        day = self.context['day']
        for strategy_id, config in sorted(self.configs.items()):
            for instance in config.get('instances', []):
                if not instance['enabled'] or instance['mode'] != 'pap':
                    continue
                if self.stop_event.is_set() or self.now().date() != day:
                    return False
                try:
                    self._update_account(strategy_id, instance)
                except self.Stopped:
                    return False
                except Exception:
                    self.logger.exception('账户估值处理失败 strategy=%s instance=%s', strategy_id, instance['id'])
        return True

    def _update_account(self, strategy_id, instance):
        """更新账户估值；复用成功结果，后续交易会使旧估值失效。"""
        day = self.context['day']
        account = AccountService.get_snapshot(self.directory, instance['id'], day.isoformat())
        if not account['initialized']:
            return
        AccountService(self.directory, instance['id'], strategy_id)
        results_service = DailyResultsService(self.directory, instance['id'])
        result = DailyResultsService.get_daily_result(self.directory, instance['id'], day.isoformat())
        # 后续交易会将估值置为 pending；否则重启时复用。
        if result and result['valuation_status'] == 'success' and result['valuation'] is not None:
            return
        if self.context.get('notice'):
            results_service.set_valuation_status(day.isoformat(), 'failed', self.context['notice'])
            return
        close_day = day if self.context['is_trading_day'] else self.context['previous_trading_day']
        if not self.context['is_trading_day'] and DailyResultsService.get_valuation(self.directory, instance['id'], close_day.isoformat()):
            return
        results_service.set_valuation_status(close_day.isoformat(), 'running')
        try:
            market = MarketProvider(self.settings['market'])
            summary = self._retry(lambda: self._value_account(market, results_service, strategy_id, instance['id']), day)
            results_service.save_valuation(summary)
        except self.Stopped:
            raise
        except Exception:
            self.logger.exception('收盘估值失败 strategy=%s instance=%s', strategy_id, instance['id'])
            results_service.set_valuation_status(close_day.isoformat(), 'failed',
                                             '收盘估值失败或交易任务仍在运行，未生成收益，请查看日志。')

    def _value_account(self, market, results_service, strategy_id, instance_id):
        """计算当日收益；缺失上一交易日估值时按历史持仓补算。"""
        day = self.context['day']
        if self.is_trade_running(strategy_id, instance_id):
            raise RuntimeError('交易任务仍在运行')
        close_day = day if self.context['is_trading_day'] else self.context['previous_trading_day']
        account = AccountService.get_snapshot(self.directory, instance_id, close_day.isoformat())
        if not self.context['is_trading_day']:
            if not account['initialized']:
                raise ValueError('休市日尚无可估值账户')
            return self._closing_assets(market, account, close_day)
        snapshot = self._closing_assets(market, account, day)
        if self.is_trade_running(strategy_id, instance_id):
            raise RuntimeError('交易任务仍在运行')
        previous_day = self.context['previous_trading_day']
        previous = DailyResultsService.get_valuation(self.directory, instance_id, previous_day.isoformat())
        if previous is None or previous['date'] != previous_day.isoformat():
            previous_account = AccountService.get_snapshot(self.directory, instance_id, previous_day.isoformat())
            if previous_account['initialized']:
                previous = results_service.save_valuation(self._closing_assets(market, previous_account, previous_day))
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

    def _closing_assets(self, market, account, day):
        """按指定交易日收盘价计算资产及累计收益。"""
        price_day = self.context.get('price_day', day) if day == self.context['day'] else day
        prices = market.closing_prices(account['positions'], price_day)
        equity = Decimal(account['cash']) + sum((prices[symbol] * position['quantity']
                                               for symbol, position in account['positions'].items()), Decimal('0'))
        equity = equity.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        initial_cash = Decimal(account['initial_cash'])
        if initial_cash <= 0:
            raise ValueError('实际开户资金必须大于零')
        return {'date': day.isoformat(), 'as_of': datetime.combine(price_day, time(15), SHANGHAI).isoformat(),
                'equity': str(equity), 'cash': account['cash'], 'positions': account['positions'],
                'cumulative_pnl': str(equity - initial_cash),
                'cumulative_return_pct': str((equity / initial_cash - 1) * 100)}

    def _retry(self, function, day):
        """失败最多尝试 3 次，间隔 30 秒；停止或跨日时退出。"""
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
