import { useEffect, useState } from 'react'
import { api, perform, report } from './state'

interface Summary {
  id: string
  goal: string
  status: string
  notice: string | null
}

interface Stage {
  position: number
  definition: { title: string; goal: string; acceptance: string; verify?: boolean }
  verification?: { receipt_id: string; exit_code: number | null } | null
  session_id: string | null
  admission_status: string | null
  verified: boolean
  report: { summary: string; artifacts: string[]; next_step: string | null } | null
  evidence: { effects: { id: string; status: string }[] } | null
}

interface Defaults {
  wall_seconds: number
  wall_seconds_max: number
  stages_max: number
  parallelism_max: number
}

interface Details extends Summary {
  stages: Stage[]
  remaining: { wall_seconds: number; max_model_calls: number; max_tool_calls: number }
  used: { wall_seconds: number }
}

const statusLabel: Record<string, string> = {
  running: '运行中', paused: '已暂停', blocked: '待核对', cancelled: '已取消',
  cancelling: '正在停止', completed: '已完成', queued: '排队中', started: '执行中',
  waiting_approval: '待批准', job_paused: '已安全暂停', unknown: '结果待对账', interrupted: '已中断', reconciled: '已对账',
}

function newStage(title = '') {
  return { id: crypto.randomUUID(), title, goal: '', acceptance: '', read_only: false, verify: false, verification_command: '', group: '' }
}

