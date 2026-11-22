"""验证交易日早安通知及其调度配置。"""
import json
import unittest
from datetime import date, datetime
from market_trading_bot.market.MarketProvider import SHANGHAI
from market_trading_bot.notification.PushNotifier import PushNotifier
from market_trading_bot.TaskScheduler import TaskScheduler
from pathlib import Path
from unittest.mock import Mock, patch


class MorningGreetingTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = TaskScheduler(Path('/unused'))
        self.scheduler.notifier = Mock()
        self.today = date(2026, 11, 20)
        self.clock = patch.object(self.scheduler, 'now', return_value=datetime(2026, 11, 20, 9, tzinfo=SHANGHAI))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_only_trading_days_send(self):
        for context, expected in (({'day': self.today, 'is_trading_day': True}, 1),
                                  ({'day': self.today, 'is_trading_day': False}, 0), (None, 0)):
            with self.subTest(context=context), patch.object(self.scheduler, '_trading_day_context', return_value=context):
                self.scheduler.notifier.reset_mock()
                self.scheduler._send_morning_greeting()
                self.assertEqual(self.scheduler.notifier.send_morning_greeting.call_count, expected)

    def test_stopped_scheduler_does_not_send(self):
        self.scheduler.stop_event.set()
        self.scheduler._send_morning_greeting()
        self.scheduler.notifier.send_morning_greeting.assert_not_called()

    def test_message_addresses_owner(self):
        notifier = PushNotifier({'pushplus': {'token': 'test'}})
        notifier.client = Mock()
        notifier.send_morning_greeting()
        title, body = notifier.client.send.call_args.args
        for text in ('主人', '交易日'):
            self.assertIn(text, title)
        for text in ('主人', '运行正常', '等待开盘', '财富自由'):
            self.assertIn(text, body)

    def test_configured_time_is_valid(self):
        config = Path(__file__).resolve().parents[1] / 'dev/market_trading_bot/config/global.json'
        schedule = json.loads(config.read_text())['schedule']
        self.assertEqual(schedule['morning_notification_time'], '00:11')
