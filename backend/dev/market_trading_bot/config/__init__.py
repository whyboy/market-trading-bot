"""加载运行配置，隔离链路测试数据。"""
import json
from pathlib import Path


def load_settings(root: Path):
    settings = json.loads((root / 'market_trading_bot/config/global.json').read_text())
    if settings.get('runtime', {}).get('test_mode', False):
        settings['storage']['data_dir'] = str(Path(settings['storage']['data_dir']) / 'test')
        settings['logging']['directory'] = str(Path(settings['logging']['directory']) / 'test')
    return settings
