"""验证每日早安通知及其调度配置。"""
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

    def test_every_day_sends_with_calendar_status(self):
        for context, expected in (({'day': self.today, 'is_trading_day': True}, True),
                                  ({'day': self.today, 'is_trading_day': False}, False), (None, None)):
            with self.subTest(context=context):
                self.scheduler.notifier.reset_mock()
                self.scheduler.calendar_context = context
                self.scheduler._send_morning_greeting()
                self.scheduler.notifier.send_morning_greeting.assert_called_once_with(expected)

    def test_stopped_scheduler_does_not_send(self):
        self.scheduler.stop_event.set()
        self.scheduler._send_morning_greeting()
        self.scheduler.notifier.send_morning_greeting.assert_not_called()

    def test_message_addresses_owner(self):
        notifier = PushNotifier({'pushplus': {'token': 'test'}})
        notifier.client = Mock()
        notifier.send_morning_greeting(True)
        title, body = notifier.client.send.call_args.args
        for text in ('主人', '交易日'):
            self.assertIn(text, title)
        for text in ('主人', '运行正常', '等待开盘', '财富自由'):
            self.assertIn(text, body)

    def test_non_trading_and_unknown_messages(self):
        notifier = PushNotifier({'pushplus': {'token': 'test'}})
        notifier.client = Mock()
        for status, expected in ((False, '休市'), (None, '无法确认')):
            with self.subTest(status=status):
                notifier.send_morning_greeting(status)
                title, body = notifier.client.send.call_args.args
                self.assertIn(expected, title + body)
                self.assertNotIn('等待开盘', body)

    def test_configured_time_is_valid(self):
        config = Path(__file__).resolve().parents[1] / 'dev/market_trading_bot/config/global.json'
        schedule = json.loads(config.read_text())['schedule']
        self.assertEqual(schedule['morning_notification_time'], '00:11')
