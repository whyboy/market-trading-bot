<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
type Instance = { id: string; mode: string; enabled: boolean }
type Strategy = { id: string; enable: boolean; instances: Instance[] }
type ScheduledTask = { instance_id: string; next_execution_at?: string; last_execution?: { state: string; error?: string; scheduled_at: string } }
type Runtime = { online: boolean; state: string; health?: string; heartbeat_scope?: string; is_trading_day?: boolean | null; scheduler_enabled?: boolean; scheduler_error?: string; scheduled_tasks?: ScheduledTask[]; started_at?: string; heartbeat_at?: string; uptime_seconds?: number; last_execution?: { success: boolean; error?: string; finished_at: string } }
type Account = { initialized: boolean; as_of?: string; date?: string; cash?: string; equity?: string; initial_cash?: string; positions?: Record<string, { quantity: number }> }
const runtime = ref<Runtime | null>(null), runtimeError = ref(''), error = ref('')
const strategies = ref<Strategy[]>([]), strategy = ref(''), instance = ref('')
const account = ref<Account | null>(null), dates = ref<string[]>([]), selectedDate = ref('')
const log = ref(''), offset = ref(0), hasMore = ref(false), loadingLog = ref(false)
const scheduledTask = computed(() => runtime.value?.scheduled_tasks?.find(task => task.instance_id === instance.value))
const taskState = (state: string) => ({ success: '成功', failed: '失败', running: '执行中' }[state] ?? state)
const accounts = computed(() => strategies.value.find(s => s.id === strategy.value)?.instances ?? [])
const label = computed(() => !runtime.value ? '状态未知' : runtime.value.state === 'disabled' ? '策略已停用' : runtime.value.scheduler_error ? '调度异常' : runtime.value.health === 'trade_timeout' ? '交易执行超时' : !runtime.value.online ? (runtime.value.state === 'unknown' ? '尚无心跳' : runtime.value.state === 'stopped' ? '已停止' : '心跳中断') : runtime.value.state === 'running' ? '执行中' : '正常待命')
const money = (value: unknown) => value == null ? '—' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const time = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'
const uptime = computed(() => runtime.value?.uptime_seconds == null ? '—' : `${Math.floor(runtime.value.uptime_seconds / 3600)} 小时 ${Math.floor(runtime.value.uptime_seconds / 60) % 60} 分钟 ${Math.floor(runtime.value.uptime_seconds) % 60} 秒`)
async function get<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) throw new Error(`请求失败（${response.status}）`)
  return response.json()
}
let runtimeRequest = 0
async function refreshRuntime() {
  const request = ++runtimeRequest
  if (!strategy.value) return
  try {
    const result = await get<Runtime>(`/api/runtime?strategy_id=${encodeURIComponent(strategy.value)}`)
    if (request === runtimeRequest) { runtime.value = result; runtimeError.value = '' }
  } catch {
    if (request === runtimeRequest) { runtime.value = null; runtimeError.value = '监控接口无法连接，请检查 Web 服务。' }
  }
}
let accountRequest = 0, dateRequest = 0, logRequest = 0
async function refreshAccount() {
  const request = ++accountRequest
  account.value = null
  if (!instance.value) return
  try { const result = await get<Account>(`/api/accounts/${encodeURIComponent(instance.value)}`); if (request === accountRequest) account.value = result }
  catch (e) { if (request === accountRequest) error.value = String(e) }
}
async function refreshDates() {
  const request = ++dateRequest
  try { const result = await get<string[]>(`/api/strategies/${encodeURIComponent(strategy.value)}/logs`); if (request === dateRequest) dates.value = result }
  catch (e) { if (request === dateRequest) error.value = String(e) }
}
async function loadLog(day: string, append = false) {
  const request = ++logRequest
  selectedDate.value = day; loadingLog.value = true; error.value = ''
  if (!append) { log.value = ''; offset.value = 0; hasMore.value = false }
  try {
    const result = await get<{ content: string; next_offset: number; has_more: boolean }>(`/api/strategies/${encodeURIComponent(strategy.value)}/logs/${day}?offset=${append ? offset.value : 0}`)
    if (request !== logRequest) return
    log.value = append ? log.value + result.content : result.content
    offset.value = result.next_offset; hasMore.value = result.has_more
  } catch (e) { if (request === logRequest) error.value = String(e) }
  finally { if (request === logRequest) loadingLog.value = false }
}
watch(strategy, () => {
  runtime.value = null; runtimeError.value = ''; void refreshRuntime()
  ++logRequest; selectedDate.value = ''; log.value = ''; hasMore.value = false; loadingLog.value = false; dates.value = []; error.value = ''
  instance.value = accounts.value[0]?.id ?? ''
  void refreshDates()
})
watch(instance, refreshAccount)
let timer: ReturnType<typeof setInterval> | undefined
onMounted(async () => {
  timer = setInterval(refreshRuntime, 5000)
  try { strategies.value = await get<Strategy[]>('/api/strategies'); strategy.value = strategies.value[0]?.id ?? '' }
  catch (e) { error.value = String(e) }
})
onUnmounted(() => { clearInterval(timer); ++runtimeRequest; ++logRequest; ++accountRequest; ++dateRequest })
</script>

