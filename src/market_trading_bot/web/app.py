"""只读监控 API 与构建后的前端页面。"""
import json
import re
import sqlite3
from asyncio import to_thread
from contextlib import asynccontextmanager, closing
from datetime import date
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from market_trading_bot.runtime.TradingScheduler import TradingScheduler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


@asynccontextmanager
async def lifespan(application: FastAPI):
    scheduler = TradingScheduler(ROOT)
    scheduler.start()
    application.state.scheduler = scheduler
    try:
        yield
    finally:
        await to_thread(scheduler.stop)
        application.state.scheduler = None


app = FastAPI(title='交易程序监控', docs_url=None, redoc_url=None, lifespan=lifespan)


def settings():
    return json.loads((ROOT / 'config/global.json').read_text())


def strategies():
    return [json.loads(path.read_text()) for path in sorted((ROOT / 'config').glob('*_strategy.json'))]


def strategy_by_id(strategy_id):
    for item in strategies():
        if item['strategy'] == strategy_id:
            return item
    raise HTTPException(404, '策略不存在')


@app.get('/api/runtime')
def runtime(strategy_id: str | None = None):
    configurations = strategies()
    if not configurations:
        return {'state': 'unknown', 'online': False}
    configuration = strategy_by_id(strategy_id) if strategy_id else configurations[0]
    strategy_id = configuration['strategy']
    if not configuration.get('enable', True):
        return {'strategy': strategy_id, 'state': 'disabled', 'online': False}
    scheduler = getattr(app.state, 'scheduler', None) or TradingScheduler(ROOT)
    return scheduler.get_status(strategy_id)


@app.get('/api/strategies')
def list_strategies():
    return [{'id': item['strategy'], 'enable': item.get('enable', True), 'instances': [
        {'id': instance['id'], 'mode': instance['mode'], 'enabled': instance['enabled']}
        for instance in item.get('instances', [])]} for item in strategies()]


@app.get('/api/accounts/{instance_id}')
def account(instance_id: str):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', instance_id):
        raise HTTPException(404, '账户不存在')
    if not any(instance['id'] == instance_id for item in strategies() for instance in item.get('instances', [])):
        raise HTTPException(404, '账户不存在')
    database = (ROOT / settings()['storage']['data_dir'] / instance_id / 'ledger.sqlite3').resolve()
    if not database.exists():
        return {'initialized': False}
    with closing(sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)) as connection:
        row = connection.execute('SELECT payload FROM runs ORDER BY day DESC LIMIT 1').fetchone()
        if row is None:
            return {'initialized': False}
        latest = json.loads(row[0])
        initial = connection.execute('SELECT initial_cash FROM account WHERE id=1').fetchone()
    return {'initialized': True, 'date': latest['date'], 'as_of': latest.get('signal', {}).get('as_of'),
            'cash': latest['cash'], 'equity': latest['equity'], 'positions': latest['positions'],
            'initial_cash': initial[0] if initial else None}


def log_directory(strategy_id):
    strategy_by_id(strategy_id)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', strategy_id):
        raise HTTPException(404, '策略不存在')
    return ROOT / settings()['logging']['directory'] / strategy_id


@app.get('/api/strategies/{strategy_id}/logs')
def log_dates(strategy_id: str):
    return sorted([path.stem for path in log_directory(strategy_id).glob('*.log')
                   if re.fullmatch(r'\d{4}-\d{2}-\d{2}', path.stem)], reverse=True)


@app.get('/api/strategies/{strategy_id}/logs/{day}')
def log_content(strategy_id: str, day: date, offset: int = Query(0, ge=0)):
    path = log_directory(strategy_id) / f'{day.isoformat()}.log'
    if not path.is_file():
        raise HTTPException(404, '日志不存在')
    with path.open('rb') as stream:
        stream.seek(offset)
        content = stream.read(65536)
        # 包含完整的最后一行，避免分页切断 UTF-8 字符。
        if content and not content.endswith(b'\n'):
            content += stream.readline(4096)
        next_offset = stream.tell()
        has_more = bool(stream.read(1))
    return {'content': content.decode('utf-8', errors='replace'), 'next_offset': next_offset, 'has_more': has_more}


DIST = ROOT / 'frontend/dist'
if (DIST / 'assets').exists():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')


@app.get('/')
def index():
    if not (DIST / 'index.html').exists():
        raise HTTPException(503, '请先构建前端：cd frontend && npm ci && npm run build')
    return FileResponse(DIST / 'index.html')
