import { LongTasks } from "./long-tasks"
import { createRoot } from 'react-dom/client'
import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Capabilities, CreateSession, Knowledge, Setup } from './components'
import { BundleImport } from './bundle-import'
import {
  $busy,
  $conversation,
  $error,
  $runtime,
  api,
  browserRuntime,
  initialize,
  loadSessions,
  openSession,
  perform,
  refreshConversation,
  loadOlderMessages,
  resetView,
  report,
  resourcePath,
  storageKey,
  workspaceApi
} from './state'
import { downloadResource, HttpError, imageResource, newRequestId } from './workspace-api'
import { AddProject } from './projects'
import { SessionList, sessionTitle } from './session-list'
import type { Admission, Message, RunEffect } from './protocol'
import { TaskComposer } from './task-composer'
import { TaskInspector } from './task-inspector'
import { webResultStatus } from './web-result'
import { Markdown, readReportText, type ReportText } from './markdown'
import { appendImages, ImageStrip, stageImages } from './image-input'
import type { ImageDraft } from './image-input'
import './styles.css'
import './images.css'

// build.mjs injects the package version.
declare const __APP_VERSION__: string

const starters = [
  [
    '⌘',
    '了解这个项目',
    '先阅读项目说明，再梳理架构与入口',
    '请阅读当前项目的 README、AGENTS.md、依赖清单和入口文件，依据代码介绍架构、主要功能和启动测试方法。不要修改文件。'
  ],
  [
    '↗',
    '一起实现功能',
    '描述需求，让 Agent 检查代码并实施',
    '请先阅读项目规范与相关实现，再完成以下功能并运行相关测试。功能需求：'
  ],
  [
    '⌕',
    '排查运行问题',
    '从复现步骤出发，定位并验证修复',
    '请帮我排查以下问题，先建立复现，再定位根因和验证修复。错误日志与复现步骤：'
  ],
  [
    '≋',
    '研究与整理',
    '结合项目证据形成可核对的结论',
    '请结合当前工作区中的文件研究以下问题，给出证据、结论与尚未验证的部分。研究问题：'
  ]
]
type Page = 'jobs' | 'chat' | 'capabilities' | 'skills' | 'memories' | 'mcp/tools' | 'settings' | 'bundles'

interface CodingReport extends ReportText {
  report_status: 'valid' | 'repaired' | 'unavailable'
  execution_evidence: {
    files: { path: string; sha256: string }[]
    workspace?: {
      status: 'observed' | 'partial' | 'unavailable'
      files: { path: string; status: 'added' | 'modified' | 'deleted' }[]
      diff: string
      diff_truncated: boolean
      isolated?: boolean
    }
    verification?: {
      status: 'not_run' | 'passed' | 'failed' | 'stale' | 'unavailable'
      receipt: { command: string; cwd: string; exit_code: number } | null
    }
    delegations?: { id: string; kind: string; mode: string; status: string }[]
    plan?: { content: string; status: string }[]
    commands: {
      receipt_id: string
      command: string
      exit_code: number | null
      purpose?: 'general' | 'reproduce' | 'verify'
    }[]
  }
}

const verificationLabels = {
  not_run: '未运行验收命令', passed: '验收通过', failed: '验收失败',
  stale: '验收已过期，工作区有变更', unavailable: '无法确认验收对应的工作区版本'
}
const fileChangeLabels = { added: '新增', modified: '修改', deleted: '删除' }

type Delivery = {
  revision?: string; status: string; diff: string; diff_truncated: boolean
  change_set_id?: string; bundle_error?: string
  files: { path: string; status: 'added' | 'modified' | 'deleted' }[]
  delivery: { revision: string; status: string; observations?: Record<string, string[]> } | null
}

function TaskDelivery({ sessionId }: { sessionId: string }) {
  const [review, setReview] = useState<Delivery | null>(null)
  const busy = useStore($busy)
  const route = `/sessions/${sessionId}/delivery`
  const refresh = async () => setReview(await api<Delivery>(route))
  const command = (action: 'apply' | 'undo' | 'discard' | 'reconcile', revision: string) => void perform(async () => {
    await api(route, 'POST', { action, revision,
      ...(action === 'apply' ? { change_set_id: review?.change_set_id } : {}) })
    await refresh()
  })
  return <div className="delivery-panel">
    <button disabled={busy} onClick={() => void perform(refresh)}>审查任务交付</button>
    {review && <>
      <ul>{review.files.map(file => <li key={file.path}>{fileChangeLabels[file.status]} <code>{file.path}</code></li>)}</ul>
      {review.diff && <pre className="coding-diff">{review.diff}</pre>}
      {review.diff_truncated && <p>文本差异仅显示可捕获部分。</p>}
      {review.bundle_error && <p>当前变更无法封装，需要缩小交付范围。</p>}
      {review.delivery?.observations && <ul>
        {Object.entries(review.delivery.observations).map(([path, states]) => (
          <li key={path}><code>{path}</code> · {states.map(value =>
            value === 'before' ? '交付前' : '交付后').join('、') || '与两端都不匹配'}</li>
        ))}
      </ul>}
      {review.delivery?.status === 'applied' ? (
        <button disabled={busy} onClick={() => command('undo', review.delivery!.revision)}>撤销已应用变更</button>
      ) : review.delivery && ['applying', 'undoing'].includes(review.delivery.status) ? (
        <button disabled={busy} onClick={() => command('reconcile', review.delivery!.revision)}>核对交付</button>
      ) : (!review.delivery || review.delivery.status === 'not_applied') &&
          review.status === 'observed' && review.files.length > 0 && review.revision &&
          review.change_set_id ? (
        <button disabled={busy} onClick={() => command('apply', review.revision!)}>应用到项目</button>
      ) : review.delivery && <p>交付状态 · {review.delivery.status}</p>}
      {review.status === 'discarded' && <p>任务工作区已清理。</p>}
      {review.status === 'observed' && review.revision && (
        <button disabled={busy} onClick={() => {
          if (window.confirm('清理任务工作区？未交付的改动将被丢弃。')) command('discard', review.revision!)
        }}>清理工作区</button>
      )}
    </>}
  </div>
}

