"""15:05更新账户每日收盘资产及收益，不发送消息、不注册定时任务。"""
import logging
import threading
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from market_trading_bot.market.AKShareMarket import AKShareMarket, SHANGHAI
from market_trading_bot.repository.PapAccountRepository import PapAccountRepository


class DailyValuationTask:
    class Stopped(RuntimeError):
        pass

    def __init__(self, root, settings, configs, context, stop_event, is_running, now):
        """创建账户估值任务，执行时间由调度器控制。

        :param root: backend/dev 目录。
        :param settings: 全局存储和行情配置。
        :param configs: 按策略 ID 索引的策略及账户配置。
        :param context: 日期、是否交易日及上一交易日，失败时包含 notice。
        :param stop_event: 与调度器共享的停止信号。
        :param is_running: 接收策略 ID、账户 ID，返回交易任务是否运行中。
        :param now: 返回当前北京时间的函数。
        """
        self.context = context
        self.thread = threading.Thread(target=self._run, name='daily-valuation-task', daemon=True)
        self.completed = False
        self.directory = root / settings['storage']['data_dir']
        self.settings = settings
        self.configs = configs
        self.stop_event = stop_event
        self.is_running = is_running
        self.now = now
        self.logger = logging.getLogger(__name__)

    def start(self):
        """由调度器在15:05启动一次性估值线程。"""
        self.thread.start()

    def stop(self):
        """发出共享停止信号，并等待估值线程退出。"""
        self.stop_event.set()
        self.thread.join()

    def _run(self):
        """执行估值，记录完成状态；未处理的异常写入日志。"""
        try:
            self.completed = self.run()
        except Exception:
            self.logger.exception('每日估值任务失败')

    def run(self):
        """逐个更新启用模拟账户的收盘估值，单个账户失败不影响其他账户。

        :return: 遍历完成返回 True；停止或跨日返回 False，不代表所有账户估值成功。
        """
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
        """更新一个账户的估值及状态；复用成功结果，交易使估值失效后重新计算。

        :param strategy_id: 账户所属策略 ID。
        :param instance: 账户配置，包含 id。
        :raises Stopped: 收到停止信号或执行期间跨日。
        """
        day = self.context['day']
        account = PapAccountRepository.get_account_snapshot_on_or_before(self.directory, instance['id'], day.isoformat())
        if not account['initialized']:
            return
        repository = PapAccountRepository(self.directory, instance['id'], strategy_id)
        result = PapAccountRepository.get_daily_result(self.directory, instance['id'], day.isoformat())
        # 重启复用已完成的估值；后续交易记账会将其置为 pending，届时重新计算。
        if result and result['valuation_status'] == 'success' and result['valuation'] is not None:
            return
        if self.context.get('notice'):
            repository.mark_valuation_status(day.isoformat(), 'failed', self.context['notice'])
            return
        target = day if self.context['is_trading_day'] else self.context['previous_trading_day']
        if not self.context['is_trading_day'] and PapAccountRepository.find_latest_valuation_on_or_before(self.directory, instance['id'], target.isoformat()):
            return
        repository.mark_valuation_status(target.isoformat(), 'running')
        try:
            market = AKShareMarket(self.settings['market'])
            summary = self._retry_valuation(lambda: self._calculate_daily_valuation(market, strategy_id, instance['id'], repository), day)
            repository.save_daily_valuation(summary)
        except self.Stopped:
            raise
        except Exception:
            self.logger.exception('收盘估值失败 strategy=%s instance=%s', strategy_id, instance['id'])
            repository.mark_valuation_status(target.isoformat(), 'failed',
                                             '收盘估值失败或交易任务仍在运行，未生成收益，请查看日志。')

    def _calculate_daily_valuation(self, market, strategy_id, instance_id, repository):
        """计算收盘资产及收益，缺失上一交易日估值时按历史持仓补算。

        :param market: 提供不复权收盘价的行情对象。
        :param strategy_id: 策略 ID。
        :param instance_id: 账户 ID。
        :param repository: 保存补算估值的账户仓库。
        :return: 交易日返回资产、当日及累计收益；休市日仅返回上一交易日资产及累计收益。
        :raises RuntimeError: 交易任务仍在运行。
        :raises ValueError: 缺少历史账户或收益基准无效。
        """
        day = self.context['day']
        if self.is_running(strategy_id, instance_id):
            raise RuntimeError('交易任务仍在运行')
        account = PapAccountRepository.get_account_snapshot_on_or_before(self.directory, instance_id, day.isoformat())
        if not self.context['is_trading_day']:
            close_day = self.context['previous_trading_day']
            account = PapAccountRepository.get_account_snapshot_on_or_before(self.directory, instance_id, close_day.isoformat())
            if not account['initialized']:
                raise ValueError('休市日尚无可估值账户')
            return self._calculate_closing_assets(market, account, close_day)
        snapshot = self._calculate_closing_assets(market, account, day)
        if self.is_running(strategy_id, instance_id):
            raise RuntimeError('交易任务仍在运行')
        previous_day = self.context['previous_trading_day']
        previous = PapAccountRepository.find_latest_valuation_on_or_before(self.directory, instance_id, previous_day.isoformat())
        if previous is None or previous['date'] != previous_day.isoformat():
            previous_account = PapAccountRepository.get_account_snapshot_on_or_before(self.directory, instance_id, previous_day.isoformat())
            if previous_account['initialized']:
                previous = repository.save_daily_valuation(self._calculate_closing_assets(market, previous_account, previous_day))
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

    def _calculate_closing_assets(self, market, account, day):
        """按指定日期的收盘价计算总资产及相对开户资金的累计收益。

        :param market: 收盘价查询对象。
        :param account: 含现金、持仓及实际开户资金的账户快照。
        :param day: 收盘价所属交易日。
        :return: 包含日期、估值时间、现金、持仓、总资产及累计收益的字典。
        :raises ValueError: 开户资金不大于零。
        """
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

    def _retry_valuation(self, function, day):
        """执行估值，失败最多尝试 3 次，间隔 30 秒。

        :param function: 无参数的估值函数。
        :param day: 本次任务日期。
        :return: 估值函数的返回值。
        :raises Stopped: 收到停止信号或跨日，立即中止重试。
        :raises Exception: 3 次均失败时抛出最后一次异常。
        """
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