export function LongTasks({ openStage }: { openStage: (id: string) => void }) {
  const [jobs, setJobs] = useState<Summary[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [current, setCurrent] = useState<Details | null>(null)
  const [goal, setGoal] = useState('')
  const [seconds, setSeconds] = useState<number | null>(null)
  const [defaults, setDefaults] = useState<Defaults | null>(null)
  const [engine, setEngine] = useState<'standard' | 'deep'>('standard')
  const [parallel, setParallel] = useState(false)
  const [stages, setStages] = useState(() => ['调研', '实现', '验证'].map(newStage))
  const [requestId, setRequestId] = useState(() => crypto.randomUUID())
  const [review, setReview] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let disposed = false
    let timer: ReturnType<typeof setTimeout>
    async function refresh() {
      try {
        const jobs = await api<Summary[]>('/jobs')
        if (!disposed) setJobs(jobs)
        if (selected) {
          const detail = await api<Details>(`/jobs/${selected}`)
          if (!disposed) setCurrent(detail)
        }
      } catch (error) {
        if (!disposed) report(error)
      } finally {
        if (!disposed) timer = setTimeout(() => void refresh(), 2500)
      }
    }
    void refresh()
    return () => { disposed = true; clearTimeout(timer) }
  }, [selected])

  useEffect(() => {
    let disposed = false
    void api<Defaults>('/jobs/defaults').then(value => {
      if (!disposed) setDefaults(value)
    }).catch(error => { if (!disposed) report(error) })
    return () => { disposed = true }
  }, [])

  const task = current?.id === selected ? current : null
  function updateStage(id: string, changes: Partial<ReturnType<typeof newStage>>) {
    setStages(rows => rows.map(row => row.id === id ? { ...row, ...changes } : row))
    setRequestId(crypto.randomUUID())
  }
  function create() {
    void perform(async () => {
      setBusy(true)
      try {
        const created = await api<Details>('/jobs', 'POST', {
          request_id: requestId, goal, engine, parallelism: parallel ? defaults?.parallelism_max : undefined,
          budget: seconds === null ? undefined : { wall_seconds: seconds },
          stages: stages.map(({ id: _id, group, ...row }) => ({
            ...row, verification_command: row.verify ? row.verification_command.trim() || null : null,
            group: !parallel || group === '' ? null : Number(group),
          })),
        })
        setCurrent(created)
        setSelected(created.id)
        setRequestId(crypto.randomUUID())
      } finally {
        setBusy(false)
      }
    })
  }
  function command(action: string, position?: number) {
    if (!task) return
    void perform(async () => {
      setBusy(true)
      try {
        setCurrent(await api<Details>(`/jobs/${task.id}/commands`, 'POST', {
          action, position, evidence: position === undefined ? undefined : review[position],
        }))
      } finally {
        setBusy(false)
      }
    })
  }

  return (
    <section className="long-tasks">
      <header><h2>长任务</h2><button onClick={() => { setSelected(null); setReview({}) }}>新建长任务</button></header>
      {jobs.length > 0 && <label>任务记录
        <select value={selected ?? ''} onChange={event => { setSelected(event.target.value || null); setReview({}) }}>
          <option value="">新建长任务</option>
          {jobs.map(job => <option key={job.id} value={job.id}>{job.goal.slice(0, 45)} · {statusLabel[job.status]}</option>)}
        </select>
      </label>}
      {selected && !task && <p>正在加载任务…</p>}
      {!selected && <form onSubmit={event => { event.preventDefault(); create() }}>
        <label>任务目标<textarea required maxLength={2400} value={goal} onChange={event => {
          setGoal(event.target.value); setRequestId(crypto.randomUUID())
        }} /></label>
        <div className="actions">
          <label>执行引擎<select value={engine} onChange={event => {
            setEngine(event.target.value === 'deep' ? 'deep' : 'standard'); setRequestId(crypto.randomUUID())
          }}><option value="standard">Standard</option><option value="deep">Deep</option></select></label>
          <label>主动执行总秒数<input type="number" min={stages.length} max={defaults?.wall_seconds_max} value={seconds ?? defaults?.wall_seconds ?? ''}
            onChange={event => { setSeconds(Number(event.target.value)); setRequestId(crypto.randomUUID()) }} /></label>
          <label><input type="checkbox" checked={parallel} disabled={(defaults?.parallelism_max ?? 1) <= 1}
            onChange={event => { setParallel(event.target.checked); setRequestId(crypto.randomUUID()) }} /> 并行处理同组只读阶段</label>
        </div>
        {stages.map((stage, index) => <fieldset key={stage.id}>
          <legend>阶段 {index + 1}</legend>
          <label>名称<input required maxLength={120} value={stage.title} onChange={event => updateStage(stage.id, { title: event.target.value })} /></label>
          <label>要完成的工作<textarea required maxLength={1600} value={stage.goal} onChange={event => updateStage(stage.id, { goal: event.target.value })} /></label>
          <label>验收条件<input required maxLength={600} value={stage.acceptance} onChange={event => updateStage(stage.id, { acceptance: event.target.value })} /></label>
          <label><input type="checkbox" checked={stage.read_only}
            onChange={event => updateStage(stage.id, { read_only: event.target.checked, verify: false })} /> 只读</label>
          <label><input type="checkbox" checked={stage.verify} disabled={stage.read_only}
            onChange={event => updateStage(stage.id, { verify: event.target.checked })} /> 要求成功的命令回执</label>
          {stage.verify && (
            <label>指定验收命令（可选）<input value={stage.verification_command}
              onChange={event => updateStage(stage.id, { verification_command: event.target.value })}
              placeholder="留空时使用阶段最后一条命令" /></label>
          )}
          {parallel && <label>组号（相同组号并行，留空顺序执行）<input type="number" min={0} value={stage.group}
            onChange={event => updateStage(stage.id, { group: event.target.value })} /></label>}
          {stages.length > 1 && <button type="button" onClick={() => {
            setStages(rows => rows.filter(row => row.id !== stage.id)); setRequestId(crypto.randomUUID())
          }}>删除阶段</button>}
        </fieldset>)}
        <div className="actions">
          <button type="button" disabled={!defaults || stages.length >= defaults.stages_max} onClick={() => {
            setStages(rows => [...rows, newStage()]); setRequestId(crypto.randomUUID())
          }}>添加阶段</button>
          <button type="submit" className="primary" disabled={busy || !defaults}>启动长任务</button>
        </div>
      </form>}
      {task && <>
        <h3>{task.goal}</h3>
        <p role="status">{statusLabel[task.status]} · 已完成 {task.stages.filter(stage => stage.verified).length}/{task.stages.length} 个阶段</p>
        <p>已结算 {Math.round(task.used.wall_seconds)} 秒 · 剩余 {Math.floor(task.remaining.wall_seconds)} 秒 / {task.remaining.max_model_calls} 次模型调用 / {task.remaining.max_tool_calls} 次工具调用</p>
        {task.notice && <p>{task.notice}</p>}
        <div className="actions">
          {task.status === 'running' && <button disabled={busy} onClick={() => command('pause')}>安全暂停</button>}
          {['paused', 'blocked'].includes(task.status) && <button disabled={busy} onClick={() => command('resume')}>继续</button>}
          {['running', 'paused', 'blocked'].includes(task.status) && <button disabled={busy} onClick={() => command('cancel')}>停止任务</button>}
        </div>
        {task.stages.map(stage => <fieldset key={stage.position}>
          <legend>{stage.position + 1}. {stage.definition.title}</legend>
          <p>{stage.verified ? '已验收' : statusLabel[stage.admission_status ?? ''] ?? '未开始'}</p>
          <p>{stage.report?.summary ?? stage.definition.goal}</p>
          {stage.verification && (
            <p className="muted small">验收退出码 {stage.verification.exit_code ?? '未取得'} · {stage.verification.receipt_id}</p>
          )}
          {stage.report?.artifacts.map((artifact, index) => <p key={index}>{artifact}</p>)}
          {stage.evidence?.effects.map(effect => <p key={effect.id}>{effect.id} · {effect.status}</p>)}
          {stage.session_id && <button onClick={() => openStage(stage.session_id!)}>查看阶段会话和审批</button>}
          {task.status === 'blocked' && !stage.verified && ['reconciled', 'interrupted', 'completed'].includes(stage.admission_status ?? '') && <>
            <label>人工验收证据<textarea maxLength={2000} value={review[stage.position] ?? ''}
              onChange={event => setReview({ ...review, [stage.position]: event.target.value })} /></label>
            <button disabled={busy || !review[stage.position]?.trim()} onClick={() => command('accept', stage.position)}>确认阶段已完成</button>
          </>}
        </fieldset>)}
      </>}
    </section>
  )
}