type DevProcess = { id: string; command: string; status: string; stdout: string; stderr: string }

function DevelopmentProcesses({ sessionId }: { sessionId: string }) {
  const busy = useStore($busy)
  const [open, setOpen] = useState(false)
  const [command, setCommand] = useState('')
  const [pending, setPending] = useState<{ command: string; request_id: string } | null>(null)
  const [rows, setRows] = useState<DevProcess[]>([])
  const route = `/sessions/${sessionId}/processes`
  const refresh = async () => setRows(await workspaceApi<DevProcess[]>(route))
  useEffect(() => {
    if (!open) return
    let active = true
    const update = () => void workspaceApi<DevProcess[]>(route).then(value => {
      if (active) setRows(value)
    }).catch(error => { if (active) report(error) })
    update()
    const timer = setInterval(update, 2000)
    return () => { active = false; clearInterval(timer) }
  }, [open, route])

  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>开发进程</summary>
    <form onSubmit={event => {
      event.preventDefault()
      void perform(async () => {
        const request = pending ?? { command: command.trim(), request_id: newRequestId() }
        setPending(request)
        await workspaceApi(route, 'POST', request)
        setPending(null)
        setCommand('')
        await refresh()
      })
    }}>
      <input aria-label="开发命令" placeholder="例如 npm run dev" value={command} disabled={busy || !!pending}
        onChange={event => setCommand(event.target.value)} />
      <button disabled={busy || !command.trim()}>{pending ? '核对启动结果' : '启动进程'}</button>
    </form>
    {rows.map(row => <details key={row.id}>
      <summary>{row.command} · {row.status}</summary>
      {(row.stdout || row.stderr) && <pre className="coding-diff">{row.stdout}{row.stderr}</pre>}
      {row.status === 'running' && <button disabled={busy} onClick={() => void perform(async () => {
        await workspaceApi(`${route}/${row.id}/stop`, 'POST', {})
        await refresh()
      })}>停止进程</button>}
    </details>)}
  </details>
}

function readCodingReport(value?: string): CodingReport | null {
  try {
    const parsed = JSON.parse(value ?? '') as CodingReport
    const body = readReportText(parsed)
    if (body && parsed.report_status && Array.isArray(parsed.execution_evidence?.files) &&
        Array.isArray(parsed.execution_evidence?.commands)) return { ...parsed, ...body }
  } catch {
    return null
  }
  return null
}

function openMarkdownLink(href: string) {
  if (window.deepHarness) void window.deepHarness.openLink(href).catch(report)
  else window.open(href, '_blank', 'noopener,noreferrer')
}

function RunArtifacts({ runId, status, revision }: { runId: string; status: string; revision?: number }) {
  const [rows, setRows] = useState<{ id: string; name: string; kind: string }[]>([])
  useEffect(() => {
    if (status !== 'completed') return
    let active = true
    void workspaceApi<typeof rows>(`/runs/${runId}/artifacts`).then(data => {
      if (active) setRows(data.filter(row => !row.kind.startsWith('image/')))
    }).catch(error => { if (active) report(error) })
    return () => { active = false }
  }, [runId, status, revision])
  if (!rows.length) return null
  return <section className="notice"><h3>产物</h3>{rows.map(row =>
    <button key={row.id} onClick={() => void perform(() =>
      downloadResource(resourcePath(`/artifacts/${row.id}`), row.name)
    )}>{row.name}</button>)}</section>
}

function HistorySearch({ sessionId }: { sessionId: string }) {
  const [query, setQuery] = useState('')
  const [matches, setMatches] = useState<Message[] | null>(null)
  const busy = useStore($busy)
  return <details>
    <summary>搜索历史</summary>
    <form onSubmit={event => {
      event.preventDefault()
      void perform(async () => {
        const result = await workspaceApi<{ messages: Message[] }>(
          `/sessions/${sessionId}/messages/search?q=${encodeURIComponent(query.trim())}`
        )
        setMatches(result.messages)
      })
    }}>
      <input aria-label="搜索历史" value={query} maxLength={200}
        onChange={event => { setQuery(event.target.value); setMatches(null) }} />
      <button disabled={busy || !query.trim()}>搜索</button>
    </form>
    {matches?.length === 0 && <p>没有匹配的已完成消息。</p>}
    {matches?.map((message, index) => <article key={message.id ?? index}>
      <strong>{message.role}</strong>
      <ReplyMarkdown text={typeof message.content === 'string' ? message.content : JSON.stringify(message.content)} />
    </article>)}
  </details>
}

