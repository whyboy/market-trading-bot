<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
type AccountConfig = { id: string; mode: string; enabled: boolean }
type Strategy = { id: string; enable: boolean; instances: AccountConfig[] }
type ScheduledTask = { instance_id: string; next_execution_at?: string; last_execution?: { state: string; error?: string; scheduled_at: string } }
type Runtime = { online: boolean; state: string; health?: string; heartbeat_scope?: string; is_trading_day?: boolean | null; scheduler_enabled?: boolean; scheduler_error?: string; scheduled_tasks?: ScheduledTask[]; started_at?: string; heartbeat_at?: string; uptime_seconds?: number; last_execution?: { success: boolean; error?: string; finished_at: string } }
type Account = { initialized: boolean; as_of?: string; date?: string; cash?: string; equity?: string; initial_cash?: string; positions?: Record<string, { quantity: number }> }
const runtime = ref<Runtime | null>(null), runtimeError = ref(''), error = ref('')
const strategies = ref<Strategy[]>([]), strategyId = ref(''), accountId = ref('')
const account = ref<Account | null>(null), logDates = ref<string[]>([]), selectedDay = ref('')
const logText = ref(''), offset = ref(0), hasMore = ref(false), loadingLog = ref(false)
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
  account.value = null
  if (!accountId.value) return
  try { const result = await fetchJson<Account>(`/api/accounts/${encodeURIComponent(accountId.value)}`); if (requestId === accountRequestId) account.value = result }
  catch (cause) { if (requestId === accountRequestId) error.value = String(cause) }
}
async function refreshDates() {
  const requestId = ++datesRequestId
  try { const result = await fetchJson<string[]>(`/api/strategies/${encodeURIComponent(strategyId.value)}/logs`); if (requestId === datesRequestId) logDates.value = result }
  catch (cause) { if (requestId === datesRequestId) error.value = String(cause) }
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
})
watch(accountId, () => { runtime.value = null; void refreshRuntime(); void refreshAccount() })
let timer: ReturnType<typeof setInterval> | undefined
onMounted(async () => {
  timer = setInterval(refreshRuntime, 5000)
  try { strategies.value = await fetchJson<Strategy[]>('/api/strategies'); strategyId.value = strategies.value[0]?.id ?? '' }
  catch (cause) { error.value = String(cause) }
})
onUnmounted(() => { clearInterval(timer); ++runtimeRequestId; ++logRequestId; ++accountRequestId; ++datesRequestId })
async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) throw new Error(`请求失败（${response.status}）`)
  return response.json()
}
</script>

<template>
  <main>
    <header><div><p class="eyebrow">MARKET TRADING BOT</p><h1>交易运行监控</h1></div><span class="badge" :class="{ online: runtime?.online }">{{ statusLabel }}</span></header>
    <p v-if="runtimeError" class="error" role="alert">{{ runtimeError }}</p>
    <section><h2>运行状况</h2><div class="metrics"><div><span>程序启动时间</span><strong>{{ formatTime(runtime?.started_at) }}</strong></div><div><span>本次运行时长</span><strong>{{ uptime }}</strong></div><div><span>最近心跳 · {{ runtime?.heartbeat_scope === 'task' ? '当前任务' : '调度服务' }}</span><strong>{{ formatTime(runtime?.heartbeat_at) }}</strong></div></div><p class="muted">时间均为北京时间。定时任务按交易日执行，待命表示调度服务运行，策略任务到点创建线程，执行完退出。</p><p v-if="runtime?.is_trading_day === false" class="muted">今日休市，不创建策略任务。</p><p v-else-if="runtime?.scheduler_enabled && runtime?.is_trading_day == null" class="muted">等待交易日确认，每天 9:00 检查。</p><p v-if="runtime?.scheduler_error" class="error">日历加载异常：{{ runtime.scheduler_error }}</p><p v-if="runtime?.last_execution">最近任务：<span :class="{ error: !runtime.last_execution.success }">{{ runtime.last_execution.success ? '成功' : '失败' }}</span> · {{ formatTime(runtime.last_execution.finished_at) }} {{ runtime.last_execution.error }}</p></section>
    <section><div class="section-title"><h2>策略与账户</h2><button @click="refreshAccount">刷新账户</button></div><div class="selectors"><label>策略<select v-model="strategyId"><option v-for="config in strategies" :key="config.id" :value="config.id">{{ config.id }}{{ config.enable ? '' : ' · 已停用' }}</option></select></label><label>账户<select v-model="accountId"><option v-for="item in accounts" :key="item.id" :value="item.id">{{ item.id }} · {{ item.mode }} · {{ item.enabled ? '已启用' : '已停用' }}</option></select></label></div>
    <div v-if="scheduledTask"><p>当日计划执行：{{ scheduledTask.next_execution_at ? formatTime(scheduledTask.next_execution_at) : '暂无计划' }}</p><p v-if="scheduledTask.last_execution">最近调度：{{ formatTime(scheduledTask.last_execution.scheduled_at) }} · {{ taskLabel(scheduledTask.last_execution.state) }} {{ scheduledTask.last_execution.error }}</p></div>
    <p v-if="!accountId" class="muted">暂无账户实例</p><p v-else-if="account && !account.initialized" class="muted">暂无账户记录，首次模拟调仓后显示。</p><template v-else-if="account?.initialized"><div class="metrics"><div><span>总资产</span><strong>¥ {{ formatMoney(account.equity) }}</strong></div><div><span>可用现金</span><strong>¥ {{ formatMoney(account.cash) }}</strong></div><div><span>持仓市值</span><strong>¥ {{ formatMoney(Number(account.equity) - Number(account.cash)) }}</strong></div><div><span>累计收益</span><strong>{{ account.initial_cash ? `${formatMoney((Number(account.equity) / Number(account.initial_cash) - 1) * 100)}%` : '—' }}</strong></div></div><p class="muted">最近调仓估值：{{ account.as_of ? formatTime(account.as_of) : account.date }}。资产并非实时行情估值。</p><table><thead><tr><th>持仓标的</th><th>数量</th></tr></thead><tbody><tr v-for="(position, symbol) in account.positions" :key="symbol"><td>{{ symbol }}</td><td>{{ position.quantity }}</td></tr></tbody></table><p v-if="!Object.keys(account.positions ?? {}).length" class="muted">当前空仓</p></template></section>
    <section><div class="section-title"><h2>策略日志</h2><button :disabled="!strategyId" @click="refreshDates">刷新日期</button></div><p class="muted">按日期倒序排列，点击加载。日志按策略归档，可能包含多个账户。</p><div class="logs"><nav aria-label="日志日期"><button v-for="day in logDates" :key="day" :class="{ active: day === selectedDay }" @click="loadLog(day)">{{ day }}</button><p v-if="!logDates.length" class="muted">暂无日志</p></nav><div class="log-body"><div v-if="selectedDay" class="section-title"><span>{{ selectedDay }}</span><button :disabled="loadingLog" @click="loadLog(selectedDay)">刷新日志</button></div><pre>{{ logText || (loadingLog ? '加载中…' : selectedDay ? '当天日志为空' : '请选择左侧日期查看日志') }}</pre><button v-if="hasMore" :disabled="loadingLog" @click="loadLog(selectedDay, true)">{{ loadingLog ? '加载中…' : '加载更多' }}</button></div></div></section>
    <p v-if="error" class="error" role="alert">{{ error }}</p><footer>只读监控 · 不提供交易操作</footer>
  </main>
</template>
