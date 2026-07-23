import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getPlatforms, invalidateConfigCache } from '@/lib/app-data'
import { apiFetch } from '@/lib/utils'
import { translateAccountStatus } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Users, CheckCircle, Clock, XCircle, RefreshCw, Bot, Save, Settings2, ScrollText } from 'lucide-react'
import { Button } from '@/components/ui/button'

const PLATFORM_COLORS: Record<string, string> = {
  trae: 'text-blue-400',
  tavily: 'text-purple-400',
  cursor: 'text-emerald-400',
}

const STATUS_VARIANT: Record<string, any> = {
  registered: 'default',
  trial: 'success',
  subscribed: 'success',
  expired: 'warning',
  invalid: 'danger',
  free: 'secondary',
  eligible: 'secondary',
  unknown: 'secondary',
  valid: 'success',
}

export default function Dashboard() {
  const { t, language } = useI18n()
  const navigate = useNavigate()
  const [stats, setStats] = useState<any>(null)
  const [poolStatus, setPoolStatus] = useState<any>(null)
  const [poolEvents, setPoolEvents] = useState<any[]>([])
  const [poolTarget, setPoolTarget] = useState('100')
  const [poolSaving, setPoolSaving] = useState(false)
  const [desktopStates, setDesktopStates] = useState<Record<string, any>>({})
  const [loading, setLoading] = useState(false)
  const poolLogRef = useRef<HTMLDivElement>(null)
  const desktopPlatforms = ['cursor', 'kiro', 'chatgpt']

  const loadPoolStatus = async () => {
    const pool = await apiFetch('/accounts/chatgpt/pool-status').catch(() => null)
    setPoolStatus(pool)
    if (pool?.target) setPoolTarget(String(pool.target))
    const taskId = String(pool?.active_task?.id || pool?.active_task?.task_id || '')
    if (!taskId) {
      setPoolEvents([])
      return
    }
    const events = await apiFetch(`/tasks/${taskId}/events?limit=200`).catch(() => ({ items: [] }))
    setPoolEvents(Array.isArray(events?.items) ? events.items.slice(-24) : [])
  }

  const load = async () => {
    setLoading(true)
    try {
      const [data, platforms] = await Promise.all([
        apiFetch('/accounts/stats'),
        getPlatforms().catch(() => []),
      ])
      setStats(data)
      await loadPoolStatus()
      const desktopEntries = await Promise.all(
        (platforms || [])
          .filter((item: any) => ['cursor', 'kiro', 'chatgpt'].includes(item.name))
          .map(async (item: any) => {
            const state = await apiFetch(`/platforms/${item.name}/desktop-state`).catch(() => ({ available: false }))
            return [item.name, { ...state, platform: item.name, display_name: item.display_name }] as const
          }),
      )
      setDesktopStates(Object.fromEntries(desktopEntries))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  useEffect(() => {
    if (!poolStatus?.active_task) return
    const timer = window.setInterval(() => { void loadPoolStatus() }, 5000)
    return () => window.clearInterval(timer)
  }, [poolStatus?.active_task])

  useEffect(() => {
    const container = poolLogRef.current
    if (container) container.scrollTop = container.scrollHeight
  }, [poolEvents])

  const savePoolConfig = async (data: Record<string, string>) => {
    setPoolSaving(true)
    try {
      await apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data }) })
      invalidateConfigCache()
      await load()
    } finally {
      setPoolSaving(false)
    }
  }

  const statCards = [
    { label: t('dashboard.totalAccounts'), value: stats?.total ?? '-', icon: Users, color: 'text-[var(--text-accent)]' },
    { label: t('dashboard.trial'), value: stats?.by_plan_state?.trial ?? 0, icon: Clock, color: 'text-amber-400' },
    { label: t('dashboard.subscribed'), value: stats?.by_plan_state?.subscribed ?? 0, icon: CheckCircle, color: 'text-emerald-400' },
    { label: t('dashboard.invalid'), value: (stats?.by_display_status?.expired ?? 0) + (stats?.by_validity_status?.invalid ?? 0), icon: XCircle, color: 'text-red-400' },
  ]
  const platformEntries = Object.entries(stats?.by_platform || {})
  const totalCount = Math.max(Number(stats?.total || 0), 0)

  const renderStatusGroup = (title: string, values: Record<string, number> | undefined, emptyCopy = t('dashboard.noData')) => (
    <div className="space-y-2">
      <div className="px-1 text-sm font-medium text-[var(--text-primary)]">{title}</div>
      {values && Object.keys(values).length > 0 ? Object.entries(values).map(([status, count]) => (
        <div key={status} className="flex items-center justify-between rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2.5">
          <Badge variant={STATUS_VARIANT[status] || 'secondary'}>{translateAccountStatus(status, language)}</Badge>
          <span className="text-sm text-[var(--text-secondary)]">{count}</span>
        </div>
      )) : (
        <div className="empty-state-panel">{emptyCopy}</div>
      )}
    </div>
  )

  return (
    <div className="space-y-4">
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {statCards.map(({ label, value, icon: Icon, color }) => (
          <div key={label} className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-[11px] tracking-[0.16em] text-[var(--text-muted)]">{label}</p>
                <p className="mt-1 text-xl font-semibold tracking-[-0.03em] text-[var(--text-primary)]">{value}</p>
              </div>
              <div className="flex h-9 w-9 items-center justify-center rounded-md border border-[var(--border-soft)] bg-[var(--chip-bg)]">
                <Icon className={`h-4.5 w-4.5 ${color} opacity-90`} />
              </div>
            </div>
          </div>
        ))}
      </div>

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <div className="flex items-center gap-2">
            <Bot className="h-4 w-4 text-[var(--text-accent)]" />
            <CardTitle>ChatGPT 自动补号</CardTitle>
          </div>
          <Button variant="ghost" size="sm" onClick={() => navigate('/accounts/chatgpt')} title="查看 ChatGPT 账号">
            <Settings2 className="h-4 w-4" />
          </Button>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2.5">
              <div className="text-xs text-[var(--text-muted)]">当前可用</div>
              <div className="mt-1 text-xl font-semibold text-[var(--text-primary)]">{poolStatus?.usable ?? '-'}</div>
            </div>
            <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2.5">
              <div className="text-xs text-[var(--text-muted)]">目标数量</div>
              <div className="mt-1 text-xl font-semibold text-[var(--text-primary)]">{poolStatus?.target ?? '-'}</div>
            </div>
            <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2.5">
              <div className="text-xs text-[var(--text-muted)]">待补数量</div>
              <div className="mt-1 text-xl font-semibold text-amber-500">{poolStatus?.shortfall ?? '-'}</div>
            </div>
            <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2.5">
              <div className="text-xs text-[var(--text-muted)]">当前任务</div>
              <div className="mt-1 truncate text-sm font-semibold text-[var(--text-primary)]">
                {poolStatus?.active_task ? `${poolStatus.active_task.progress} ${poolStatus.active_task.status}` : '空闲'}
              </div>
            </div>
          </div>
          <div className="progress-track">
            <div
              className="progress-fill"
              style={{ width: `${Math.min(100, Math.round(((poolStatus?.usable || 0) / Math.max(poolStatus?.target || 1, 1)) * 100))}%` }}
            />
          </div>
          {poolStatus?.active_task ? (
            <div className="border-t border-[var(--border-soft)] pt-3">
              <div className="mb-2 flex items-center justify-between gap-3">
                <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)]">
                  <ScrollText className="h-4 w-4 text-[var(--text-accent)]" />
                  实时执行日志
                </div>
                <span className="text-xs text-[var(--text-muted)]">
                  {poolStatus.active_task.progress} · {poolStatus.active_task.status}
                </span>
              </div>
              <div ref={poolLogRef} className="max-h-56 overflow-y-auto rounded-md border border-[var(--border-soft)] bg-[var(--bg-input)] px-3 py-2 font-mono text-xs leading-5">
                {poolEvents.length > 0 ? poolEvents.map((event) => {
                  const message = String(event?.line || event?.message || '')
                  const tone = /失败|错误|超时|失效/.test(message)
                    ? 'text-red-400'
                    : /成功|验证码:|已导入/.test(message)
                      ? 'text-emerald-400'
                      : 'text-[var(--text-secondary)]'
                  return <div key={event.id} className={`break-words ${tone}`}>{message}</div>
                }) : (
                  <div className="py-3 text-center text-[var(--text-muted)]">等待任务日志...</div>
                )}
              </div>
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-3 border-t border-[var(--border-soft)] pt-3">
            <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
              <input
                type="checkbox"
                checked={Boolean(poolStatus?.enabled)}
                disabled={poolSaving || !poolStatus}
                onChange={(event) => savePoolConfig({ chatgpt_pool_maintenance_enabled: String(event.target.checked) })}
                className="h-4 w-4 accent-[var(--accent)]"
              />
              启用自动补量
            </label>
            <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
              目标
              <input
                type="number"
                min="1"
                value={poolTarget}
                onChange={(event) => setPoolTarget(event.target.value)}
                className="control-surface w-24"
              />
            </label>
            <Button
              variant="outline"
              size="sm"
              disabled={poolSaving || !poolStatus}
              onClick={() => savePoolConfig({ chatgpt_pool_target: String(Math.max(Number(poolTarget) || 1, 1)) })}
              title="保存目标数量"
            >
              <Save className="h-4 w-4" />
            </Button>
            <Button variant="ghost" size="sm" onClick={load} disabled={loading} title="刷新号池状态">
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(300px,0.55fr)]">
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle>{t('dashboard.platformDistribution')}</CardTitle>
            <Button variant="outline" size="sm" onClick={load} disabled={loading}>
              <RefreshCw className={`mr-1 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              {t('common.refresh')}
            </Button>
          </CardHeader>
          <CardContent className="space-y-3">
            {platformEntries.length > 0 ? platformEntries.map(([platform, count]) => {
              const countValue = Number(count) || 0
              const ratio = totalCount > 0 ? Math.round((countValue / totalCount) * 100) : 0
              return (
                <div key={platform} className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2.5">
                  <div className="flex items-center justify-between gap-3">
                    <span className={`text-sm font-medium ${PLATFORM_COLORS[platform] || 'text-[var(--text-secondary)]'}`}>
                      {platform}
                    </span>
                    <span className="text-xs text-[var(--text-muted)]">{countValue} / {ratio}%</span>
                  </div>
                  <div className="progress-track mt-3">
                    <div className="progress-fill" style={{ width: `${ratio}%` }} />
                  </div>
                </div>
              )
            }) : (
              <div className="empty-state-panel">{stats ? t('dashboard.noPlatformData') : t('dashboard.loadingStats')}</div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>{t('dashboard.desktopStatus')}</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            {desktopPlatforms.map((platform) => {
              const state = desktopStates[platform]
              const label = state?.app_name || state?.display_name || platform
              const badges = state
                ? [
                    { label: state.installed ? t('dashboard.installed') : t('dashboard.notInstalled'), variant: state.installed ? 'success' : 'secondary' },
                    { label: state.configured ? t('dashboard.configured') : t('dashboard.notConfigured'), variant: state.configured ? 'success' : 'warning' },
                    { label: state.running ? t('dashboard.opened') : t('dashboard.notOpened'), variant: state.running ? 'success' : 'secondary' },
                    { label: state.ready ? t('dashboard.ready') : t('dashboard.notReady'), variant: state.ready ? 'success' : 'warning' },
                  ]
                : []
              return (
                <div key={platform} className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-[var(--text-primary)]">{label}</div>
                      <div className="mt-1 text-xs leading-5 text-[var(--text-muted)]">
                        {state?.available === false
                          ? (state?.message || t('dashboard.desktopUnavailable'))
                          : (state?.ready_label || state?.status_label || t('dashboard.desktopReadyDescription'))}
                      </div>
                    </div>
                    <Badge variant={state?.ready ? 'success' : 'secondary'}>
                      {state?.ready ? t('common.ready') : t('common.standby')}
                    </Badge>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {badges.length > 0 ? badges.map((badge) => (
                      <Badge key={`${platform}-${badge.label}`} variant={badge.variant as any}>{badge.label}</Badge>
                    )) : (
                      <span className="text-xs text-[var(--text-muted)]">{t('common.loading')}</span>
                    )}
                  </div>
                </div>
              )
            })}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader><CardTitle>{t('dashboard.statusDistribution')}</CardTitle></CardHeader>
        <CardContent className="grid gap-4 xl:grid-cols-3">
          {renderStatusGroup(t('dashboard.plan'), stats?.by_plan_state, t('dashboard.noPlanData'))}
          {renderStatusGroup(t('dashboard.lifecycle'), stats?.by_lifecycle_status, t('dashboard.noLifecycleData'))}
          {renderStatusGroup(t('dashboard.validity'), stats?.by_validity_status, t('dashboard.noValidityData'))}
        </CardContent>
      </Card>
    </div>
  )
}