function RunReconciliation({ run, effects, connector }: {
  run: Admission; effects: RunEffect[]; connector: boolean
}) {
  const busy = useStore($busy)
  const [evidence, setEvidence] = useState('')
  const [review, setReview] = useState<Record<string, string>>({})
  const [lookup, setLookup] = useState<Record<string, unknown>>({})
  return <section className="notice">
    <h3>执行结果对账</h3>
    {effects.map(effect => <div key={effect.id}>
      <code>{effect.id}</code>
      {connector && <button disabled={busy} onClick={() => void perform(async () => {
        const result = await workspaceApi(`/runs/${run.id}/effects/${encodeURIComponent(effect.id)}/status`)
        setLookup(previous => ({ ...previous, [effect.id]: result }))
      })}>查询外部状态</button>}
      <select aria-label="对账结论" value={review[effect.id] || ''} onChange={event => {
        const outcome = event.target.value
        setReview(previous => ({ ...previous, [effect.id]: outcome }))
      }}>
        <option value="">选择结论</option><option value="applied">已生效</option>
        <option value="not_applied">未生效</option>
      </select>
      {effect.id in lookup && <pre>{JSON.stringify(lookup[effect.id], null, 2)}</pre>}
    </div>)}
    <label>对账证据<textarea value={evidence} maxLength={2000} onChange={e => setEvidence(e.target.value)} /></label>
    <button disabled={busy || !evidence.trim() || effects.some(effect => !review[effect.id])}
      onClick={() => void perform(async () => {
        await workspaceApi(`/runs/${run.id}/reconciliation`, 'POST', {
          generation: run.generation, evidence, effects: effects.map(effect => ({
            effect_id: effect.id, proposal_hash: effect.proposal_hash, outcome: review[effect.id]
          }))
        })
        await refreshConversation()
      })}>确认对账</button>
  </section>
}

function ReplyMarkdown({ text }: { text: string }) {
  return <Markdown text={text} openLink={openMarkdownLink} />
}

function ReportBody({ body, showActions = false }: { body: ReportText; showActions?: boolean }) {
  const sections = [
    ...(showActions ? [['改动', body.changes], ['验证', body.tests]] as const : []),
    ['限制与未验证事项', body.limitations] as const
  ]
  return <div className="report-body">
    <ReplyMarkdown text={body.summary} />
    {body.evidence.length > 0 && <section>
      <h3>代码依据</h3>
      <ul>{body.evidence.map((entry, index) => (
        <li key={index}><code>{entry.source}</code><ReplyMarkdown text={entry.observation} /></li>
      ))}</ul>
    </section>}
    {sections.filter(([, items]) => items.length > 0).map(([title, items]) => (
      <section key={title}>
        <h3>{title}</h3>
        <ul>{items.map((item, index) => <li key={index}><ReplyMarkdown text={item} /></li>)}</ul>
      </section>
    ))}
  </div>
}

