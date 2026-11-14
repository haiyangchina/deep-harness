import { createRoot } from 'react-dom/client'
import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'
import { CreateSession, Knowledge, Setup } from './components'
import {
  $busy,
  $conversation,
  $error,
  $runtime,
  $sessions,
  api,
  initialize,
  loadSessions,
  openSession,
  perform,
  refreshConversation,
  resetView,
  report,
  storageKey
} from './state'
import type { Admission } from './protocol'
import { TaskComposer } from './task-composer'
import { TaskInspector } from './task-inspector'
import './styles.css'

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
type Page = 'chat' | 'skills' | 'memories' | 'mcp/tools' | 'settings'

function Chat({ newSession }: { newSession: () => void }) {
  const conversation = useStore($conversation)
  const busy = useStore($busy)
  const sessionId = conversation?.id ?? ''
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState<{ submission_id: string; text: string } | null>(null)
  const tail = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  useEffect(() => {
    follow.current = true
    setDraft(localStorage.getItem(storageKey('draft', sessionId)) || '')
    const raw = localStorage.getItem(storageKey('pending', sessionId))
    setPending(raw ? JSON.parse(raw) : null)
  }, [sessionId])
  const updateDraft = (value: string) => {
    setDraft(value)
    localStorage.setItem(storageKey('draft', sessionId), value)
  }
  const status = conversation?.status
  const latest = status?.latest_admission
  const running = status?.active_admissions.some(a => ['queued', 'started', 'waiting_approval'].includes(a.status))
  // Only unknown/reconciled mean a write may have landed without a receipt. An
  // interrupted run is a deterministic local stop with no uncertain side effects.
  const uncertain = status?.active_admissions.some(a => ['unknown', 'reconciled'].includes(a.status))
  const interrupted = status?.active_admissions.some(a => a.status === 'interrupted')
  const send = () =>
    void perform(async () => {
      if (!sessionId) {
        newSession()
        return
      }
      const submission = pending || { submission_id: crypto.randomUUID(), text: draft.trim() }
      if (!submission.text) return
      localStorage.setItem(storageKey('pending', sessionId), JSON.stringify(submission))
      setPending(submission)
      await api<Admission>(`/sessions/${sessionId}/inputs`, 'POST', submission)
      localStorage.removeItem(storageKey('pending', sessionId))
      localStorage.removeItem(storageKey('draft', sessionId))
      if ($conversation.get()?.id === sessionId) {
        setPending(null)
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
            {status?.session.engine ?? 'LOCAL'} · {status?.session.harness ?? 'Ollama'}
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
          {conversation?.messages
            .filter(message => message.content !== '' && message.content !== null)
            .map((message, index) => (
              <article className={`message ${message.role}`} key={message.id || index}>
                <div className="message-role">
                  {message.role === 'user' ? '你' : message.role === 'assistant' ? 'deep-harness' : message.role}
                </div>
                {message.role === 'tool' ? (
                  <details>
                    <summary>工具执行结果 · 展开查看</summary>
                    <div className="message-content">
                      {typeof message.content === 'string' ? message.content : JSON.stringify(message.content, null, 2)}
                    </div>
                  </details>
                ) : (
                  <div className="message-content">
                    {typeof message.content === 'string' ? message.content : JSON.stringify(message.content, null, 2)}
                  </div>
                )}
              </article>
            ))}
          {latest && !conversation?.messages.some(message => message.id === `${latest.id}:user`) && (
            <article className="message user">
              <div className="message-role">当前任务 · {latest.status}</div>
              <div className="message-content">{latest.text}</div>
            </article>
          )}
          {running && deltas && (
            <article className="message assistant">
              <div className="message-role">deep-harness · 生成中</div>
              <div className="message-content">{deltas}</div>
            </article>
          )}
          {latest?.result && latest.status !== 'completed' && <p className="notice">{latest.result}</p>}
          {latest?.status === 'completed' && !latest.result && !running && (
            <p className="notice">
              本次执行已完成，但模型没有返回文本结果。常见原因是模型开启了 thinking / reasoning
              模式，回答被写入思考字段而未被读取（qwen3.5 等思考模型默认如此）。请确认配置中
              reasoning 为 false，或换用非思考模型，然后新建会话重试。
            </p>
          )}
          {status?.runtime.compatible === false && (
            <p className="notice">当前模型或工具配置与会话快照不同。请恢复原配置，或审阅后新建分支。</p>
          )}
          {uncertain && (
            <div className="notice">
              <strong>任务结果不确定，已停止自动推进。</strong>
              <p>先核对实际文件与右侧状态台账。分支只复制完整已完成轮次，不会重做未知操作。</p>
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
          {interrupted && (
            <div className="notice">
              <strong>任务已安全停止，未产生未知副作用。</strong>
              <p>上方为具体停止原因。常见原因是单次读取内容过大或超出上下文预算；请缩小读取范围，或新建会话后重试。</p>
              <button disabled={busy} onClick={newSession}>
                新建会话
              </button>
            </div>
          )}
          <div ref={tail} />
        </div>
        <TaskComposer draft={draft} pending={Boolean(pending)} updateDraft={updateDraft} send={send} />
      </main>
      <TaskInspector />
    </div>
  )
}

function App() {
  const runtime = useStore($runtime)
  const sessions = useStore($sessions)
  const conversation = useStore($conversation)
  const error = useStore($error)
  const [page, setPage] = useState<Page>('chat')
  const [creating, setCreating] = useState(false)
  useEffect(() => {
    void initialize().catch(report)
    const timer = setInterval(() => {
      void window.deepHarness
        .info()
        .then(info => {
          const previous = $runtime.get()
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
        <button className="new-task" disabled={!ready} onClick={() => setCreating(true)}>
          ＋ 新建会话 <kbd>LOCAL</kbd>
        </button>
        <nav>
          {(
            [
              ['chat', '◈', '工作台'],
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
                  {session.engine} · {session.harness}
                </small>
              </div>
            </button>
          ))}
        </div>
        <div className="sidebar-bottom">
          <button onClick={() => setPage('settings')}>⚙ 运行设置</button>
          <div className="connection">
            <span className={ready ? 'dot' : 'dot offline'} />
            {ready ? '本机 Agent 已连接' : '等待配置'}
            <small>v0.1 · 独立开发版</small>
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
          <Setup onClose={ready ? () => setPage('chat') : undefined} />
        ) : page === 'chat' ? (
          <Chat newSession={() => setCreating(true)} />
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
