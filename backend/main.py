"""统一启动网页服务和定时模拟交易线程。"""
import argparse
import sys
import uvicorn
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'dev'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1', help='监听地址')
    parser.add_argument('--port', type=int, default=8001, help='后端端口，默认8001')
    args = parser.parse_args()
    uvicorn.run('market_trading_bot.web.app:app', host=args.host, port=args.port, workers=1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