function Chat({ newSession, openModelSettings }: { newSession: () => void; openModelSettings: () => void }) {
  const conversation = useStore($conversation)
  const runtime = useStore($runtime)!
  const server = Boolean(runtime.catalog)
  const busy = useStore($busy)
  const sessionId = conversation?.id ?? ''
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState<{ submission_id: string; text: string; image_ids?: string[]; artifact_ids?: string[] } | null>(null)
  const [images, setImages] = useState<ImageDraft[]>([])
  const [files, setFiles] = useState<File[]>([])
  const loadImage = useCallback((id: string, full = false) => imageResource(resourcePath(
    `/sessions/${sessionId}/images/${id}${full ? '' : '?thumbnail=true'}`
  )), [sessionId])
  const tail = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  useEffect(() => {
    follow.current = true
    setDraft(localStorage.getItem(storageKey('draft', sessionId)) || '')
    const raw = localStorage.getItem(storageKey('pending', sessionId))
    const saved = raw ? JSON.parse(raw) : null
    setPending(saved)
    setFiles([])
    setImages(saved?.image_ids?.map((id: string) => ({ id })) ?? [])
  }, [sessionId])
  const updateDraft = (value: string) => {
    setDraft(value)
    localStorage.setItem(storageKey('draft', sessionId), value)
  }
  const status = conversation?.status
  const latest = status?.latest_admission
  const imagesDisabled = !sessionId || status?.vision === false
  const addImages = (items: ImageDraft[]) => {
    if (busy || pending || imagesDisabled) return
    const result = appendImages(images, items)
    setImages(result.images)
    if (result.error) report(result.error)
  }
  const codingReport = status?.session.harness === 'coding' && latest?.status === 'completed'
    ? readCodingReport(latest.result)
    : null
  const running = status?.active_admissions.some(a => ['queued', 'started', 'waiting_approval'].includes(a.status))
  // Only unknown/reconciled mean a write may have landed without a receipt. An
  // interrupted run is a deterministic local stop with no uncertain side effects.
  const uncertain = status?.active_admissions.find(a => ['unknown', 'reconciled'].includes(a.status))
  const interrupted = status?.active_admissions.find(a => a.status === 'interrupted')
  const paused = uncertain || interrupted
  const queuedCount = status?.active_admissions.filter(a => a.status === 'queued').length ?? 0
  const workspaceEvent = latest && conversation?.events.findLast(event =>
    event.payload.admission_id === latest.id &&
    (event.type.startsWith('workspace.') || event.type.startsWith('tool.') ||
      event.type === 'model.started' || event.type === 'message.committed'))
  const workspacePhase = workspaceEvent?.type
  const workspaceWaiting = (latest?.status === 'queued' || latest?.status === 'started') &&
    workspacePhase === 'workspace.waiting'
  const workspacePreparing = latest?.status === 'queued' &&
    (workspacePhase === 'workspace.preparing' || workspacePhase === 'workspace.baseline')
  const retryWorkspace = interrupted?.generation === 0 &&
    /^Workspace preparation stopped \[(workspace_busy|workspace_timeout)\]$/.test(interrupted.result ?? '')
  const retrySummary = status?.session.harness === 'coding' && !status?.job_id &&
    /^部分结果 \[(coding_finalization|coding_finalization_timeout|coding_finalization_tool_request)\]/.test(interrupted?.result ?? '')
  const workspaceAction = (admissionId: string, action: 'retry-workspace' | 'retry-summary') =>
    void perform(async () => {
      await workspaceApi(`/runs/${admissionId}/${action}`, 'POST')
      await refreshConversation()
    })
  const send = () =>
    void perform(async () => {
      if (paused || running || status?.pause_requested ||
          status?.active_admissions.some(run => run.status === 'job_paused')) return
      if (!sessionId) {
        newSession()
        return
      }
      if (!pending && !draft.trim() && !images.length) return
      const imageIds = pending ? pending.image_ids : await stageImages(images, async file => {
        const uploaded = await workspaceApi<{ id: string }>(
          `/sessions/${sessionId}/images`, 'POST', undefined,
          { name: file.name, data: await file.arrayBuffer() }
        )
        return uploaded.id
      }, staged => { if ($conversation.get()?.id === sessionId) setImages(staged) })
      const artifactIds = pending?.artifact_ids ?? []
      if (!pending) for (const file of files) {
        const uploaded = await workspaceApi<{ id: string }>(
          `/sessions/${sessionId}/attachments`, 'POST', undefined,
          { name: file.name, data: await file.arrayBuffer() }
        )
        artifactIds.push(uploaded.id)
      }
      const submission = pending || {
        submission_id: newRequestId(), text: draft.trim(), image_ids: imageIds, artifact_ids: artifactIds
      }
      localStorage.setItem(storageKey('pending', sessionId), JSON.stringify(submission))
      setPending(submission)
      const existing = pending && await workspaceApi(
        `/sessions/${sessionId}/submission?submission_id=${encodeURIComponent(submission.submission_id)}`
      )
      if (!existing) {
        try {
          await workspaceApi(`/sessions/${sessionId}/runs`, 'POST', submission)
        } catch (error) {
          if (error instanceof HttpError && error.status >= 400 && error.status < 500) {
            localStorage.removeItem(storageKey('pending', sessionId))
            if ($conversation.get()?.id === sessionId) setPending(null)
          }
          throw error
        }
      }
      localStorage.removeItem(storageKey('pending', sessionId))
      localStorage.removeItem(storageKey('draft', sessionId))
      if ($conversation.get()?.id === sessionId) {
        setPending(null)
        setImages([])
        setFiles([])
        setDraft('')
      }
      await refreshConversation()
    })
  const visibleRun = status?.active_admissions.find(run => run.status === 'started') ?? latest
  const messageId = conversation?.events.findLast(event => event.type === 'model.started' &&
    typeof event.payload.id === 'string' && event.payload.id.startsWith(`${visibleRun?.id}:`))?.payload.id
  const showDeltas = messageId && visibleRun && ['started', 'completed'].includes(visibleRun.status) &&
    !conversation?.messages.some(message => message.id === messageId)
  const deltas = (showDeltas ? conversation?.events ?? [] : [])
    .filter(e => e.type === 'model.delta' && e.payload.message_id === messageId)
    .map(e => String(e.payload.text ?? ''))
    .join('')
  const commandEvents = conversation?.events.filter(e =>
    e.type === 'coding.command_output' && String(e.payload.id).startsWith(`${latest?.id}:`)
  ) ?? []
  const lastCommand = commandEvents.at(-1)?.payload.id
  const commandOutput = commandEvents.filter(e => e.payload.id === lastCommand)
    .map(e => String(e.payload.content ?? '')).join('')
  useEffect(() => {
    if (conversation && follow.current) tail.current?.scrollIntoView({ block: 'end' })
  }, [conversation?.messages.length, deltas])
  return (
    <div className="workspace">
      <main className="chat">
        <header className="chat-header">
          <div>
            <span className="eyebrow">WORKSPACE</span>
            <p>{status?.session ? sessionTitle(status.session)
              : sessionId ? `会话 ${sessionId.slice(0, 10)}` : '开始新的工作'}</p>
          </div>
          <span className="chip">
            {status?.session.harness ?? 'Ollama'}
          </span>
          {sessionId && <HistorySearch key={sessionId} sessionId={sessionId} />}
        </header>
        <div
          className="transcript"
          onScroll={e => {
            const node = e.currentTarget
            follow.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80
          }}
        >
          {!conversation && (
            <section className="welcome">
              <span className="hero-symbol">
                H<span>✦</span>
              </span>
              <div className="eyebrow">YOUR LOCAL AGENT WORKSPACE</div>
              <h1>让想法，成为下一步。</h1>
              <p className="muted">deep-harness · 读懂项目、执行任务、保留你的工作上下文。</p>
              <div className="starter-grid">
                {starters.map(([icon, title, detail, prompt]) => (
                  <button
                    className="starter"
                    key={title}
                    onClick={() => {
                      updateDraft(prompt)
                      newSession()
                    }}
                  >
                    <span className="starter-icon">{icon}</span>
                    <strong>{title}</strong>
                    <small>{detail}</small>
                  </button>
                ))}
              </div>
              <p className="local-note">
                <span className="dot" />
                本地运行，无需登录 · 由你选择批准方式
              </p>
            </section>
          )}
          {conversation?.hasOlderMessages && (
            <button onClick={event => {
              const panel = event.currentTarget.closest('.transcript')
              const height = panel?.scrollHeight ?? 0
              follow.current = false
              void perform(async () => {
                await loadOlderMessages()
                requestAnimationFrame(() => {
                  if (panel) panel.scrollTop += panel.scrollHeight - height
                })
              })
            }}>加载更早的消息</button>
          )}
          {conversation?.messages
            .filter(message => message.image_ids?.length || (message.content !== '' && message.content !== null))
            .map((message, index) => {
              const reportText = message.role === 'assistant' ? readReportText(message.content) : null
              if (reportText && codingReport && latest && message.id.startsWith(`${latest.id}:`)) {
                return null
              }
              return <article className={`message ${message.role}`} key={message.id || index}>
                <div className="message-role">
                  {message.role === 'user' ? '你' : message.role === 'assistant' ? 'deep-harness' : message.role}
                </div>
                {message.role === 'tool' ? (
                  <details>
                    <summary>{webResultStatus(message.content) ?? '工具执行结果'} · 展开查看</summary>
                    <pre className="message-content raw-tool-output">
                      {typeof message.content === 'string' ? message.content : JSON.stringify(message.content, null, 2)}
                    </pre>
                  </details>
                ) : (
                  <div className="message-content">
                    {reportText ? <ReportBody body={reportText} showActions /> :
                      typeof message.content === 'string' ? <ReplyMarkdown text={message.content} /> :
                        <pre>{JSON.stringify(message.content, null, 2)}</pre>}
                  </div>
                )}
                <ImageStrip images={message.image_ids?.map(id => ({ id })) ?? []} load={loadImage}
                  reuse={id => addImages([{ id }])} disabled={imagesDisabled || busy || Boolean(pending)}
                  context={status?.active_admissions.some(row => message.id === `${row.id}:user` && row.status === 'queued')
                    ? undefined : status?.image_context ?? undefined} />
              </article>
            })}
          {latest && !conversation?.messages.some(message => message.id === `${latest.id}:user`) && (
            <article className="message user">
              <div className="message-role">当前任务 · {latest.status}</div>
              <div className="message-content"><ReplyMarkdown text={latest.text} /></div>
            </article>
          )}
          {codingReport && (
            <article className="message assistant">
              <div className="message-role">编码执行结果</div>
              <div className="message-content">
                {codingReport.report_status === 'unavailable'
                  ? <p>执行已结束，报告生成失败。</p> : <ReportBody body={codingReport} />}
                {codingReport.execution_evidence.verification && (
                  <p>
                    {verificationLabels[codingReport.execution_evidence.verification.status]}
                    {codingReport.execution_evidence.verification.receipt && (
                      <> · <code>{codingReport.execution_evidence.verification.receipt.command}</code></>
                    )}
                  </p>
                )}
                {codingReport.execution_evidence.workspace &&
                  codingReport.execution_evidence.workspace.status !== 'unavailable' && (
                  <details>
                    <summary>工作区观测差异 · {codingReport.execution_evidence.workspace.files.length} 个文件</summary>
                    {!codingReport.execution_evidence.workspace.isolated && (
                      <p>当前目录的差异可能包含同时发生的外部修改。</p>
                    )}
                    {codingReport.execution_evidence.workspace.status !== 'observed' && (
                      <p>工作区快照不完整。</p>
                    )}
                    <ul>{codingReport.execution_evidence.workspace.files.map(file => (
                      <li key={file.path}>{fileChangeLabels[file.status]} <code>{file.path}</code></li>
                    ))}</ul>
                    {codingReport.execution_evidence.workspace.diff_truncated && <p>文本差异仅包含可捕获的部分。</p>}
                    {codingReport.execution_evidence.workspace.diff && (
                      <pre className="coding-diff">{codingReport.execution_evidence.workspace.diff}</pre>
                    )}
                  </details>
                )}
                {codingReport.execution_evidence.workspace?.diff && latest && <button
                  onClick={() => void perform(() => downloadResource(
                    resourcePath(`/runs/${latest.id}/diff`), 'coding-task.diff'
                  ))}>下载差异</button>}
                {Boolean(codingReport.execution_evidence.delegations?.length) && (
                  <details>
                    <summary>子 Agent · {codingReport.execution_evidence.delegations?.length}</summary>
                    <ul>{codingReport.execution_evidence.delegations?.map(child => (
                      <li key={child.id}>{child.kind === 'coding' ? '实现' :
                        child.kind === 'review' ? '审查' : '调查'} · {child.status}</li>
                    ))}</ul>
                  </details>
                )}
                {Boolean(codingReport.execution_evidence.plan?.length) && (
                  <details><summary>任务计划</summary>
                    <ul>{codingReport.execution_evidence.plan?.map((item, index) => (
                      <li key={index}>{item.content} · {item.status}</li>
                    ))}</ul>
                  </details>
                )}
                {(!codingReport.execution_evidence.workspace?.isolated ||
                  codingReport.execution_evidence.workspace.status === 'unavailable') &&
                  codingReport.execution_evidence.files.length > 0 && (
                  <ul>{codingReport.execution_evidence.files.map(file => (
                    <li key={file.path}>已修改 <code>{file.path}</code></li>
                  ))}</ul>
                )}
                {codingReport.execution_evidence.workspace?.isolated && sessionId && (
                  <>
                    {!server && <TaskDelivery key={latest?.id} sessionId={sessionId} />}
                    <button onClick={() => void perform(() => downloadResource(
                      resourcePath(`/sessions/${sessionId}/bundle`), 'changes.json'
                    ))}>下载任务变更包</button>
                  </>
                )}
                {codingReport.execution_evidence.commands.length > 0 && (
                  <ul>{codingReport.execution_evidence.commands.map(command => (
                    <li key={command.receipt_id}>
                      {command.purpose === 'reproduce' ? '复现 · ' :
                        command.purpose === 'verify' ? '验收 · ' : ''}
                      <code>{command.command}</code> · 退出码 {command.exit_code ?? '未取得'}
                    </li>
                  ))}</ul>
                )}
              </div>
            </article>
          )}
          {deltas && (
            <article className="message assistant">
              <div className="message-role">deep-harness · 生成中</div>
              <div className="message-content"><ReplyMarkdown text={deltas} /></div>
            </article>
          )}
          {running && commandOutput && (
            <article className="message assistant">
              <div className="message-role">当前命令输出</div>
              <pre className="coding-diff">{commandOutput}</pre>
            </article>
          )}
          {(paused?.result || (latest?.status !== 'completed' && latest?.result)) && (
            <div className="notice">
              <ReplyMarkdown text={paused?.result || latest?.result || ''} />
            </div>
          )}
          {latest?.status === 'completed' && !latest.result && !running && (
            <p className="notice">
              本次执行已完成，但模型没有返回文本结果。常见原因是模型开启了 thinking / reasoning
              模式，回答被写入思考字段而未被读取（qwen3.5 等思考模型默认如此）。请确认配置中
              reasoning 为 false，或换用非思考模型，然后新建会话重试。
            </p>
          )}
          {status?.runtime?.compatible === false && (
            <p className="notice">
              当前配置与会话快照不同。请恢复原配置，或审阅后新建分支。
              {status.runtime.changed_fields?.length
                ? ` 差异字段 ${status.runtime.changed_fields.join('、')}` : ''}
            </p>
          )}
          {latest && <RunArtifacts key={latest.id} runId={latest.id} status={latest.status}
            revision={conversation?.events.findLast(event =>
              event.type === 'artifact.ready' && event.payload.run_id === latest.id)?.seq} />}
          {status?.managed_task && <DevelopmentProcesses key={sessionId} sessionId={sessionId} />}
          {uncertain && (
            <div className="notice">
              <strong>任务结果不确定，已停止自动推进。</strong>
              {queuedCount > 0 && <p>此会话有 {queuedCount} 条消息尚未执行。</p>}
              <p>核对文件与右侧状态台账后，创建分支并重新发送问题。分支只复制完整已完成轮次。</p>
              {uncertain.status === 'unknown' && <RunReconciliation key={uncertain.id} run={uncertain}
                connector={server} effects={(status?.active_effects ?? []).filter(effect =>
                  effect.status === 'dispatched' && effect.admission_id === uncertain.id)} />}
              {uncertain.status === 'reconciled' && <button
                disabled={busy}
                onClick={() =>
                  void perform(async () => {
                    const fork = await api<{ id: string }>(`/sessions/${sessionId}/forks`, 'POST', {
                      request_id: newRequestId()
                    })
                    await loadSessions()
                    await openSession(fork.id)
                  })
                }
              >
                我已核对，创建新分支
              </button>}
            </div>
          )}
          {latest && (workspaceWaiting || workspacePreparing) && (
            <div className="notice">
              <strong>{workspaceWaiting ? '正在等待项目锁' :
                workspacePhase === 'workspace.baseline' ? '正在记录隔离工作区基线' : '正在准备隔离工作区'}</strong>
            </div>
          )}
          {interrupted && (
            <div className="notice">
              <strong>{retrySummary ? '已有项目证据，汇总未完成。' : '任务已停止，请查看上方原因。'}</strong>
              {(status?.active_effects ?? []).some(effect =>
                effect.status === 'dispatched' && effect.admission_id === interrupted.id) &&
                <RunReconciliation key={interrupted.id} run={interrupted} connector={server}
                  effects={(status?.active_effects ?? []).filter(effect =>
                    effect.status === 'dispatched' && effect.admission_id === interrupted.id)} />}
              {retrySummary ? <>
                <p>只使用已读取的证据重新生成答复。</p>
                <button disabled={busy} onClick={() => workspaceAction(interrupted.id, 'retry-summary')}>
                  仅重试汇总
                </button>
              </> : retryWorkspace ? <>
                <p>模型和工具尚未运行。项目可用后，可重试这条输入。</p>
                <button disabled={busy} onClick={() => workspaceAction(interrupted.id, 'retry-workspace')}>
                  重试工作区准备
                </button>
              </> : <>
                {interrupted.result?.includes('[workspace_git]') && <p>Git 工作树需要已有提交。可新建会话并选择当前项目或隔离副本。</p>}
                {interrupted.result?.includes('[workspace_incomplete]') && <p>项目副本超过复制上限或包含无法读取的文件。</p>}
                {interrupted.result?.includes('[workspace_busy]') && <p>项目正被其他操作占用，等待该操作完成后再试。</p>}
                <button disabled={busy} onClick={newSession}>新建会话</button>
              </>}
            </div>
          )}
          <div ref={tail} />
        </div>
        {status?.job_id ? (
          <p className="notice">此会话属于长任务阶段。可在右侧审批，在「长任务」页控制执行。</p>
        ) : (
          <TaskComposer
            draft={draft} pending={Boolean(pending)} paused={Boolean(paused)}
            updateDraft={updateDraft} send={send} openModelSettings={openModelSettings}
            images={images} loadImage={loadImage}
            imagesDisabled={imagesDisabled} addImages={files => addImages(files.map(file => ({ file })))}
            removeImage={index => setImages(rows => rows.filter((_, position) => position !== index))}
            files={files} setFiles={setFiles}
          />
        )}
      </main>
      <TaskInspector />
    </div>
  )
}

