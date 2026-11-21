# market-trading-bot

ETF 动量轮动程序，启动后按交易日定时模拟调仓，并提供网页运行监控。模拟账户使用 SQLite 原子记账，可恢复、可防止同日重复成交，并导出账户交易汇总 CSV。

当前仅实现 `etf_rotation` 策略和 `pap` 模拟模式。已支持定时调仓，收盘日报和推送尚未实现。

## 策略简介

每个 A 股交易日北京时间 **14:30 自动模拟调仓**；**15:15 日报和推送**为后续功能，暂不执行。

策略每天对 ETF 池计算动量和波动率：用最近 24 个交易日收盘价加当前价做回归，以“年化收益 × 拟合优度”作为动量得分，选择得分最高的 ETF；再按“15% 目标年化波动率 ÷ 标的波动率”确定仓位，最高 100%。执行时按调仓前净资产计算目标数量，先卖后买，并应用交易单位、T+1、佣金、滑点和现金约束。

行情无效、过期或数据不足时停止运行，不生成模拟成交。

## 代码结构

```text
market-trading-bot/
├── frontend/                              # 前端，开发端口8000
├── backend/
│   ├── main.py                            # 后端启动入口，默认端口8001
│   ├── requirements.txt
│   ├── dev/                               # 后端源码和运行资源，生产环境同样使用
│   │   ├── logs/                          # 运行日志，启动后生成
│   │   └── market_trading_bot/
│   │       ├── config/                    # 全局与策略配置
│   │       ├── TradingScheduler.py        # 交易日判断与任务调度
│   │       ├── model/                      # 共享数据结构，不依赖业务模块
│   │       │   ├── MarketSnapshot.py        # MarketSnapshot、DailyClose
│   │       │   └── StrategyResult.py        # StrategyResult、RankedETF
│   │       ├── TradingTask.py         # 单次策略执行及状态更新
│   │       ├── strategy/                  # 策略实现
│   │       │   └── ETFRotationStrategyImpl.py
│   │       ├── repository/                # 数据库访问
│   │       │   ├── TaskStatusRepository.py
│   │       │   └── PapAccountRepository.py
│   │       ├── log/StrategyLogger.py      # 日志处理代码
│   │       ├── market/AKShareMarket.py
│   │       ├── execution/PapExecutor.py
│   │       └── app.py
│   └── test/                              # 核心测试，不与源码包同名
│       ├── TradingSchedulerTests.py
│       ├── strategy/ETFRotationStrategyImplTests.py
│       ├── market/AKShareMarketTests.py
│       ├── repository/PapAccountRepositoryTests.py
│       ├── execution/PapExecutorTests.py
│       └── AppTests.py
├── AGENTS.md
└── README.md
```

## 快速开始

要求 Python 3.12+。以下命令均从项目根目录执行。

```bash
python -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt
```

启动后端和每日调度服务（默认端口 `8001`）：

```bash
backend/.venv/bin/python backend/main.py
```

可通过 `--host`、`--port` 调整监听地址与端口。策略配置从 `backend/dev/market_trading_bot/config/*_strategy.json` 加载，修改后重启生效。

从旧版目录升级时，先停止服务，将原 `backend/data/`、`backend/logs/` 内容迁入 `backend/dev/data/`、`backend/dev/logs/`，保留账本与历史日志。配置位于 `backend/dev/market_trading_bot/config/`；这些路径不依赖启动时的工作目录。

## 配置

全局配置位于 `backend/dev/market_trading_bot/config/global.json`：

| 配置 | 用途 |
|---|---|
| `market` | 行情源、请求超时与间隔、报价最大延迟、历史回溯天数 |
| `storage.data_dir` | SQLite 账本与 CSV 目录 |
| `logging` | 日志目录、级别和保留日期数 |
| `runtime.trade_timeout_seconds` | 交易执行超时阈值，默认 300 秒，仅标记异常 |
| `pushplus` | 预留配置，当前不发送推送 |

策略配置位于 `backend/dev/market_trading_bot/config/etf_rotation_strategy.json`：

| 配置 | 默认值 | 用途 |
|---|---|---|
| `enable` | `true`（省略时默认启用） | 是否为此策略创建 TradingTask 线程 |
| `strategy` | `etf_rotation` | 策略类型 |
| `instances` | `etf_rotation_pap`；开户资金 100,000 元；佣金率 0.02%；最低佣金 5 元；滑点 0 | 模拟账户与交易费用 |
| `parameters.etf_pool` | `518880.XSHG`、`513100.XSHG`、`159915.XSHE`、`510180.XSHG` | ETF 池 |
| `parameters.momentum_price_points` | `25` | 动量价格点数量 |
| `parameters.volatility_price_points` | `20` | 波动率价格点数量 |
| `parameters.target_annual_volatility` | `0.15` | 目标年化波动率 15% |
| `parameters.annualization_days` | `250` | 年化交易日数量 |
| `parameters.select_num` | `1` | 选择排名第一的 ETF |
| `instruments` | 每手 100 份；黄金 ETF、纳指 ETF 为 T+0，创业板 ETF、上证 180 ETF 为 T+1 | 交易单位和卖出规则 |
| `schedule.trade_time` | `14:30` | 北京时间自动调仓时间 |
| `schedule.report_time` | `15:15` | 收盘日报时间，当前尚未实现 |

`initial_cash` 仅在首次开户时使用并保存到账本；后续修改配置不会改变账户基准。PushPlus Token 等凭据不要提交到仓库。

