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
  $sessions,
  $sessionCursor,
  api,
  initialize,
  loadSessions,
  loadMoreSessions,
  openSession,
  perform,
  refreshConversation,
  loadOlderMessages,
  resetView,
  report,
  storageKey
} from './state'
import type { Admission } from './protocol'
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

function TaskPause({ sessionId }: { sessionId: string }) {
  const [paused, setPaused] = useState<boolean | null>(null)
  const busy = useStore($busy)
  const route = `/sessions/${sessionId}/task-control`
  useEffect(() => {
    let active = true
    void api<{ pause_requested: boolean }>(route).then(value => {
      if (active) setPaused(value.pause_requested)
    }).catch(() => {})
    return () => { active = false }
  }, [route])
  if (paused === null) return null
  return <button disabled={busy} onClick={() => void perform(async () => {
    const value = await api<{ pause_requested: boolean }>(route, 'POST', {
      action: paused ? 'resume' : 'pause'
    })
    setPaused(value.pause_requested)
    await refreshConversation()
  })}>{paused ? '继续任务' : '在安全边界暂停'}</button>
}

function DevelopmentProcesses({ sessionId }: { sessionId: string }) {
  const busy = useStore($busy)
  const [open, setOpen] = useState(false)
  const [command, setCommand] = useState('')
  const [pending, setPending] = useState<{ command: string; request_id: string } | null>(null)
  const [rows, setRows] = useState<DevProcess[]>([])
  const route = `/sessions/${sessionId}/processes`
  const refresh = async () => setRows(await api<DevProcess[]>(route))
  useEffect(() => {
    if (!open) return
    let active = true
    const update = () => void api<DevProcess[]>(route).then(value => {
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
        const request = pending ?? { command: command.trim(), request_id: crypto.randomUUID() }
        setPending(request)
        await api(route, 'POST', request)
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
        await api(`${route}/${row.id}/stop`, 'POST', {})
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
  void window.deepHarness.openLink(href).catch(report)
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
  const busy = useStore($busy)
  const sessionId = conversation?.id ?? ''
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState<{ submission_id: string; text: string; image_ids?: string[] } | null>(null)
  const [images, setImages] = useState<ImageDraft[]>([])
  const loadImage = useCallback(async (id: string, full = false) => {
    const result = await api<{ data: string; media_type: string }>(
      `/sessions/${sessionId}/images/${id}?encoding=base64${full ? '' : '&thumbnail=1'}`
    )
    return `data:${result.media_type};base64,${result.data}`
  }, [sessionId])
  const tail = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  useEffect(() => {
    follow.current = true
    setDraft(localStorage.getItem(storageKey('draft', sessionId)) || '')
    const raw = localStorage.getItem(storageKey('pending', sessionId))
    const saved = raw ? JSON.parse(raw) : null
    setPending(saved)
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
  const workspaceAction = (admissionId: string, action: 'cancel' | 'retry-workspace' | 'retry-summary') =>
    void perform(async () => {
      await api(`/sessions/${sessionId}/admissions/${admissionId}/${action}`, 'POST')
      await refreshConversation()
    })
  const send = () =>
    void perform(async () => {
      if (paused) return
      if (!sessionId) {
        newSession()
        return
      }
      if (!pending && !draft.trim() && !images.length) return
      const imageIds = pending ? pending.image_ids : await stageImages(images, async file => {
        const uploaded = await window.deepHarness.request<{ id: string }>({
          method: 'POST', path: `/sessions/${sessionId}/images`,
          upload: { name: file.name, data: await file.arrayBuffer() }
        })
        return uploaded.id
      }, staged => { if ($conversation.get()?.id === sessionId) setImages(staged) })
      const submission = pending || {
        submission_id: crypto.randomUUID(), text: draft.trim(), image_ids: imageIds
      }
      localStorage.setItem(storageKey('pending', sessionId), JSON.stringify(submission))
      setPending(submission)
      const existing = pending ? await api<Admission | null>(
        `/sessions/${sessionId}/submission?submission_id=${encodeURIComponent(submission.submission_id)}`
      ) : null
      if (!existing) {
        try {
          await api<Admission>(`/sessions/${sessionId}/inputs`, 'POST', submission)
        } catch (error) {
          if (error instanceof Error && /本机 API 返回 HTTP 4\d\d/.test(error.message)) {
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
        setDraft('')
      }
      await refreshConversation()
    })
  const accepted =
    conversation?.events.findLastIndex(e => e.type === 'admission.accepted' && e.payload.id === latest?.id) ?? -1
  const fragments = conversation?.events.slice(Math.max(0, accepted)).filter(e => e.type === 'model.delta') ?? []
  const messageId = fragments.at(-1)?.payload.message_id
  const deltas = fragments
    .filter(e => e.payload.message_id === messageId)
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
            <p>{sessionId ? `会话 ${sessionId.slice(0, 10)}` : '开始新的工作'}</p>
          </div>
          <span className="chip">
            {status?.session.harness ?? 'Ollama'}
            {sessionId && status?.session.harness === 'coding' && (
              <TaskPause key={sessionId} sessionId={sessionId} />
            )}
          </span>
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
                    <TaskDelivery key={latest?.id} sessionId={sessionId} />
                    <DevelopmentProcesses key={sessionId} sessionId={sessionId} />
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
          {running && deltas && (
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
          {uncertain && (
            <div className="notice">
              <strong>任务结果不确定，已停止自动推进。</strong>
              {queuedCount > 0 && <p>此会话有 {queuedCount} 条消息尚未执行。</p>}
              <p>核对文件与右侧状态台账后，创建分支并重新发送问题。分支只复制完整已完成轮次。</p>
              <button
                disabled={busy}
                onClick={() =>
                  void perform(async () => {
                    const fork = await api<{ id: string }>(`/sessions/${sessionId}/forks`, 'POST', {
                      request_id: crypto.randomUUID()
                    })
                    await loadSessions()
                    await openSession(fork.id)
                  })
                }
              >
                我已核对，创建新分支
              </button>
            </div>
          )}
          {latest && (workspaceWaiting || workspacePreparing) && (
            <div className="notice">
              <strong>{workspaceWaiting ? '正在等待项目锁' :
                workspacePhase === 'workspace.baseline' ? '正在记录隔离工作区基线' : '正在准备隔离工作区'}</strong>
              <button disabled={busy} onClick={() => workspaceAction(latest.id, 'cancel')}>取消</button>
            </div>
          )}
          {interrupted && (
            <div className="notice">
              <strong>{retrySummary ? '已有项目证据，汇总未完成。' : '任务已停止，请查看上方原因。'}</strong>
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
          />
        )}
      </main>
      <TaskInspector />
    </div>
  )
}

function App() {
  const runtime = useStore($runtime)
  const busy = useStore($busy)
  const sessions = useStore($sessions)
  const sessionCursor = useStore($sessionCursor)
  const conversation = useStore($conversation)
  const error = useStore($error)
  const [page, setPage] = useState<Page>('chat')
  const [creating, setCreating] = useState(false)
  const selectProject = (id: string | null) => void perform(async () => {
    const next = await window.deepHarness.openProject(id)
    const previous = $runtime.get()
    if (previous && next.projectId === previous.projectId && next.ready === previous.ready) return
    resetView()
    $runtime.set(next)
    setCreating(false)
    setPage(next.ready ? 'chat' : 'settings')
    if (next.ready) await loadSessions()
  })
  useEffect(() => {
    void initialize().catch(report)
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
        <h2>正在打开本机工作台…</h2>
        {error && <p className="notice">{error}</p>}
      </div>
    )
  const ready = runtime.ready
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span>D✦</span>
          <div>
            deep-<b>harness</b>
            <small>AGENT WORKSPACE</small>
          </div>
        </div>
        <div className="section-label">
          项目 <button disabled={busy} onClick={() => selectProject(null)}>＋ 新增项目</button>
        </div>
        <nav className="project-list" aria-label="项目">
          {runtime.projects?.map(project => (
            <button key={project.id} title={project.workspace} disabled={busy}
              aria-current={runtime.projectId === project.id ? 'true' : undefined}
              className={runtime.projectId === project.id ? 'nav-active' : ''}
              onClick={() => selectProject(project.id)}>
              <span aria-hidden="true">▱</span>{project.name}
            </button>
          ))}
        </nav>
        <button className="new-task" disabled={!ready} onClick={() => setCreating(true)}>
          ＋ 新建会话 <kbd>LOCAL</kbd>
        </button>
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
          ).map(([key, icon, label]) => (
            <button
              key={key}
              disabled={!ready}
              className={page === key ? 'nav-active' : ''}
              onClick={() => setPage(key)}
            >
              <span>{icon}</span>
              {label}
            </button>
          ))}
        </nav>
        <div className="section-label">
          会话记录{' '}
          <button disabled={!ready} aria-label="刷新会话" onClick={() => void perform(loadSessions)}>
            ↻
          </button>
        </div>
        <div className="session-list">
          {!sessions.length && <p className="muted small">你的工作从这里开始。</p>}
          {sessions.map(session => (
            <button
              key={session.id}
              className={conversation?.id === session.id ? 'active-session' : ''}
              onClick={() => {
                setPage('chat')
                void openSession(session.id).catch(report)
              }}
            >
              <span>◇</span>
              <div>
                {session.id.slice(0, 12)}
                <small>
                  {session.harness}
                </small>
              </div>
            </button>
          ))}
          {sessionCursor && <button onClick={() => void perform(loadMoreSessions)}>加载更多会话</button>}
        </div>
        <div className="sidebar-bottom">
          <button onClick={() => setPage('settings')}>⚙ 运行设置</button>
          <div className="connection">
            <span className={ready ? 'dot' : 'dot offline'} />
            {ready ? '本机 Agent 已连接' : '等待配置'}
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
        {!ready || page === 'settings' ? (
          <Setup key={runtime.projectId || 'setup'} onClose={ready ? () => setPage('chat') : undefined} />
        ) : page === 'chat' ? (
          <Chat newSession={() => setCreating(true)} openModelSettings={() => setPage('settings')} />
        ) : page === 'jobs' ? (
          <LongTasks openStage={id => {
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
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