function FolderIcon({ open }: { open: boolean }) {
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className="project-folder-icon">
    <path d={open
      ? 'M3 18V6a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v1M3 18l3-7h15a1 1 0 0 1 .9 1.5l-2.5 5.7a2 2 0 0 1-1.8 1.2H5a2 2 0 0 1-2-1.4Z'
      : 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z'} />
    {!open && <path d="M3 10h18" />}
  </svg>
}

function App() {
  const runtime = useStore($runtime)
  const busy = useStore($busy)
  const error = useStore($error)
  const [page, setPage] = useState<Page>('chat')
  const [creating, setCreating] = useState(false)
  const [addingProject, setAddingProject] = useState(false)
  const [mobileNav, setMobileNav] = useState(false)
  const [projectsOpen, setProjectsOpen] = useState(true)
  const [projectSessionsOpen, setProjectSessionsOpen] = useState(true)
  const selectProject = (id: string | null) => void perform(async () => {
    if (id === null && !window.deepHarness) {
      setAddingProject(true)
      return
    }
    const next = window.deepHarness ? await window.deepHarness.openProject(id) : await browserRuntime(id ?? undefined)
    const previous = $runtime.get()
    if (previous && next.projectId === previous.projectId && next.ready === previous.ready) return
    resetView()
    $runtime.set(next)
    setProjectsOpen(true)
    setProjectSessionsOpen(true)
    setCreating(false)
    setPage(next.ready ? 'chat' : 'settings')
    if (next.ready) await loadSessions()
  })
  useEffect(() => {
    void initialize().catch(report)
    if (!window.deepHarness) return
    const timer = setInterval(() => {
      void window.deepHarness
        .info()
        .then(info => {
          const previous = $runtime.get()
          if (previous?.projectId !== info.projectId) return
          if (previous?.ready !== info.ready || previous?.error !== info.error) {
            if (!info.ready) resetView()
            $runtime.set(info)
          }
        })
        .catch(report)
    }, 3000)
    return () => clearInterval(timer)
  }, [])
  if (!runtime)
    return (
      <div className="boot">
        <span className="hero-symbol">H✦</span>
        <h2>正在打开工作台…</h2>
        {error && <p className="notice">{error}</p>}
        {error && <button onClick={() => void initialize().catch(report)}>重试连接</button>}
      </div>
    )
  const ready = runtime.ready
  const manageKnowledge = !runtime.catalog ||
    runtime.catalog.workspaces.find(row => row.id === runtime.projectId)?.manage_knowledge
  return (
    <div className={`app${mobileNav ? ' mobile-nav-open' : ''}`}>
      <button type="button" className="mobile-nav-toggle" aria-expanded={mobileNav}
        onClick={() => setMobileNav(open => !open)}>{mobileNav ? '关闭导航' : '项目和历史'}</button>
      <aside className="sidebar">
        <div className="brand">
          <span>D✦</span>
          <div>
            deep-<b>harness</b>
            <small>AGENT WORKSPACE</small>
          </div>
        </div>
        <button className="new-task" disabled={!ready} onClick={() => {
          setProjectsOpen(true); setProjectSessionsOpen(true); setMobileNav(false); setCreating(true)
        }}>
          ＋ 新建会话
        </button>
        <div className="section-label project-heading">
          <button type="button" className="project-group-toggle" aria-expanded={projectsOpen}
            aria-controls="sidebar-projects" onClick={() => setProjectsOpen(open => !open)}>
            项目 <span className="project-chevron" aria-hidden="true" />
          </button>
          {(!runtime.catalog || runtime.catalog.local_projects) &&
            <button disabled={busy} onClick={() => selectProject(null)}>＋ 新增项目</button>}
        </div>
        <nav id="sidebar-projects" className="project-list" aria-label="项目" hidden={!projectsOpen}>
          {runtime.projects?.map(project => {
            const active = runtime.projectId === project.id
            const expanded = active && projectSessionsOpen
            const sessionsId = `project-sessions-${project.id}`
            return <div className="project-entry" key={project.id}>
              <button type="button" className={`project-toggle${active ? ' nav-active' : ''}`}
                title={project.workspace} disabled={busy} aria-current={active ? 'true' : undefined}
                aria-expanded={expanded} aria-controls={active ? sessionsId : undefined}
                onClick={() => active ? setProjectSessionsOpen(open => !open) : selectProject(project.id)}>
                <FolderIcon open={projectsOpen && expanded} />
                <span className="project-name">{project.name}</span>
              </button>
              {active && <div className="project-sessions" id={sessionsId} hidden={!expanded}>
                <SessionList ready={ready} open={id => {
                  setPage('chat')
                  setMobileNav(false)
                  void openSession(id).catch(report)
                }} />
              </div>}
            </div>
          })}
        </nav>
        <nav>
          {(
            [
              ['chat', '◈', '工作台'],
              ['jobs', '◇', '长任务'],
              ['bundles', '⇩', '导入变更包'],
              ['capabilities', '◎', '能力'],
              ['skills', '◫', '技能库'],
              ['memories', '◉', '记忆'],
              ['mcp/tools', '⌘', 'MCP 工具']
            ] as const
          ).filter(([key]) => !runtime.catalog || ['chat', 'jobs', 'capabilities'].includes(key) ||
              (manageKnowledge && ['skills', 'memories'].includes(key)))
            .map(([key, icon, label]) => (
            <button
              key={key}
              disabled={!ready}
              className={page === key ? 'nav-active' : ''}
              onClick={() => { setPage(key); setMobileNav(false) }}
            >
              <span>{icon}</span>
              {label}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          {!runtime.catalog && <button onClick={() => setPage('settings')}>⚙ 运行设置</button>}
          <div className="connection">
            <span className={ready ? 'dot' : 'dot offline'} />
            {ready ? (runtime.catalog ? '服务器已连接' : '本机 Agent 已连接') : '等待配置'}
            <small>v{__APP_VERSION__} · 独立开发版</small>
          </div>
        </div>
      </aside>
      <div className="main-area">
        {error && (
          <div className="error-banner" role="alert">
            <span>{error}</span>
            <button aria-label="关闭错误提示" onClick={() => $error.set('')}>
              ×
            </button>
          </div>
        )}
        {runtime.catalog && !ready ? (
          <p className="notice">当前实例没有可用的工作区。</p>
        ) : !ready || page === 'settings' ? (
          <Setup key={runtime.projectId || 'setup'} onClose={ready ? () => setPage('chat') : undefined} />
        ) : page === 'chat' ? (
          <Chat key={runtime.projectId} newSession={() => setCreating(true)}
            openModelSettings={() => runtime.catalog ? setCreating(true) : setPage('settings')} />
        ) : page === 'jobs' ? (
          <LongTasks key={runtime.projectId} openStage={id => {
            setPage('chat')
            void openSession(id).catch(report)
          }} />
        ) : page === 'bundles' ? (
          <BundleImport key={runtime.projectId ?? runtime.settings.workspace} workspace={runtime.settings.workspace} />
        ) : page === 'capabilities' ? (
          <Capabilities />
        ) : (
          <Knowledge key={page} kind={page} />
        )}
      </div>
      {creating && (
        <CreateSession
          close={() => {
            setCreating(false)
            setPage('chat')
          }}
        />
      )}
      {addingProject && runtime.catalog && <AddProject sandbox={runtime.catalog.project_sandbox}
        onClose={() => setAddingProject(false)} onAdd={project => {
          setAddingProject(false)
          selectProject(project.id)
        }} />}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
