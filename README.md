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
│   │       ├── TaskScheduler.py           # 交易日判断与任务调度
│   │       ├── task/                      # 调度器管理的一次性任务
│   │       │   ├── TradingTask.py          # 单次策略执行及状态更新
│   │       │   ├── DailyValuationTask.py  # 15:05 更新收盘估值
│   │       │   └── DailySummaryTask.py    # 15:10 汇总并发送日报
│   │       ├── strategy/                  # 策略实现
│   │       │   ├── ETFRotationStrategyImpl.py
│   │       │   └── StrategyData.py          # StrategyData、RankedETF
│   │       ├── repository/                # 数据库访问
│   │       │   ├── TaskStatusRepository.py
│   │       │   └── PapAccountRepository.py
│   │       ├── log/StrategyLogger.py      # 日志处理代码
│   │       ├── market/                    # 行情适配及数据
│   │       │   ├── AKShareMarket.py
│   │       │   └── MarketData.py            # MarketData、DailyClose
│   │       ├── trader/PapTrader.py
│   │       └── app.py
│   └── test/                              # 核心测试，不与源码包同名
│       ├── TaskSchedulerTests.py
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
| `runtime.test_mode` | 默认 `false`；开启后允许任意时段使用最近收盘数据测试，自动隔离测试数据和日志 |
| `runtime.trade_timeout_seconds` | 交易执行超时阈值，默认 300 秒，仅标记异常 |
| `pushplus.token` | PushPlus 消息 Token，报告生成后直接发送 |

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

`initial_cash` 仅在首次开户时使用并保存到账本；后续修改配置不会改变账户基准。PushPlus Token 配置在 `global.json` 的 `pushplus.token` 中。

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

- `TaskScheduler` 每天北京时间 **9:00** 加载交易日历，失败每隔 5 秒重试，停止服务或到 15:00 时结束重试。
- 确认当天为交易日后，按各策略 `schedule.trade_time`（默认 `14:30`）注册当天执行时间；休市日不注册、不创建任务。
- 9:00 后启动服务会立即补做当天的交易日判断，只安排尚未到时间的策略，不补执行已错过的任务。
- `enable` 默认为 `true`，设为 `false` 不安排该策略。到点为每个启用的策略账户创建独立 `TradingTask` 线程，完成后退出。
- 一个策略＋一个账户对应一个 Task；账户间独立执行，同一账户不重叠。调度器为每个 Task 构建独立策略、行情、模拟执行器及 Repository 对象，Task 不读取配置、不构建业务依赖。
- 任务状态存储在 `backend/dev/data/runtime/<strategy>/<instance_id>/runtime.sqlite3`，页面显示当日计划执行时间、最近执行结果和交易日判断状态。配置修改后重启生效。

交易执行依赖关系：

```text
TaskScheduler → TradingTask（绑定策略＋账户）
                    ├── Market → MarketData → Strategy → StrategyData
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

### 收盘后测试交易链路

在 `global.json` 中将 `runtime.test_mode` 设为 `true`，再将策略配置的 `schedule.trade_time` 改为几分钟后的北京时间。例如当前 19:23，可设为：

```json
"schedule": {
  "trade_time": "19:30"
}
```

完成配置后执行 `docker compose up -d --build`，并在设定时间之前启动服务。无需将系统时间改成交易时间；测试模式支持收盘后、周末和节假日。已经错过的时间不会补执行，需要重新设置未来时间并重启。

测试到点后执行真实的策略计算、模拟调仓、SQLite 记账、CSV 导出和执行报告推送。行情使用最近已完成交易日的不复权收盘价，以及该日之前的前复权历史；当天尚未收盘时使用上一交易日的数据。行情日志会注明来源日期，数据缺失或请求失败时记录失败，不伪造成交。

测试账本及任务状态自动保存到配置数据目录的 `test/` 子目录，日志保存到配置日志目录的 `test/` 子目录，例如 `data/test/`、`logs/test/`。前端显示“链路测试模式”，账户和日志接口自动读取测试目录。测试模式只执行交易链路，跳过固定的 15:05 收盘估值和 15:10 汇总日报。

部署后先在页面确认“当日计划执行”，到点后刷新账户、日志日期并打开当天日志，可依次看到任务开始、历史行情校验、行情来源日期、策略排名和任务完成。失败时同样能通过日志和最近任务结果定位。

同一测试账户当天已有记账结果时仍会复用；需要当天重新计算整条链路时，修改测试账户的 `id` 后重启。测试完成后将 `runtime.test_mode` 改回 `false`，将 `trade_time` 恢复为 `14:30` 并重启，页面将重新读取原账户和日志。

### 微信报告（PushPlus）

| 报告 | 北京时间 | 内容 |
|---|---|---|
| 交易执行报告 | 交易日 14:30 策略执行结束后 | 成交、持仓、执行时资产和累计收益；无成交也发送 |
| 每日收益总结 | 每天 15:10 | 一条消息按策略汇总所有启用的模拟账户，包含收盘资产、当日及累计收益、持仓和买卖笔数；休市日展示最近有效估值 |

定时任务统一由外层 `TaskScheduler` 注册并管理，任务类集中在 `task/`：
`TradingTask` 执行交易，`DailyValuationTask` 每天 15:05 更新收盘估值，`DailySummaryTask` 每天 15:10 只读汇总并发送。
调度器统一获取并缓存当天日历，将是否交易日、上一交易日传给日报任务；任务不重复获取日历。
报告为纯文本，由 `notification/report/` 中的 `TradeExecutionReport`、`DailyAccountReport` 生成。
累计收益以账本实际开户资金为基准，包含交易费用。当日收益比较上一交易日收盘资产；首次开户以开户资金为基准。
收盘快照缺失时按对应日期的历史持仓重建，不将跨日收益当作当日收益。估值使用不复权收盘价和现有模拟账本，不新增分红或份额调整处理。

每个账户使用独立的 `data/<instance_id>/ledger.sqlite3`，仅含两张表：
`account` 保存账户、策略、开户资金和创建时间；`daily_results` 每天一行，分别保存交易结果、收盘估值、收益和估值状态。
旧 `runs` 表首次写入时在事务中自动迁移，保留交易及开户资金；旧账户无法还原的创建时间留空。估值只更新收盘字段，不覆盖交易结果。
收盘价尚未更新、日历异常或任务仍在执行时，间隔 30 秒最多尝试 3 次；15:10 汇总时按保存的状态标注未执行、执行中或失败，不影响其他账户；不使用旧价格代替当天收盘价。
未开户发送“暂无收益数据”；当天策略未完成记账时，按现有持仓估值并注明。

15:05 后启动先补估值；已过 15:10 时等待补估值结束再发送当天日报，不补发历史日期；同一进程内每天执行一次，重启后可能再次发送。
配置位于 `global.json` 的 `pushplus`。消息直接发送、不保存；网络或服务端临时故障最多尝试 3 次，间隔 1、2 秒，配置错误不重试。
最终发送失败仅记录日志，不影响交易结果；超时重试可能重复送达。

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
