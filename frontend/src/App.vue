<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
type AccountConfig = { id: string; mode: string; enabled: boolean }
type Strategy = { id: string; enable: boolean; instances: AccountConfig[] }
type ScheduledTask = { instance_id: string; next_execution_at?: string; last_execution?: { state: string; error?: string; scheduled_at: string } }
type Runtime = { online: boolean; state: string; health?: string; heartbeat_scope?: string; is_trading_day?: boolean | null; scheduler_enabled?: boolean; scheduler_error?: string; scheduled_tasks?: ScheduledTask[]; started_at?: string; heartbeat_at?: string; uptime_seconds?: number; last_execution?: { success: boolean; error?: string; finished_at: string } }
type Account = { initialized: boolean; as_of?: string; date?: string; cash?: string; equity?: string; initial_cash?: string; positions?: Record<string, { quantity: number }> }
type Valuation = { date: string; daily_pnl?: string; daily_return_pct?: string; cumulative_return_pct: string }
type Fill = { fill_id: string; date: string; symbol: string; side: 'buy' | 'sell'; quantity: number; price: string; amount: string; commission: string }
type Candle = { date: string; open: number; high: number; low: number; close: number }
type CandleResponse = { symbols: string[]; series: Record<string, { candles: Candle[]; updated_at: string | null; error: string | null }> }
const runtime = ref<Runtime | null>(null), runtimeError = ref(''), error = ref('')
const strategies = ref<Strategy[]>([]), strategyId = ref(''), accountId = ref('')
const account = ref<Account | null>(null), logDates = ref<string[]>([]), selectedDay = ref('')
const valuations = ref<Valuation[]>([])
const fills = ref<Fill[]>([]), fillsError = ref(''), loadingFills = ref(false), hasMoreFills = ref(false)
const today = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
const todayValuation = computed(() => valuations.value.find(item => item.date === today() && item.daily_pnl != null))
const chartValues = computed(() => valuations.value.filter(item => item.cumulative_return_pct != null && Number.isFinite(Number(item.cumulative_return_pct))))
const chartPoints = computed(() => {
  const values = chartValues.value.map(item => Number(item.cumulative_return_pct))
  if (!values.length) return ''
  const min = Math.min(...values), max = Math.max(...values), range = max - min || 1
  return values.map((value, index) => `${values.length === 1 ? 50 : 4 + index * 92 / (values.length - 1)},${max === min ? 50 : 90 - (value - min) * 80 / range}`).join(' ')
})
const logText = ref(''), offset = ref(0), hasMore = ref(false), loadingLog = ref(false)
const dateList = ref<HTMLElement | null>(null)
const candleData = ref<CandleResponse | null>(null), hiddenSymbols = ref<string[]>([]), candleError = ref('')
const candleColors = ['#287bba', '#dd9143', '#8a68be', '#319b86', '#cd6b80', '#78844e']
const visibleCandles = computed(() => (candleData.value?.symbols ?? []).filter(symbol => !hiddenSymbols.value.includes(symbol)))
const candleChart = computed(() => {
  const series = candleData.value?.series ?? {}
  const dates = [...new Set(visibleCandles.value.flatMap(symbol => (series[symbol]?.candles ?? []).map(item => item.date)))].sort()
  const items = visibleCandles.value.flatMap(symbol => (series[symbol]?.candles ?? []).map(item => ({ ...item, symbol })))
  if (!dates.length || !items.length) return null
  const min = Math.min(...items.map(item => item.low)), max = Math.max(...items.map(item => item.high))
  const padding = Math.max((max - min) * .08, max * .005), bottom = min - padding, top = max + padding
  const y = (value: number) => 20 + (top - value) / (top - bottom) * 260
  const step = 840 / dates.length, width = Math.max(1, Math.min(7, step / Math.max(visibleCandles.value.length, 1) * .8))
  return { dates, ticks: [top, (top + bottom) / 2, bottom], y, width,
    items: items.map(item => ({ ...item, x: 60 + (dates.indexOf(item.date) + .5) * step +
      (visibleCandles.value.indexOf(item.symbol) - (visibleCandles.value.length - 1) / 2) * width,
      color: candleColors[(candleData.value?.symbols.indexOf(item.symbol) ?? 0) % candleColors.length] })) }
})
let candleRequestId = 0
async function refreshCandles() {
  const requestId = ++candleRequestId
  if (!strategyId.value) return
  try {
    const result = await fetchJson<CandleResponse>(`/api/strategies/${encodeURIComponent(strategyId.value)}/candles`)
    if (requestId === candleRequestId) { candleData.value = result; candleError.value = '' }
  } catch { if (requestId === candleRequestId) candleError.value = 'K线数据加载失败，请稍后重试。' }
}
function toggleSymbol(symbol: string) {
  hiddenSymbols.value = hiddenSymbols.value.includes(symbol) ? hiddenSymbols.value.filter(item => item !== symbol) : [...hiddenSymbols.value, symbol]
}
const scheduledTask = computed(() => runtime.value?.scheduled_tasks?.find(task => task.instance_id === accountId.value))
const taskLabel = (state: string) => ({ success: '成功', failed: '失败', running: '执行中' }[state] ?? state)
const accounts = computed(() => strategies.value.find(config => config.id === strategyId.value)?.instances ?? [])
const statusLabel = computed(() => {
  const status = runtime.value
  if (!status) return '状态未知'
  if (status.state === 'disabled') return '策略已停用'
  if (status.scheduler_error) return '调度异常'
  if (status.health === 'trade_timeout') return '交易执行超时'
  if (status.online) return status.state === 'running' ? '执行中' : '正常待命'
  return { unknown: '尚无心跳', stopped: '已停止' }[status.state] ?? '心跳中断'
})
const formatMoney = (value: unknown) => value == null ? '—' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const formatTime = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'
const uptime = computed(() => runtime.value?.uptime_seconds == null ? '—' : `${Math.floor(runtime.value.uptime_seconds / 3600)} 小时 ${Math.floor(runtime.value.uptime_seconds / 60) % 60} 分钟 ${Math.floor(runtime.value.uptime_seconds) % 60} 秒`)
let runtimeRequestId = 0
async function refreshRuntime() {
  const requestId = ++runtimeRequestId
  if (!strategyId.value) return
  try {
    const result = await fetchJson<Runtime>(`/api/runtime?strategy_id=${encodeURIComponent(strategyId.value)}${accountId.value ? `&instance_id=${encodeURIComponent(accountId.value)}` : ''}`)
    if (requestId === runtimeRequestId) { runtime.value = result; runtimeError.value = '' }
  } catch {
    if (requestId === runtimeRequestId) { runtime.value = null; runtimeError.value = '监控接口无法连接，请检查 Web 服务。' }
  }
}
let accountRequestId = 0, datesRequestId = 0, logRequestId = 0
async function refreshAccount() {
  const requestId = ++accountRequestId
  account.value = null; valuations.value = []; fills.value = []; fillsError.value = ''; loadingFills.value = false; hasMoreFills.value = false
  if (!accountId.value) return
  void loadFills(requestId)
  void fetchJson<Valuation[]>(`/api/accounts/${encodeURIComponent(accountId.value)}/valuations`)
    .then(result => { if (requestId === accountRequestId) valuations.value = result })
    .catch(cause => { if (requestId === accountRequestId) error.value = String(cause) })
  try { const result = await fetchJson<Account>(`/api/accounts/${encodeURIComponent(accountId.value)}`); if (requestId === accountRequestId) account.value = result }
  catch (cause) { if (requestId === accountRequestId) error.value = String(cause) }
}
async function loadFills(requestId = accountRequestId) {
  if (!accountId.value || loadingFills.value) return
  loadingFills.value = true; fillsError.value = ''
  try {
    const result = await fetchJson<{ items: Fill[]; has_more: boolean }>(`/api/accounts/${encodeURIComponent(accountId.value)}/fills?offset=${fills.value.length}`)
    if (requestId === accountRequestId) { fills.value = [...fills.value, ...result.items]; hasMoreFills.value = result.has_more }
  } catch { if (requestId === accountRequestId) fillsError.value = '操作记录加载失败，请重试。' }
  finally { if (requestId === accountRequestId) loadingFills.value = false }
}
async function refreshDates() {
  const requestId = ++datesRequestId
  if (!strategyId.value) return
  try {
    const result = await fetchJson<string[]>(`/api/strategies/${encodeURIComponent(strategyId.value)}/logs`)
    if (requestId !== datesRequestId) return
    logDates.value = result
    if (!result.length) {
      ++logRequestId; selectedDay.value = ''; logText.value = ''; offset.value = 0; hasMore.value = false; loadingLog.value = false
    } else if (!result.includes(selectedDay.value)) {
      void loadLog(result[0]!)
    }
  }
  catch (cause) { if (requestId === datesRequestId) error.value = String(cause) }
}
function scrollDates(direction: number) {
  dateList.value?.scrollBy({ left: direction * dateList.value.clientWidth, behavior: 'smooth' })
}
async function loadLog(day: string, append = false) {
  const requestId = ++logRequestId
  selectedDay.value = day; loadingLog.value = true; error.value = ''
  if (!append) { logText.value = ''; offset.value = 0; hasMore.value = false }
  try {
    const result = await fetchJson<{ content: string; next_offset: number; has_more: boolean }>(`/api/strategies/${encodeURIComponent(strategyId.value)}/logs/${day}?offset=${append ? offset.value : 0}`)
    if (requestId !== logRequestId) return
    logText.value = append ? logText.value + result.content : result.content
    offset.value = result.next_offset; hasMore.value = result.has_more
  } catch (cause) { if (requestId === logRequestId) error.value = String(cause) }
  finally { if (requestId === logRequestId) loadingLog.value = false }
}
watch(strategyId, () => {
  runtime.value = null; runtimeError.value = ''; void refreshRuntime()
  ++logRequestId; selectedDay.value = ''; logText.value = ''; hasMore.value = false; loadingLog.value = false; logDates.value = []; error.value = ''
  accountId.value = accounts.value[0]?.id ?? ''
  void refreshDates()
  candleData.value = null; hiddenSymbols.value = []; candleError.value = ''; void refreshCandles()
})
watch(accountId, () => { runtime.value = null; void refreshRuntime(); void refreshAccount() })
let timer: ReturnType<typeof setInterval> | undefined, candleTimer: ReturnType<typeof setInterval> | undefined
onMounted(async () => {
  timer = setInterval(refreshRuntime, 5000)
  candleTimer = setInterval(refreshCandles, 30000)
  try { strategies.value = await fetchJson<Strategy[]>('/api/strategies'); strategyId.value = strategies.value[0]?.id ?? '' }
  catch (cause) { error.value = String(cause) }
})
onUnmounted(() => { clearInterval(timer); clearInterval(candleTimer); ++runtimeRequestId; ++logRequestId; ++accountRequestId; ++datesRequestId; ++candleRequestId })
async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) throw new Error(`请求失败（${response.status}）`)
  return response.json()
}
</script>

