"""按策略和北京时间日期写日志，保留最近配置数量的日志日期。"""
import logging
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class StrategyLogger(logging.Handler):
    def __init__(self, directory: Path, retention_dates: int):
        super().__init__()
        if type(retention_dates) is not int or retention_dates < 1:
            raise ValueError('日志保留日期数必须为正整数')
        self.directory = directory
        self.retention_dates = retention_dates
        self.last_date = None
        directory.mkdir(parents=True, exist_ok=True)

    def emit(self, record: logging.LogRecord):
        day = datetime.fromtimestamp(record.created, ZoneInfo('Asia/Shanghai')).date().isoformat()
        path = self.directory / f'{day}.log'
        with path.open('a', encoding='utf-8') as stream:
            stream.write(self.format(record) + '\n')
        if day != self.last_date:
            paths = sorted(p for p in self.directory.iterdir()
                           if p.is_file() and re.fullmatch(r'\d{4}-\d{2}-\d{2}\.log', p.name))
            for expired in paths[:-self.retention_dates]:
                expired.unlink()
            self.last_date = day


@contextmanager
def strategy_logging(root: Path, strategy: str, settings: dict):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', strategy):
        raise ValueError('策略日志名称只能包含字母、数字、下划线及连字符')
    logger = logging.Logger(f'market_trading_bot.{strategy}')
    logger.setLevel(settings['level'])
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S%z')
    formatter.formatTime = lambda record, datefmt=None: datetime.fromtimestamp(
        record.created, ZoneInfo('Asia/Shanghai')).strftime(datefmt or '%Y-%m-%d %H:%M:%S%z')
    file_handler = StrategyLogger(root / settings['directory'] / strategy, settings['retention_dates'])
    console_handler = logging.StreamHandler()
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    try:
        yield logger
    finally:
        for handler in tuple(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
