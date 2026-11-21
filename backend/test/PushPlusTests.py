"""通知不得影响交易，发送状态与重试预算必须跨重启保留。"""
import os
import tempfile
import unittest
from datetime import datetime
from market_trading_bot.market.AKShareMarket import SHANGHAI
from market_trading_bot.PushPlusClient import PushPlusClient
from market_trading_bot.PushPlusNotifier import PushPlusNotifier
from market_trading_bot.TradingReport import TradingReport
from market_trading_bot.TradingTask import TradingTask
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class PushPlusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = {'storage': {'data_dir': 'data'}, 'pushplus': {'enabled': True}}
        self.environment = patch.dict(os.environ, {'PUSHPLUS_TOKEN': 'test-secret'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def task(self):
        return SimpleNamespace(
            strategy_id='etf_rotation', instance_id='pap',
            scheduled_at=datetime(2026, 11, 20, 14, 30, tzinfo=SHANGHAI),
            result={'success': True, 'finished_at': '2026-11-20T14:31:00+08:00'},
            output={'date': '2026-11-20', 'equity': '102350', 'cash': '12350',
                    'positions': {}, 'fills': []},
            account_repository=SimpleNamespace(directory=self.root / 'data/pap',
                                               latest=lambda *args: {'initial_cash': '100000'}))

    def test_no_fills_still_reports_and_deduplicates_after_restart(self):
        notifier = PushPlusNotifier(self.root, self.settings)
        notifier.report(self.task())
        notifier.report(self.task())
        title, content = TradingReport.build(self.task())
        self.assertIn('今日无调仓', content)
        self.assertIn('+2.35%', content)
        self.assertIn('非收盘收益', content)
        restarted = PushPlusNotifier(self.root, self.settings)
        with patch.object(restarted.client, 'send', return_value='receipt') as send:
            restarted.dispatch()
            restarted.report(self.task())
            restarted.dispatch()
        send.assert_called_once_with(title, content)
        self.assertIsNone(restarted.repository.due())

    def test_retry_budget_survives_restart(self):
        notifier = PushPlusNotifier(self.root, self.settings)
        notifier.report(self.task())
        with patch('market_trading_bot.repository.NotificationRepository.time.time', return_value=100):
            with patch.object(notifier.client, 'send', side_effect=PushPlusClient.Error('timeout', True)):
                notifier.dispatch()
            self.assertIsNone(notifier.repository.due())
        restarted = PushPlusNotifier(self.root, self.settings)
        with patch('market_trading_bot.repository.NotificationRepository.time.time', return_value=1000):
            self.assertEqual(restarted.repository.due()['attempts'], 1)
            with patch.object(restarted.client, 'send', return_value='ok'):
                restarted.dispatch()
        self.assertIsNone(restarted.repository.due())

    def test_permanent_error_not_retried(self):
        notifier = PushPlusNotifier(self.root, self.settings)
        notifier.report(self.task())
        with patch.object(notifier.client, 'send', side_effect=PushPlusClient.Error('invalid token')) as send:
            notifier.dispatch()
            notifier.dispatch()
        self.assertEqual(send.call_count, 1)
        self.assertIsNone(notifier.repository.due())

    def test_maximum_three_attempts(self):
        notifier = PushPlusNotifier(self.root, self.settings)
        notifier.report(self.task())
        with patch.object(notifier.client, 'send', side_effect=PushPlusClient.Error('network', True)) as send:
            for stamp in (100, 1000, 2000, 3000):
                with patch('market_trading_bot.repository.NotificationRepository.time.time', return_value=stamp):
                    notifier.dispatch()
        self.assertEqual(send.call_count, 3)
        self.assertIsNone(notifier.repository.due())

    def test_disabled_or_missing_token_does_not_create_database(self):
        self.settings['pushplus']['enabled'] = False
        self.assertIsNone(PushPlusNotifier(self.root, self.settings).repository)
        self.settings['pushplus']['enabled'] = True
        with patch.dict(os.environ, {'PUSHPLUS_TOKEN': ''}):
            self.assertIsNone(PushPlusNotifier(self.root, self.settings).repository)
        self.assertFalse((self.root / 'data').exists())

    def test_failed_report_does_not_expose_exception(self):
        task = self.task()
        task.result.update(success=False, error='secret-token-in-url')
        title, content = TradingReport.build(task)
        self.assertIn('失败', title)
        self.assertNotIn('102350', content)
        self.assertNotIn('secret-token', content)

    def test_client_checks_application_code_and_hides_response(self):
        client = PushPlusClient('test-secret')
        with patch('market_trading_bot.PushPlusClient.requests.post', return_value=Mock(
                status_code=200, json=lambda: {'code': 903, 'msg': 'test-secret'})) as post:
            with self.assertRaises(PushPlusClient.Error) as error:
                client.send('title', 'content')
        self.assertFalse(error.exception.retryable)
        self.assertNotIn('test-secret', str(error.exception))
        self.assertEqual(post.call_args.kwargs['json']['channel'], 'wechat')
        self.assertEqual(post.call_args.kwargs['timeout'], (3, 10))

    def test_notification_failure_does_not_change_successful_trade(self):
        notifier = Mock()
        notifier.report.side_effect = RuntimeError('disk full')
        task = TradingTask(Mock(), Mock(), Mock(), Mock(), Mock(), strategy_id='example',
                           instance_id='pap', initial_cash=100000,
                           scheduled_at=datetime(2026, 11, 20, 14, 30, tzinfo=SHANGHAI), notifier=notifier)
        with patch.object(task, 'run_pap', return_value={'fills': []}) as trade:
            task.start()
            task.thread.join(timeout=3)
        self.assertFalse(task.thread.is_alive())
        self.assertTrue(task.result['success'])
        trade.assert_called_once()
        notifier.report.assert_called_once_with(task)

    def test_database_failure_does_not_prevent_service_setup(self):
        with patch('market_trading_bot.PushPlusNotifier.NotificationRepository', side_effect=OSError('disk full')):
            self.assertIsNone(PushPlusNotifier(self.root, self.settings).repository)

    def test_failed_trade_is_persisted_without_network_on_trade_thread(self):
        notifier = PushPlusNotifier(self.root, self.settings)
        task = TradingTask(Mock(), Mock(), Mock(), Mock(), Mock(), strategy_id='example',
                           instance_id='pap', initial_cash=100000,
                           scheduled_at=datetime(2026, 11, 20, 14, 30, tzinfo=SHANGHAI), notifier=notifier)
        with patch.object(task, 'run_pap', side_effect=ValueError('bad market data')):
            with patch.object(notifier.client, 'send') as send:
                task.start()
                task.thread.join(timeout=3)
                send.assert_not_called()
        self.assertFalse(task.result['success'])
        self.assertIn('失败', notifier.repository.due()['title'])

    def test_fills_report_contains_trade_details(self):
        task = self.task()
        task.output['fills'] = [{'side': 'buy', 'symbol': '510300.XSHG', 'quantity': 100,
                                 'price': '3.500', 'commission': '5.00'}]
        task.output['positions'] = {'510300.XSHG': {'quantity': 100}}
        _, content = TradingReport.build(task)
        self.assertIn('买入 510300.XSHG × 100 份', content)
        self.assertIn('手续费 5.00 元', content)
        self.assertNotIn('今日无调仓', content)