<template>
  <main>
    <header><div><p class="eyebrow">MARKET TRADING BOT</p><h1>HY的量化交易监控室</h1></div><span class="badge" :class="{ online: runtime?.online }">{{ statusLabel }}</span></header>
    <p v-if="runtimeError" class="error" role="alert">{{ runtimeError }}</p>
    <section><h2>运行状况</h2><div class="metrics"><div><span>程序启动时间</span><strong>{{ formatTime(runtime?.started_at) }}</strong></div><div><span>本次运行时长</span><strong>{{ uptime }}</strong></div><div><span>最近心跳 · {{ runtime?.heartbeat_scope === 'task' ? '当前任务' : '调度服务' }}</span><strong>{{ formatTime(runtime?.heartbeat_at) }}</strong></div></div><p class="muted">时间均为北京时间。定时任务按交易日执行，待命表示调度服务运行，策略任务到点创建线程，执行完退出。</p><p v-if="runtime?.is_trading_day === false" class="muted">今日休市，不创建策略任务。</p><p v-else-if="runtime?.scheduler_enabled && runtime?.is_trading_day == null" class="muted">等待交易日确认，每天 9:00 检查。</p><p v-if="runtime?.scheduler_error" class="error">日历加载异常：{{ runtime.scheduler_error }}</p><p v-if="runtime?.last_execution">最近任务：<span :class="{ error: !runtime.last_execution.success }">{{ runtime.last_execution.success ? '成功' : '失败' }}</span> · {{ formatTime(runtime.last_execution.finished_at) }} {{ runtime.last_execution.error }}</p></section>
    <section><div class="section-title"><h2>策略与账户</h2><button @click="refreshAccount">刷新账户</button></div><div class="selectors"><label>策略<select v-model="strategyId"><option v-for="config in strategies" :key="config.id" :value="config.id">{{ config.id }}{{ config.enable ? '' : ' · 已停用' }}</option></select></label><label>账户<select v-model="accountId"><option v-for="item in accounts" :key="item.id" :value="item.id">{{ item.id }} · {{ item.mode }} · {{ item.enabled ? '已启用' : '已停用' }}</option></select></label></div>
    <div v-if="scheduledTask"><p>当日计划执行：{{ scheduledTask.next_execution_at ? formatTime(scheduledTask.next_execution_at) : '暂无计划' }}</p><p v-if="scheduledTask.last_execution">最近调度：{{ formatTime(scheduledTask.last_execution.scheduled_at) }} · {{ taskLabel(scheduledTask.last_execution.state) }} {{ scheduledTask.last_execution.error }}</p></div>
    <p v-if="!accountId" class="muted">暂无账户实例</p><p v-else-if="account && !account.initialized" class="muted">暂无账户记录，首次模拟调仓后显示。</p><template v-else-if="account?.initialized"><div class="metrics"><div><span>总资产</span><strong>¥ {{ formatMoney(account.equity) }}</strong></div><div><span>可用现金</span><strong>¥ {{ formatMoney(account.cash) }}</strong></div><div><span>持仓市值</span><strong>¥ {{ formatMoney(Number(account.equity) - Number(account.cash)) }}</strong></div><div><span>累计收益</span><strong>{{ account.initial_cash ? `${formatMoney((Number(account.equity) / Number(account.initial_cash) - 1) * 100)}%` : '—' }}</strong></div><div><span>今日收益 · 收盘估值</span><strong v-if="todayValuation" :class="{ gain: Number(todayValuation.daily_pnl) > 0, loss: Number(todayValuation.daily_pnl) < 0 }"><span class="pnl-arrow" v-if="Number(todayValuation.daily_pnl) !== 0" aria-hidden="true">{{ Number(todayValuation.daily_pnl) > 0 ? '▲' : '▼' }}</span>¥ {{ formatMoney(todayValuation.daily_pnl) }} <small v-if="todayValuation.daily_return_pct != null">({{ formatMoney(todayValuation.daily_return_pct) }}%)</small></strong><strong v-else>—</strong></div></div><p class="muted">今日收益基于当日收盘估值，估值完成后显示。</p><div class="return-chart"><div class="chart-heading"><h3>累计收益曲线</h3><span>基于每日收盘估值</span></div><template v-if="chartValues.length"><div class="chart-summary"><strong>{{ formatMoney(chartValues[chartValues.length - 1]?.cumulative_return_pct) }}%</strong><span>最新累计收益</span></div><svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="累计收益率曲线"><line x1="4" y1="90" x2="96" y2="90" /><line x1="4" y1="50" x2="96" y2="50" /><line x1="4" y1="10" x2="96" y2="10" /><polyline v-if="chartValues.length > 1" :points="chartPoints" /><circle v-else cx="50" cy="50" r="1.2" /></svg><div class="chart-dates"><span>{{ chartValues[0]?.date }}</span><span>{{ chartValues[chartValues.length - 1]?.date }}</span></div></template><p v-else class="muted">暂无收盘估值数据</p></div><p class="muted">最近调仓估值：{{ account.as_of ? formatTime(account.as_of) : account.date }}。资产并非实时行情估值。</p><table><thead><tr><th>持仓标的</th><th>数量</th></tr></thead><tbody><tr v-for="(position, symbol) in account.positions" :key="symbol"><td>{{ symbol }}</td><td>{{ position.quantity }}</td></tr></tbody></table><p v-if="!Object.keys(account.positions ?? {}).length" class="muted">当前空仓</p><div class="trade-history"><div class="trade-heading"><h3>操作记录</h3><span>模拟成交 · 按日期倒序</span></div><p v-if="fillsError" class="error">{{ fillsError }}</p><p v-if="loadingFills && !fills.length" class="muted">正在加载操作记录…</p><p v-else-if="!fills.length && !fillsError" class="muted">暂无成交记录</p><div v-if="fills.length" class="trade-table"><table><thead><tr><th>日期</th><th>方向</th><th>标的</th><th>数量</th><th>成交价</th><th>成交金额</th><th>佣金</th></tr></thead><tbody><tr v-for="fill in fills" :key="fill.date + fill.fill_id"><td>{{ fill.date }}</td><td><span class="trade-side" :class="fill.side">{{ fill.side === 'buy' ? '买入' : '卖出' }}</span></td><td>{{ fill.symbol }}</td><td>{{ fill.quantity.toLocaleString() }}</td><td>¥ {{ fill.price }}</td><td>¥ {{ formatMoney(fill.amount) }}</td><td>¥ {{ formatMoney(fill.commission) }}</td></tr></tbody></table></div><button v-if="hasMoreFills || (fillsError && !fills.length)" class="load-fills" :disabled="loadingFills" @click="loadFills()">{{ loadingFills ? '加载中…' : fillsError ? '重试' : '加载更多' }}</button></div></template></section>
    <section class="candles-section">
      <div class="candle-heading"><div><h2>策略标的</h2><p class="muted">最近 90 个自然日 · 前复权日 K · 仅展示已完成交易日</p></div><div class="candle-legend" aria-label="选择显示的标的"><button v-for="(symbol, index) in candleData?.symbols ?? []" :key="symbol" :class="{ mutedSymbol: hiddenSymbols.includes(symbol) }" :aria-pressed="!hiddenSymbols.includes(symbol)" @click="toggleSymbol(symbol)"><i :style="{ background: candleColors[index % candleColors.length] }"></i>{{ symbol }}</button></div></div>
      <p v-if="candleError" class="error">{{ candleError }}</p>
      <p v-for="symbol in candleData?.symbols ?? []" :key="symbol" v-show="candleData?.series[symbol]?.error" class="muted">{{ symbol }} 更新失败，{{ candleData?.series[symbol]?.updated_at ? `显示上次成功数据（${formatTime(candleData.series[symbol]?.updated_at ?? undefined)}）` : '暂无数据' }}。</p>
      <div v-if="candleChart" class="candle-plot"><svg viewBox="0 0 920 320" role="img" aria-label="策略标的前复权日K线图"><g v-for="(tick, index) in candleChart.ticks" :key="index"><line x1="60" x2="900" :y1="20 + index * 130" :y2="20 + index * 130" class="candle-grid" /><text x="52" :y="24 + index * 130" text-anchor="end" class="candle-axis">{{ tick.toFixed(2) }}</text></g><g v-for="item in candleChart.items" :key="item.symbol + item.date"><title>{{ item.symbol }} · {{ item.date }} 开 {{ item.open }} 高 {{ item.high }} 低 {{ item.low }} 收 {{ item.close }}</title><line :x1="item.x" :x2="item.x" :y1="candleChart.y(item.high)" :y2="candleChart.y(item.low)" :stroke="item.color" /><rect :x="item.x - candleChart.width / 2" :y="candleChart.y(Math.max(item.open, item.close))" :width="candleChart.width" :height="Math.max(1, Math.abs(candleChart.y(item.open) - candleChart.y(item.close)))" :stroke="item.color" :fill="item.close >= item.open ? item.color : 'white'" /></g><text x="60" y="307" class="candle-axis">{{ candleChart.dates[0] }}</text><text x="900" y="307" text-anchor="end" class="candle-axis">{{ candleChart.dates[candleChart.dates.length - 1] }}</text></svg></div>
      <p v-else class="muted">{{ candleData ? (visibleCandles.length ? 'K线数据准备中或暂无数据。' : '请选择要显示的标的。') : '正在加载K线数据…' }}</p>
      <p class="muted">点击标的名称切换显示；实心表示收盘不低于开盘，空心表示收盘低于开盘。鼠标悬停查看价格。</p>
    </section>
    <section><div class="section-title"><h2>策略日志</h2><button :disabled="!strategyId" @click="refreshDates">刷新日期</button></div><p class="muted">按日期倒序排列，点击加载。日志按策略归档，可能包含多个账户。</p><div class="logs"><div class="date-navigation" v-if="logDates.length"><button aria-label="向左翻看日期" @click="scrollDates(-1)">‹</button><nav ref="dateList" aria-label="日志日期"><button v-for="day in logDates" :key="day" :class="{ active: day === selectedDay }" :aria-pressed="day === selectedDay" @click="loadLog(day)">{{ day }}</button></nav><button aria-label="向右翻看日期" @click="scrollDates(1)">›</button></div><p v-else class="muted">暂无日志</p><div class="log-body"><div v-if="selectedDay" class="section-title"><button class="refresh-log" :disabled="loadingLog" @click="loadLog(selectedDay)">刷新日志</button></div><pre>{{ logText || (loadingLog ? '加载中…' : selectedDay ? '当天日志为空' : '暂无日志可查看') }}</pre><button v-if="hasMore" :disabled="loadingLog" @click="loadLog(selectedDay, true)">{{ loadingLog ? '加载中…' : '加载更多' }}</button></div></div></section>
    <p v-if="error" class="error" role="alert">{{ error }}</p><footer>只读监控 · 不提供交易操作</footer>
  </main>
</template>
