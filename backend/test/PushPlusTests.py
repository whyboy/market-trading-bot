"""直接发送执行报告，通知失败不得影响交易。"""
import requests
import tempfile
import unittest
from datetime import datetime
from market_trading_bot.market.AKShareMarket import SHANGHAI
from market_trading_bot.notification.PushPlusClient import PushPlusClient
from market_trading_bot.notification.PushPlusNotifier import PushPlusNotifier
from market_trading_bot.notification.report.TradeExecutionReport import TradeExecutionReport
from market_trading_bot.TradingTask import TradingTask
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, Mock, patch


class PushPlusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = {'storage': {'data_dir': 'data'}, 'pushplus': {'token': 'test-secret'}}

    def task(self):
        return SimpleNamespace(
            strategy_id='etf_rotation', instance_id='pap',
            scheduled_at=datetime(2026, 11, 20, 14, 30, tzinfo=SHANGHAI),
            result={'success': True, 'finished_at': '2026-11-20T14:31:00+08:00'},
            output={'date': '2026-11-20', 'signal': {'as_of': '2026-11-20T14:30:00+08:00'},
                    'equity': '102350', 'cash': '12350',
                    'positions': {}, 'fills': [], 'csv_exported': True},
            account_repository=SimpleNamespace(directory=self.root / 'data/pap',
                                               latest=lambda *args: {'initial_cash': '100000'}))

    def test_no_fills_still_sends_report_directly(self):
        notifier = PushPlusNotifier(self.settings)
        title, content = TradeExecutionReport.build(self.task())
        self.assertIn('今日无调仓', content)
        self.assertIn('+2.35%', content)
        self.assertIn('非收盘收益', content)
        with patch.object(notifier.client, 'send', return_value='receipt') as send:
            notifier.send_trade_execution_report(self.task())
        send.assert_called_once_with(title, content)
        self.assertFalse((self.root / 'data').exists())

    def test_failed_report_does_not_expose_exception(self):
        task = self.task()
        task.result.update(success=False, error='secret-token-in-url')
        title, content = TradeExecutionReport.build(task)
        self.assertIn('失败', title)
        self.assertNotIn('102350', content)
        self.assertNotIn('secret-token', content)

    def test_client_checks_application_code_and_hides_response(self):
        client = PushPlusClient('test-secret')
        with patch('market_trading_bot.notification.PushPlusClient.requests.post', return_value=Mock(
                status_code=200, json=lambda: {'code': 903, 'msg': 'test-secret'})) as post:
            with self.assertRaises(PushPlusClient.Error) as error:
                client.send('title', 'content')
        post.assert_called_once()
        self.assertNotIn('test-secret', str(error.exception))
        self.assertEqual(post.call_args.kwargs['json']['channel'], 'wechat')
        self.assertEqual(post.call_args.kwargs['timeout'], (3, 10))

    def test_notification_failure_does_not_change_successful_trade(self):
        notifier = PushPlusNotifier(self.settings)
        task = TradingTask(Mock(), Mock(), Mock(), Mock(), Mock(), strategy_id='example',
                           instance_id='pap', initial_cash=100000,
                           scheduled_at=datetime(2026, 11, 20, 14, 30, tzinfo=SHANGHAI), notifier=notifier)
        with patch.object(task, 'run_pap', return_value={'fills': []}) as trade:
            with patch.object(TradeExecutionReport, 'build', return_value=('title', 'content')):
                with patch.object(notifier.client, 'send', side_effect=PushPlusClient.Error('network')) as send:
                    task.start()
                    task.thread.join(timeout=3)
        self.assertFalse(task.thread.is_alive())
        self.assertTrue(task.result['success'])
        trade.assert_called_once()
        send.assert_called_once()

    def test_failed_trade_sends_failure_report(self):
        notifier = PushPlusNotifier(self.settings)
        task = TradingTask(Mock(), Mock(), Mock(), Mock(), Mock(), strategy_id='example',
                           instance_id='pap', initial_cash=100000,
                           scheduled_at=datetime(2026, 11, 20, 14, 30, tzinfo=SHANGHAI), notifier=notifier)
        with patch.object(task, 'run_pap', side_effect=ValueError('bad market data')):
            with patch.object(notifier.client, 'send', return_value='receipt') as send:
                task.start()
                task.thread.join(timeout=3)
        self.assertFalse(task.thread.is_alive())
        self.assertFalse(task.result['success'])
        send.assert_called_once()
        self.assertIn('失败', send.call_args.args[0])

    def test_fills_report_contains_trade_details(self):
        task = self.task()
        task.output['fills'] = [{'side': 'buy', 'symbol': '510300.XSHG', 'quantity': 100,
                                 'price': '3.500', 'commission': '5.00'}]
        task.output['positions'] = {'510300.XSHG': {'quantity': 100}}
        _, content = TradeExecutionReport.build(task)
        self.assertIn('买入 510300.XSHG × 100 份', content)
        self.assertIn('手续费 5.00 元', content)
        self.assertNotIn('今日无调仓', content)

    def test_network_failure_retries_then_succeeds(self):
        response = Mock(status_code=200, json=lambda: {'code': 200, 'data': 'receipt'})
        with patch('market_trading_bot.notification.PushPlusClient.requests.post',
                   side_effect=[requests.Timeout('secret'), requests.ConnectionError('secret'), response]) as post:
            with patch('market_trading_bot.notification.PushPlusClient.time.sleep') as sleep:
                receipt = PushPlusClient('test-secret').send('title', 'content')
        self.assertEqual(receipt, 'receipt')
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])

    def test_network_failure_stops_after_three_attempts(self):
        with patch('market_trading_bot.notification.PushPlusClient.requests.post',
                   side_effect=requests.Timeout('test-secret')) as post:
            with patch('market_trading_bot.notification.PushPlusClient.time.sleep') as sleep:
                with self.assertRaises(PushPlusClient.Error) as error:
                    PushPlusClient('test-secret').send('title', 'content')
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn('test-secret', str(error.exception))

    def test_temporary_service_errors_retry_and_permanent_http_errors_do_not(self):
        for status, code, attempts in ((503, 0, 3), (429, 0, 3), (200, 500, 3), (403, 0, 1)):
            with self.subTest(status=status, code=code):
                with patch('market_trading_bot.notification.PushPlusClient.requests.post',
                           return_value=Mock(status_code=status, json=lambda: {'code': code})) as post:
                    with patch('market_trading_bot.notification.PushPlusClient.time.sleep'):
                        with self.assertRaises(PushPlusClient.Error):
                            PushPlusClient('test-secret').send('title', 'content')
                self.assertEqual(post.call_count, attempts)

    def test_success_does_not_retry(self):
        with patch('market_trading_bot.notification.PushPlusClient.requests.post',
                   return_value=Mock(status_code=200, json=lambda: {'code': 200, 'data': 'receipt'})) as post:
            with patch('market_trading_bot.notification.PushPlusClient.time.sleep') as sleep:
                self.assertEqual(PushPlusClient('test-secret').send('title', 'content'), 'receipt')
        post.assert_called_once()
        sleep.assert_not_called()
