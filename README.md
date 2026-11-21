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
├── frontend/                              # 前端，开发端口8080
├── backend/
│   ├── main.py                            # 后端启动入口，默认端口8081
│   ├── requirements.txt
│   ├── dev/                               # 后端源码和运行资源，生产环境同样使用
│   │   ├── logs/                          # 运行日志，启动后生成
│   │   └── market_trading_bot/
│   │       ├── config/                    # 全局与策略配置
│   │       ├── TradingScheduler.py        # 交易日判断与任务调度
│   │       ├── data/                      # 共享数据结构，不依赖业务模块
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
│   │       ├── trader/PapTrader.py
│   │       └── app.py
│   └── test/                              # 核心测试，不与源码包同名
│       ├── TradingSchedulerTests.py
│       ├── strategy/ETFRotationStrategyImplTests.py
│       ├── market/AKShareMarketTests.py
│       ├── repository/PapAccountRepositoryTests.py
│       ├── trader/PapTraderTests.py
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

启动后端和每日调度服务（默认端口 `8081`）：

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
| `pushplus.enabled` | 是否启用执行报告推送，默认关闭；Token 使用环境变量 `PUSHPLUS_TOKEN` |

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
- `enable` 默认为 `true`，设为 `false` 不安排该策略。到点为每个启用的策略账户创建独立 `TradingTask` 线程，完成后退出。
- 一个策略＋一个账户对应一个 Task；账户间独立执行，同一账户不重叠。调度器为每个 Task 构建独立策略、行情、模拟执行器及 Repository 对象，Task 不读取配置、不构建业务依赖。
- 任务状态存储在 `backend/dev/data/runtime/<strategy>/<instance_id>/runtime.sqlite3`，页面显示当日计划执行时间、最近执行结果和交易日判断状态。配置修改后重启生效。

交易执行依赖关系：

```text
TradingScheduler → TradingTask（绑定策略＋账户）
                    ├── Market → Strategy → StrategyResult
                    ├── PapTrader：纯内存模拟调仓
                    ├── PapAccountRepository：账户事务与CSV导出
                    └── TaskStatusRepository：该账户任务状态
```

Repository 不依赖执行器。Task 在账户事务内读取最新快照、检查当日结果、调用执行器并保存；任一步失败整体回滚，保证并发防重复与资金一致性。
网页选择账户后读取对应任务状态；未指定账户查询时汇总整个策略，任一运行账户超时都会显示异常。

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
# 后端：8081，自动注册每日任务
backend/.venv/bin/python backend/main.py

# 前端：8080，浏览器打开 http://127.0.0.1:8080
npm run dev --prefix frontend -- --port 8080 --strictPort
```

前端将 `/api` 请求代理到后端 `8081`。调度服务每 5 秒更新自身心跳；执行期间页面显示任务线程的心跳，任务结束后显示等待下次执行。任务线程在策略执行前后更新心跳，不另外创建心跳线程。
待命超过 20 秒未更新心跳判定失联；交易执行超时由 `runtime.trade_timeout_seconds` 配置，默认 300 秒。
超时只标记异常，不会强制中断或重试交易。
后端仅启动一个实例；启动后按上述定时规则自动调仓。服务正常关闭时等待正在执行的交易结束，不再启动新任务。

部署时执行 `npm run build --prefix frontend`，只需启动一个后端服务，
通过 `http://127.0.0.1:8081` 同时访问页面与 API，无需启动前端开发服务。

### Docker 部署（Azure VM）

在项目根目录执行，首次部署和更新使用同一命令：

```bash
docker compose up -d --build
docker compose ps
curl -f http://127.0.0.1:8081/api/strategies
```

镜像自动构建前端，由单个 Python 服务提供页面、API 和定时模拟交易，默认监听本机 `127.0.0.1:8081`。
使用 `deploy/nginx.conf` 配置 Nginx 转发 `8080 → 8081`；安装前备份现有站点，执行 `nginx -t` 通过后 reload。
Azure 网络安全组放行 TCP `8080` 后，访问 `http://<服务器公网IP>:8080`。

- 配置：修改 `backend/dev/market_trading_bot/config` 后，执行 `docker compose restart trading` 生效。
- 数据：账本和日志保存在命名卷，重建或停止容器均保留；`docker compose down -v` 会删除数据。已有本地账本需停服后手动迁移。
- 运维：容器以非 root 用户运行，自动重启并轮转容器日志；只运行一个实例。
- 排查：`docker compose logs --tail=100 -f trading` 查看日志，`/api/runtime` 查看调度与交易状态；容器健康检查仅验证 API 响应。
- 停止：执行 `docker compose down`。停止或更新时最多等待 2 分钟，超时会强制结束进程，宜在无交易任务运行时操作。

### 微信执行报告（PushPlus）

1. 在 [PushPlus](https://www.pushplus.plus/push1.html) 登录、关注微信服务号并完成实名认证。
2. 创建专用消息 Token，在网页“试一试”中选择微信渠道，确认能收到消息。
3. 在项目根目录的 `.env` 文件中配置 `PUSHPLUS_TOKEN=你的消息Token`（文件已被 Git 和 Docker 构建忽略，勿提交凭据）。
4. 将 `backend/dev/market_trading_bot/config/global.json` 中的 `pushplus.enabled` 设为 `true`，执行 `docker compose up -d --build`。

本地运行时需自行设置 `PUSHPLUS_TOKEN` 环境变量，Python 不自动加载 `.env`。
后续修改 `.env` 后执行 `docker compose up -d` 重建容器，单纯 restart 不会更新容器环境变量。

每个账户在策略执行结束后发送一份模拟交易报告，无成交也发送；包括成交、持仓、执行时资产及累计收益，失败则提示查看日志。
累计收益以账本实际开户资金为基准，包含交易费用；不是收盘收益。休市日不执行策略，本阶段不发送休市通知或 15:05 日报。

报告保存在数据卷的 `runtime/notifications.sqlite3`，每 5 秒处理一条，网络或服务端临时故障最多尝试 3 次，间隔为 60、120 秒；配置错误不重试。
按策略、账户、计划日期和消息类型去重，重启不会重置发送次数；接口受理成功不等于微信已送达，网络超时后重试可能重复送达。
交易账本与通知独立提交，推送失败不会回滚或重跑交易；进程在账本提交后、消息入队前崩溃时，可能漏报。
Token 缺失时记录错误并停用通知，交易继续运行。使用 `docker compose logs --tail=100 trading` 排查推送异常。

### API

以下均为只读 `GET` 接口：

| 路径 | 功能 |
|---|---|
| `/api/runtime?strategy_id=etf_rotation&instance_id=etf_rotation_pap` | 后台交易线程状态、心跳、各账户当日计划执行时间与最近结果 |
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
