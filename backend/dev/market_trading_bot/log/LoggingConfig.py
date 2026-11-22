"""集中管理应用和策略日志的公共格式及控制台输出。"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_FORMAT = '%(asctime)s %(levelname)s %(message)s'
DEFAULT_DATE_FORMAT = '%Y-%m-%d %H:%M:%S (UTC+8)'


def create_formatter(settings):
    """使用统一的北京时间格式，兼容未配置格式的旧配置文件。"""
    formatter = logging.Formatter(settings.get('format', DEFAULT_FORMAT),
                                  datefmt=settings.get('date_format', DEFAULT_DATE_FORMAT))
    formatter.formatTime = lambda record, datefmt=None: datetime.fromtimestamp(
        record.created, ZoneInfo('Asia/Shanghai')).strftime(datefmt or DEFAULT_DATE_FORMAT)
    return formatter


def configure_logging(settings):
    """配置应用命名空间的控制台日志，不修改其他库的日志设置。"""
    logger = logging.getLogger('market_trading_bot')
    logger.setLevel(settings['level'])
    logger.propagate = False
    for handler in tuple(logger.handlers):
        if getattr(handler, '_market_trading_bot_console', False):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler()
    handler._market_trading_bot_console = True
    handler.setFormatter(create_formatter(settings))
    logger.addHandler(handler)
