import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import { api, HttpError, requestId, upload } from './api'
import { appendImages, dropImages, ImagePicker, ImageStrip, pasteImages, stageImages } from '../image-input'
import type { ImageDraft } from '../image-input'
import './style.css'
import '../images.css'

declare const __APP_VERSION__: string

type Workspace = { id: string; mode: string }
type Model = { id: string; label: string; provider: string; model: string; configured: boolean; capabilities: string[] }
type Agent = { id: string; label: string; workflow: string | null }
type Catalog = {
  default_workspace: string; default_model: string
  workspaces: Workspace[]; models: Model[]; agents: Agent[]
}
type Session = { id: string; prompt: string }
type Run = { id: string; generation: number; status: string; result: string | null }
type Approval = {
  id: string; admission_id: string; generation: number; proposal_hash: string
  proposal: { tool_name: string; arguments: unknown }
}
type Effect = { id: string; proposal_hash: string; status: string }
type Status = {
  latest_admission: Run | null; model_id: string
  vision: boolean; image_context: string[] | null
  approvals: Approval[]; active_effects: Effect[]
}
type Message = { seq: number; message: { id: string; role: string; content: unknown; image_ids?: string[] } }
type Artifact = { id: string; name: string; size: number; kind: string }
type Submission = { text: string; submission_id: string; artifact_ids?: string[]; image_ids?: string[] }
type Job = {
  id: string; goal: string; status: string; notice?: string
  stages?: { position: number; definition: { title: string }; session_id: string | null;
    admission_status: string | null; verified: boolean }[]
}

const labels: Record<string, string> = {
  queued: '排队', started: '运行中', waiting_approval: '等待审批', completed: '完成',
  unknown: '待对账', paused: '已暂停', cancelled: '已取消', blocked: '需要处理',
  interrupted: '已中断', reconciled: '已对账', running: '运行中', tool: '工具'
}
const stateText = (status: string) => labels[status] ?? status
const short = (value: string) => value.replace(/\s+/g, ' ').slice(0, 48)
const rootPath = (workspace: string) => `/api/v1/workspaces/${encodeURIComponent(workspace)}`