<template>
  <main>
    <header><div><p class="eyebrow">MARKET TRADING BOT</p><h1>交易运行监控</h1></div><span class="badge" :class="{ online: runtime?.online }">{{ label }}</span></header>
    <p v-if="runtimeError" class="error" role="alert">{{ runtimeError }}</p>
    <section><h2>运行状况</h2><div class="metrics"><div><span>程序启动时间</span><strong>{{ time(runtime?.started_at) }}</strong></div><div><span>本次运行时长</span><strong>{{ uptime }}</strong></div><div><span>最近心跳 · {{ runtime?.heartbeat_scope === 'task' ? '当前任务' : '调度服务' }}</span><strong>{{ time(runtime?.heartbeat_at) }}</strong></div></div><p class="muted">时间均为北京时间。定时任务按交易日执行，待命表示调度服务运行，策略任务到点创建线程，执行完退出。</p><p v-if="runtime?.is_trading_day === false" class="muted">今日休市，不创建策略任务。</p><p v-else-if="runtime?.scheduler_enabled && runtime?.is_trading_day == null" class="muted">等待交易日确认，每天 9:00 检查。</p><p v-if="runtime?.scheduler_error" class="error">日历加载异常：{{ runtime.scheduler_error }}</p><p v-if="runtime?.last_execution">最近任务：<span :class="{ error: !runtime.last_execution.success }">{{ runtime.last_execution.success ? '成功' : '失败' }}</span> · {{ time(runtime.last_execution.finished_at) }} {{ runtime.last_execution.error }}</p></section>
    <section><div class="section-title"><h2>策略与账户</h2><button @click="refreshAccount">刷新账户</button></div><div class="selectors"><label>策略<select v-model="strategy"><option v-for="s in strategies" :key="s.id" :value="s.id">{{ s.id }}{{ s.enable ? '' : ' · 已停用' }}</option></select></label><label>账户<select v-model="instance"><option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.id }} · {{ a.mode }} · {{ a.enabled ? '已启用' : '已停用' }}</option></select></label></div>
    <div v-if="scheduledTask"><p>当日计划执行：{{ scheduledTask.next_execution_at ? time(scheduledTask.next_execution_at) : '暂无计划' }}</p><p v-if="scheduledTask.last_execution">最近调度：{{ time(scheduledTask.last_execution.scheduled_at) }} · {{ taskState(scheduledTask.last_execution.state) }} {{ scheduledTask.last_execution.error }}</p></div>
    <p v-if="!instance" class="muted">暂无账户实例</p><p v-else-if="account && !account.initialized" class="muted">暂无账户记录，首次模拟调仓后显示。</p><template v-else-if="account?.initialized"><div class="metrics"><div><span>总资产</span><strong>¥ {{ money(account.equity) }}</strong></div><div><span>可用现金</span><strong>¥ {{ money(account.cash) }}</strong></div><div><span>持仓市值</span><strong>¥ {{ money(Number(account.equity) - Number(account.cash)) }}</strong></div><div><span>累计收益</span><strong>{{ account.initial_cash ? `${money((Number(account.equity) / Number(account.initial_cash) - 1) * 100)}%` : '—' }}</strong></div></div><p class="muted">最近调仓估值：{{ account.as_of ? time(account.as_of) : account.date }}。资产并非实时行情估值。</p><table><thead><tr><th>持仓标的</th><th>数量</th></tr></thead><tbody><tr v-for="(position, symbol) in account.positions" :key="symbol"><td>{{ symbol }}</td><td>{{ position.quantity }}</td></tr></tbody></table><p v-if="!Object.keys(account.positions ?? {}).length" class="muted">当前空仓</p></template></section>
    <section><div class="section-title"><h2>策略日志</h2><button :disabled="!strategy" @click="refreshDates">刷新日期</button></div><p class="muted">按日期倒序排列，点击加载。日志按策略归档，可能包含多个账户。</p><div class="logs"><nav aria-label="日志日期"><button v-for="day in dates" :key="day" :class="{ active: day === selectedDate }" @click="loadLog(day)">{{ day }}</button><p v-if="!dates.length" class="muted">暂无日志</p></nav><div class="log-body"><div v-if="selectedDate" class="section-title"><span>{{ selectedDate }}</span><button :disabled="loadingLog" @click="loadLog(selectedDate)">刷新日志</button></div><pre>{{ log || (loadingLog ? '加载中…' : selectedDate ? '当天日志为空' : '请选择左侧日期查看日志') }}</pre><button v-if="hasMore" :disabled="loadingLog" @click="loadLog(selectedDate, true)">{{ loadingLog ? '加载中…' : '加载更多' }}</button></div></div></section>
    <p v-if="error" class="error" role="alert">{{ error }}</p><footer>只读监控 · 不提供交易操作</footer>
  </main>
</template>