## 账户数据

`data/` 不作为预置目录提交，运行时按配置自动创建。每个实例的数据保存在 `backend/dev/data/<instance_id>/`：

- `ledger.sqlite3`：权威账本，保存全部运行结果、账户状态、信号、订单和成交。
- `account_trades.csv`：可重建的查看副本，仅记录发生实际成交的日期。

账户更新和当日运行结果在同一 SQLite 事务中提交。同一实例同一北京时间日期只提交一次；并发或重复运行会返回首次结果。CSV 导出失败不会回滚已提交账本，再次运行可补齐导出。

请备份整个实例目录，不要通过修改 CSV 调整账户。更换 ETF 池前必须先处理已有持仓，否则缺少持仓行情时运行会停止。

### CSV 字段

| 字段 | 含义 |
|---|---|
| `date` | 交易日期 |
| `buy_etf` / `buy_quantity` / `buy_price` | 买入标的、实际数量和成交价 |
| `sell_etf` / `sell_quantity` / `sell_price` | 卖出标的、实际数量和成交价 |
| `commission` | 当次佣金合计 |
| `holding_etf` / `holding_quantity` / `holding_price` | 交易后持仓、数量和估值价 |
| `market_value` / `cash` / `total_assets` | 持仓市值、现金和总资产 |
| `initial_cash` | 实际开户资金 |
| `cumulative_pnl` | 总资产减开户资金 |
| `cumulative_return_pct` | 累计收益率，单位为百分比 |
| `net_value` | 总资产除以开户资金 |

多笔成交或持仓使用分号按顺序对齐。CSV 的资产值采用调仓时估值，且不记录无成交日期，因此不是每日净值曲线。完整明细以 SQLite 为准。

## 日志

日志写入 `backend/dev/logs/<strategy>/YYYY-MM-DD.log`，默认保留最近 30 个有日志的日期。日志包含运行状态、行情区间、报价、策略排名、目标权重和模拟成交摘要，不记录 Token。

## 定时执行规则

- `TradingScheduler` 每天北京时间 **9:00** 加载交易日历，失败每隔 5 秒重试，停止服务或到 15:00 时结束重试。
- 确认当天为交易日后，按各策略 `schedule.trade_time`（默认 `14:30`）注册当天执行时间；休市日不注册、不创建任务。
- 9:00 后启动服务会立即补做当天的交易日判断，只安排尚未到时间的策略，不补执行已错过的任务。
- `enable` 默认为 `true`，设为 `false` 不安排该策略。到点创建一次性的 `TradingTask` 线程，只执行策略和更新自身状态，完成后退出。
- 同一策略的账户串行执行，不同策略互不阻塞。任务不负责交易日判断或每日调度，账户账本继续防止同日重复成交。
- 任务状态存储在 `backend/dev/data/runtime/<strategy>/runtime.sqlite3`，页面显示当日计划执行时间、最近执行结果和交易日判断状态。配置修改后重启生效。

## 网页运行监控

Vue 3 前端 + FastAPI 后端，支持运行状态、策略与账户概览、按日期查看和刷新日志。
账户展示最近调仓估值，非实时估值；收盘日报暂未实现。

### 端口与启动

以下命令从项目根目录执行，需要 Python 3.12+、Node.js 20.19+ 或 22.12+。

首次安装：

```bash
backend/.venv/bin/python -m pip install -r backend/requirements.txt
npm ci --prefix frontend
```

开发时分别在两个终端启动：

```bash
# 后端：8001，自动注册每日任务
backend/.venv/bin/python backend/main.py

# 前端：8000，浏览器打开 http://127.0.0.1:8000
npm run dev --prefix frontend -- --port 8000 --strictPort
```

前端将 `/api` 请求代理到后端 `8001`。调度服务每 5 秒更新自身心跳；执行期间页面显示任务线程的心跳，任务结束后显示等待下次执行。任务线程在策略执行前后更新心跳，不另外创建心跳线程。
待命超过 20 秒未更新心跳判定失联；交易执行超时由 `runtime.trade_timeout_seconds` 配置，默认 300 秒。
超时只标记异常，不会强制中断或重试交易。
后端仅启动一个实例；启动后按上述定时规则自动调仓。服务正常关闭时等待正在执行的交易结束，不再启动新任务。

部署时执行 `npm run build --prefix frontend`，只需启动一个后端服务，
通过 `http://127.0.0.1:8001` 同时访问页面与 API，无需启动前端开发服务。

### API

以下均为只读 `GET` 接口：

| 路径 | 功能 |
|---|---|
| `/api/runtime?strategy_id=etf_rotation` | 后台交易线程状态、心跳、各账户当日计划执行时间与最近结果 |
| `/api/strategies` | 策略与账户实例列表 |
| `/api/accounts/{instance_id}` | 最近账户资产与持仓 |
| `/api/strategies/{strategy_id}/logs` | 日志日期列表，按日期倒序 |
| `/api/strategies/{strategy_id}/logs/{date}?offset=0` | 指定日期日志，返回内容及下一页偏移量 |

日志日期格式为 `YYYY-MM-DD`；日志按策略归档。页面为只读，交易结果通过账户与日志查看。

## 验证

```bash
PYTHONPATH=backend/dev backend/.venv/bin/python -m unittest discover -s backend/test -p '*Tests.py'
npm run build --prefix frontend
```