function Workbench() {
  const [catalog, setCatalog] = useState<Catalog>()
  const [workspace, setWorkspace] = useState('')
  const [model, setModel] = useState('')
  const [agent, setAgent] = useState('')
  const [harness, setHarness] = useState('general')
  const [sessions, setSessions] = useState<Session[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [session, setSession] = useState('')
  const [job, setJob] = useState<Job>()
  const [tab, setTab] = useState<'sessions' | 'jobs'>('sessions')
  const [status, setStatus] = useState<Status>()
  const [messages, setMessages] = useState<Message[]>([])
  const [older, setOlder] = useState(false)
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [files, setFiles] = useState<File[]>([])
  const [images, setImages] = useState<ImageDraft[]>([])
  const [submission, setSubmission] = useState<Submission | null>(null)
  const selection = useRef('')
  const [input, setInput] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [ready, setReady] = useState(false)
  const [useFiles, setUseFiles] = useState(true)
  const [useShell, setUseShell] = useState(false)
  const [evidence, setEvidence] = useState('')
  const [reviewed, setReviewed] = useState<Record<string, string>>({})
  const [lookup, setLookup] = useState<Record<string, unknown>>({})
  const base = rootPath(workspace)
  const sessionPath = `${base}/sessions/${session}`
  const submissionKey = `deep-harness-web:${workspace}:${session}:pending`
  const loadImage = useCallback((id: string, full = false) =>
    `${sessionPath}/images/${id}${full ? '' : '?thumbnail=1'}`, [sessionPath])
  const imagesDisabled = status?.vision === false
  const addImages = (items: ImageDraft[]) => {
    if (busy || submission || imagesDisabled) return
    const result = appendImages(images, items)
    setImages(result.images)
    setError(result.error)
  }
  const addFiles = (items: File[]) => addImages(items.map(file => ({ file })))

  useEffect(() => {
    selection.current = sessionPath
    const saved = session ? sessionStorage.getItem(submissionKey) : null
    const previous: Submission | null = saved ? JSON.parse(saved) : null
    setSubmission(previous)
    setImages(previous?.image_ids?.map(id => ({ id })) ?? [])
    setInput(previous?.text ?? '')
    setFiles([])
  }, [session, sessionPath, submissionKey])
  const selectedAgent = catalog?.agents.find(row => row.id === agent)
  const sandbox = catalog?.workspaces.find(row => row.id === workspace)?.mode === 'sandbox'

  useEffect(() => {
    void api<Catalog>('/api/v1/instance').then(data => {
      setCatalog(data)
      setWorkspace(data.default_workspace)
      setModel(data.default_model)
    }).catch(exc => setError(exc.message))
  }, [])

  const refreshLists = useCallback(async () => {
    if (!workspace) return
    const [conversations, tasks] = await Promise.all([
      api<{ sessions: Session[] }>(`${base}/sessions`), api<Job[]>(`${base}/jobs`)
    ])
    setSessions(conversations.sessions)
    setJobs(tasks)
  }, [base, workspace])

  useEffect(() => {
    void refreshLists().catch(exc => setError(exc.message))
  }, [refreshLists])

  useEffect(() => {
    if (!session) return
    let mounted = true
    let timer: ReturnType<typeof setTimeout>
    const refresh = async () => {
      try {
        const [newStatus, page] = await Promise.all([
          api<Status>(sessionPath),
          api<{ items: Message[]; has_more: boolean }>(`${sessionPath}/messages`)
        ])
        if (!mounted) return
        setStatus(newStatus)
        setMessages(previous => {
          const recent = new Map(previous.map(row => [row.seq, row]))
          for (const row of page.items) recent.set(row.seq, row)
          return Array.from(recent.values()).sort((a, b) => a.seq - b.seq)
        })
        setOlder(page.has_more)
        const run = newStatus.latest_admission
        const found = run ? await api<Artifact[]>(`${base}/runs/${run.id}/artifacts`) : []
        if (mounted) setArtifacts(found)
      } catch (exc) {
        if (mounted) setError((exc as Error).message)
      }
    }
    const events = new EventSource(`${sessionPath}/events`)
    events.onopen = () => setReady(true)
    events.onerror = () => setReady(false)
    for (const event of ['admission.accepted', 'execution.started', 'execution.completed',
      'execution.unknown', 'execution.interrupted', 'execution.cancelled', 'execution.reconciled',
      'execution.waiting_approval', 'interaction.resolved', 'artifact.ready', 'model.started', 'model.delta']) {
      events.addEventListener(event, () => {
        clearTimeout(timer)
        timer = setTimeout(() => void refresh(), 100)
      })
    }
    const check = setInterval(() => void refresh(), 2200)
    void refresh()
    return () => {
      mounted = false
      clearInterval(check)
      clearTimeout(timer)
      events.close()
      setReady(false)
    }
  }, [session, sessionPath, base])

  useEffect(() => {
    if (!job?.id) return
    let mounted = true
    const id = job.id
    const refresh = () => void api<Job>(`${base}/jobs/${id}`).then(value => {
      if (mounted) setJob(value)
    }).catch(exc => { if (mounted) setError(exc.message) })
    const timer = setInterval(refresh, 1600)
    refresh()
    return () => { mounted = false; clearInterval(timer) }
  }, [base, job?.id])

  function openSession(id: string) {
    setSession(id)
    setStatus(undefined)
    setMessages([])
    setArtifacts([])
    setFiles([])
    setInput('')
    setEvidence('')
    setReviewed({})
    setLookup({})
    setOlder(false)
    setTab('sessions')
  }

  async function perform(action: () => Promise<void>) {
    if (busy) return
    setBusy(true)
    setError('')
    try { await action() } catch (exc) { setError((exc as Error).message) }
    finally { setBusy(false) }
  }

  async function create() {
    if (!workspace || !model) return
    const created = await api<Session>(`${base}/sessions`, {
      request_id: requestId(), model_id: model, agent_id: agent || null, harness,
      tools: agent ? [] : [
        ...(useFiles ? ['read', 'list_files', 'search_files'] : []),
        ...(sandbox && useShell && !harness.startsWith('research') ? ['bash'] : [])
      ],
    })
    await refreshLists()
    openSession(created.id)
  }

  function send(event: FormEvent) {
    event.preventDefault()
    if (!session || (!submission && !input.trim() && !images.length)) return
    void perform(async () => {
      let payload = submission
      if (!payload) {
        const imageIds = await stageImages(images, async file =>
          (await upload(`${sessionPath}/images`, file)).id,
        staged => { if (selection.current === sessionPath) setImages(staged) })
        const artifactIds: string[] = []
        for (const file of files) artifactIds.push((await upload(`${sessionPath}/attachments`, file)).id)
        payload = { submission_id: requestId(), text: input.trim(), artifact_ids: artifactIds, image_ids: imageIds }
        sessionStorage.setItem(submissionKey, JSON.stringify(payload))
        if (selection.current === sessionPath) setSubmission(payload)
      }
      const existing = submission && await api<Run | null>(
        `${sessionPath}/submission?submission_id=${encodeURIComponent(payload.submission_id)}`
      )
      if (!existing) {
        try {
          await api(`${sessionPath}/runs`, payload)
        } catch (error) {
          if (error instanceof HttpError && error.status < 500) {
            sessionStorage.removeItem(submissionKey)
            if (selection.current === sessionPath) setSubmission(null)
          }
          throw error
        }
      }
      sessionStorage.removeItem(submissionKey)
      if (selection.current === sessionPath) {
        setSubmission(null)
        setInput('')
        setImages([])
        setFiles([])
      }
    })
  }

  async function decide(approval: Approval, choice: 'approve' | 'deny') {
    await api(`${base}/runs/${approval.admission_id}/approvals/${encodeURIComponent(approval.id)}/decision`, {
      decision: choice, generation: approval.generation, proposal_hash: approval.proposal_hash
    })
    setStatus(previous => previous && ({
      ...previous, approvals: previous.approvals.filter(row => row.id !== approval.id)
    }))
  }

  async function startJob() {
    if (!selectedAgent?.workflow || !input.trim()) return
    const started = await api<Job>(`${base}/workflows/${selectedAgent.workflow}/runs`, {
      request_id: requestId(), goal: input.trim(), agent_id: agent, model_id: model
    })
    setJob(started)
    setTab('jobs')
    setInput('')
    await refreshLists()
  }

  const active = status?.latest_admission
  const pending = status?.active_effects.filter(row => row.status === 'dispatched') ?? []
  const reconcileReady = !!evidence.trim() && pending.every(row => reviewed[row.id])
  return <div className="layout">
    <header className="top">
      <div className="brand"><span className="mark" aria-hidden="true">◈</span> deep-harness <small>v{__APP_VERSION__}</small></div>
      <span className="top-divider" />
      <label className="workspace">工作目录
        <select value={workspace} onChange={event => {
          setWorkspace(event.target.value); openSession(''); setJob(undefined); setUseShell(false); setError('')
        }}>
          {catalog?.workspaces.map(row => <option key={row.id} value={row.id}>{row.id}</option>)}
        </select>
      </label>
      <span className="connection"><i className={ready ? 'online' : ''} />{session ? (ready ? '实时连接' : '正在连接') : '本地实例'}</span>
    </header>
    <aside className="sidebar">
      <div className="switch">
        <button className={tab === 'sessions' ? 'selected' : ''} onClick={() => setTab('sessions')}>会话</button>
        <button className={tab === 'jobs' ? 'selected' : ''} onClick={() => setTab('jobs')}>工作流</button>
      </div>
      <button className="new" onClick={() => { openSession(''); setJob(undefined) }}>＋ 新建任务</button>
      <nav aria-label={tab === 'sessions' ? '会话' : '工作流'}>
        {tab === 'sessions' ? sessions.map(row =>
          <button key={row.id} className={row.id === session ? 'active' : ''} onClick={() => openSession(row.id)}>
            {short(row.prompt) || row.id}
          </button>) : jobs.map(row =>
          <button key={row.id} className={row.id === job?.id ? 'active' : ''} onClick={() => setJob(row)}>
            {short(row.goal)}<small>{stateText(row.status)}</small>
          </button>)}
      </nav>
    </aside>
    <main>
      {error && <div role="alert" className="error"><span>{error}</span><button onClick={() => setError('')}>关闭</button></div>}
      {tab === 'jobs' && job ? <section className="paper job">
        <div className="job-title"><h1>{job.goal}</h1><span className="pill">{stateText(job.status)}</span></div>
        {job.notice && <p>{job.notice}</p>}
        {['paused', 'running', 'blocked'].includes(job.status) && <div className="actions">
          <button disabled={busy} onClick={() => void perform(async () => {
            const action = job.status === 'running' ? 'pause' : 'resume'
            setJob(await api<Job>(`${base}/jobs/${job.id}/commands`, { action }))
          })}>{job.status === 'running' ? '暂停' : '继续'}</button>
          <button disabled={busy} onClick={() => void perform(async () => {
            setJob(await api<Job>(`${base}/jobs/${job.id}/commands`, { action: 'cancel' }))
          })}>取消工作流</button>
        </div>}
        <ol className="steps">{job.stages?.map(step => <li key={step.position}>
          <strong>{step.definition.title}</strong><span>{step.verified ? '已验收' : step.admission_status ? stateText(step.admission_status) : '等待前序'}</span>
          {step.session_id && <button onClick={() => openSession(step.session_id!)}>查看会话</button>}
        </li>)}</ol>
      </section> : session ? <>
        <div className="thread-head"><div><h1>会话</h1><small>{status?.model_id || '—'} · {status?.vision === false ? '仅文字' : '图文'} · {workspace}</small></div>
          {active && <span className="pill">{stateText(active.status)}</span>}
          {active && ['queued', 'started', 'waiting_approval'].includes(active.status) &&
            <button disabled={busy} onClick={() => void perform(async () => {
              await api(`${base}/runs/${active.id}/cancel`, {})
            })}>停止运行</button>}
        </div>
        <section className="thread" aria-label="消息">
          {older && <button onClick={() => void perform(async () => {
            const page = await api<{ items: Message[]; has_more: boolean }>(`${sessionPath}/messages?before=${messages[0].seq}`)
            setMessages(previous => [...page.items, ...previous])
            setOlder(page.has_more)
          })}>加载较早消息</button>}
          {!messages.length && <p className="empty">发送消息开始任务。</p>}
          {messages.map(row => <article key={row.seq} className={'message ' + row.message.role}>
            <h2>{row.message.role === 'user' ? '你' : row.message.role === 'tool' ? '工具' : 'Agent'}</h2>
            <div className="content">{typeof row.message.content === 'string' ? row.message.content : JSON.stringify(row.message.content, null, 2)}</div>
            <ImageStrip images={row.message.image_ids?.map(id => ({ id })) ?? []} load={loadImage}
              reuse={id => addImages([{ id }])} disabled={imagesDisabled || busy || Boolean(submission)}
              context={status?.latest_admission?.status === 'queued' && row.message.id === `${status.latest_admission.id}:user`
                ? undefined : status?.image_context ?? undefined} />
          </article>)}
          {status?.approvals.map(approval => <article key={approval.id} className="notice">
            <h2>待审批 · {approval.proposal.tool_name}</h2>
            <pre>{JSON.stringify(approval.proposal.arguments, null, 2)}</pre>
            <div className="actions"><button className="primary" disabled={busy} onClick={() => void perform(() => decide(approval, 'approve'))}>批准</button>
              <button disabled={busy} onClick={() => void perform(() => decide(approval, 'deny'))}>拒绝</button></div>
          </article>)}
          {active?.status === 'unknown' && <article className="notice">
            <h2>执行结果待对账</h2>
            <p>{active.result}</p>
            {pending.map(effect => <div key={effect.id} className="effect">
              <code>{effect.id}</code>
              <button disabled={busy} onClick={() => void perform(async () => {
                const value = await api<unknown>(`${base}/runs/${active.id}/effects/${encodeURIComponent(effect.id)}/status`)
                setLookup(previous => ({ ...previous, [effect.id]: value }))
              })}>查询外部状态</button>
              <select aria-label="对账结论" value={reviewed[effect.id] || ''} onChange={event => setReviewed(previous => ({ ...previous, [effect.id]: event.target.value }))}>
                <option value="">选择结论</option><option value="applied">已生效</option><option value="not_applied">未生效</option>
              </select>
              {effect.id in lookup && <pre>{JSON.stringify(lookup[effect.id], null, 2)}</pre>}
            </div>)}
            <textarea aria-label="对账证据" placeholder="记录核对证据" value={evidence} onChange={event => setEvidence(event.target.value)} />
            <button disabled={!reconcileReady || busy} onClick={() => void perform(async () => {
              await api(`${base}/runs/${active.id}/reconciliation`, {
                generation: active.generation, evidence,
                effects: pending.map(effect => ({
                  effect_id: effect.id, proposal_hash: effect.proposal_hash, outcome: reviewed[effect.id]
                })),
              })
              setEvidence('')
            })}>确认对账</button>
          </article>}
          {artifacts.some(row => !row.kind.startsWith('image/')) && <section className="artifacts"><h2>产物</h2>{artifacts.filter(row => !row.kind.startsWith('image/')).map(row =>
            <a key={row.id} href={`${base}/artifacts/${row.id}`} download>{row.name}<span>{row.size} B</span></a>
          )}</section>}
        </section>
        <form className="composer" onSubmit={send}
          onDragOver={event => { if (event.dataTransfer.types.includes('Files')) event.preventDefault() }}
          onDrop={event => dropImages(event, addFiles, imagesDisabled || busy || Boolean(submission))}>
          <textarea aria-label="任务输入" placeholder="输入任务或粘贴截图…" value={input} disabled={Boolean(submission)}
            onChange={event => setInput(event.target.value)}
            onPaste={event => { if (!imagesDisabled) pasteImages(event, addFiles) }} />
          <ImageStrip images={images} load={loadImage} disabled={busy || Boolean(submission)}
            remove={index => setImages(rows => rows.filter((_, position) => position !== index))} />
          <div className="actions">
            <ImagePicker add={addFiles} disabled={imagesDisabled || busy || Boolean(submission)} />
            <label className="upload">附件<input type="file" multiple disabled={busy || Boolean(submission)}
              onChange={event => {
                const selected = Array.from(event.target.files ?? [])
                const pictures = selected.filter(file => file.type.startsWith('image/') || /\.(png|jpe?g)$/i.test(file.name))
                if (pictures.length && imagesDisabled) setError('当前模型仅支持文字，请选择支持图文的模型。')
                else if (pictures.length) addFiles(pictures)
                setFiles(selected.filter(file => !pictures.includes(file)).slice(0, 8))
                event.currentTarget.value = ''
              }} /></label>
            <span>{files.map(file => file.name).join(' · ')}</span>
            <button className="primary" disabled={busy || (!input.trim() && !images.length && !submission)}>
              {submission ? '核对后重试相同请求' : '发送'}
            </button>
          </div>
        </form>
      </> : <section className="paper start">
        <span className="mark big" aria-hidden="true">◈</span>
        <h1>新建任务</h1>
        <div className="fields">
          <label>模型<select value={model} onChange={event => setModel(event.target.value)}>
            {catalog?.models.map(row => <option key={row.id} value={row.id} disabled={!row.configured}>{row.label} · {row.model} · {row.capabilities.includes('vision') ? '图文' : '仅文字'}</option>)}
          </select></label>
          <label>Agent<select value={agent} onChange={event => setAgent(event.target.value)}>
            <option value="">通用 Agent</option>
            {catalog?.agents.map(row => <option key={row.id} value={row.id}>{row.label}</option>)}
          </select></label>
        </div>
        {!agent && <label>Harness<select value={harness} onChange={event => setHarness(event.target.value)}>
          <option value="general">通用</option>
          <option value="research">研究</option>
          <option value="research_web">联网研究</option>
          <option value="coding">编码</option>
        </select></label>}
        {!agent && <div className="checks">
          <label><input type="checkbox" checked={useFiles} onChange={event => setUseFiles(event.target.checked)} /> 工作目录只读工具</label>
          {sandbox && !harness.startsWith('research') && <label><input type="checkbox" checked={useShell} onChange={event => setUseShell(event.target.checked)} /> 隔离命令执行</label>}
        </div>}
        {selectedAgent?.workflow && <textarea aria-label="工作流目标" placeholder="工作流目标" value={input} onChange={event => setInput(event.target.value)} />}
        <div className="actions"><button className="primary" disabled={busy || !catalog} onClick={() => void perform(create)}>创建会话</button>
          {selectedAgent?.workflow && <button disabled={busy || !input.trim()} onClick={() => void perform(startJob)}>运行工作流</button>}</div>
      </section>}
    </main>
  </div>
}

createRoot(document.getElementById('root')!).render(<Workbench />)
