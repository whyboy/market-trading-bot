"""验证统一日志配置和策略文件隔离。"""
import logging
import tempfile
import unittest
from datetime import datetime
from market_trading_bot.log.LoggingConfig import configure_logging
from market_trading_bot.log.StrategyLogger import strategy_logging
from pathlib import Path
from zoneinfo import ZoneInfo


class LoggingConfigTests(unittest.TestCase):
    def test_shared_format_and_strategy_files(self):
        settings = {'directory': 'logs', 'level': 'INFO', 'retention_dates': 30,
                    'format': '%(asctime)s %(levelname)s %(message)s',
                    'date_format': '%Y-%m-%d %H:%M:%S (UTC+8)'}
        app_logger = logging.getLogger('market_trading_bot')
        old_handlers, old_level, old_propagate = app_logger.handlers[:], app_logger.level, app_logger.propagate
        try:
            configure_logging(settings)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with strategy_logging(root, 'first', settings) as first, strategy_logging(root, 'second', settings) as second:
                    first.info('第一条')
                    second.info('第二条')
                day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
                first_text = (root / 'logs/first' / f'{day}.log').read_text(encoding='utf-8')
                second_text = (root / 'logs/second' / f'{day}.log').read_text(encoding='utf-8')
                self.assertRegex(first_text, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \(UTC\+8\) INFO 第一条\n$')
                self.assertIn('第二条', second_text)
                self.assertNotIn('第二条', first_text)
                record = logging.LogRecord('market_trading_bot', logging.INFO, '', 0, '测试', (), None)
                self.assertRegex(app_logger.handlers[-1].format(record), r'\(UTC\+8\) INFO 测试$')
        finally:
            for handler in app_logger.handlers:
                if handler not in old_handlers:
                    handler.close()
            app_logger.handlers = old_handlers
            app_logger.setLevel(old_level)
            app_logger.propagate = old_propagate

    def test_legacy_settings_use_default_format(self):
        settings = {'directory': 'logs', 'level': 'INFO', 'retention_dates': 30}
        with tempfile.TemporaryDirectory() as directory:
            with strategy_logging(Path(directory), 'legacy', settings) as logger:
                self.assertIn('(UTC+8) INFO 测试', logger.handlers[0].format(
                    logging.LogRecord('legacy', logging.INFO, '', 0, '测试', (), None)))
