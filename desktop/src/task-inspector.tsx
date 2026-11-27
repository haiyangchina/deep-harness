import { useStore } from '@nanostores/react'
import { useState } from 'react'
import { Json } from './components'
import { $busy, $conversation, $runtime, api, perform, refreshConversation, refreshInspector, report } from './state'

function eventLabel(event: { type: string; payload: unknown }): string {
  const payload = event.payload as { tool?: unknown; elapsed_ms?: unknown; phase?: unknown; error_code?: unknown; fallback?: unknown } | null
  const tool = payload && typeof payload.tool === 'string' ? payload.tool : null
  if (event.type === 'workspace.waiting') return tool ? `等待项目锁 · ${tool}` : '等待项目锁'
  if (event.type === 'workspace.preparing') return '正在准备隔离工作区'
  if (event.type === 'workspace.baseline') return '正在记录隔离工作区基线'
  if (event.type === 'workspace.ready') {
    const mode = (event.payload as { mode?: string } | null)?.mode
    return mode === 'worktree' ? 'Git 工作树已就绪' : '隔离副本已就绪'
  }
  if (event.type === 'workspace.retry') return '已重新排队准备工作区'
  if (event.type === 'summary.retry_started') return '已开始重试汇总'
  if (event.type === 'model.started') return payload?.phase === 'finalizing' ? '正在汇总证据' : '正在调用模型'
  if (event.type === 'tool.started') {
    return tool ? `正在执行工具 · ${tool}` : '正在执行工具'
  }
  if (event.type === 'tool.failed') {
    if (payload?.fallback === 'shallow') return '目录枚举超时 · 已返回浅层结果'
    const label = payload?.error_code === 'TimeoutError' ? '工具超时' : '工具未完成'
    return tool ? `${label} · ${tool}` : label
  }
  if (event.type === 'tool.completed') {
    const ms = payload && typeof payload.elapsed_ms === 'number'
      ? ` · ${(payload.elapsed_ms / 1000).toFixed(1)}s`
      : ''
    return tool ? `工具完成 · ${tool}${ms}` : '工具完成'
  }
  return event.type
}

function proposalScope(proposal: unknown): string | null {
  if (proposal && typeof proposal === 'object' && 'scope_key' in proposal) {
    const value = (proposal as { scope_key?: unknown }).scope_key
    return typeof value === 'string' ? value : null
  }
  return null
}

function PatchPreview({ proposal }: { proposal: unknown }) {
  if (!proposal || typeof proposal !== 'object' || !('tool_name' in proposal) ||
      proposal.tool_name !== 'apply_patch' || !('arguments' in proposal)) return null
  const args = proposal.arguments as {
    files?: { path: string; diff: string; expected_sha256: string | null }[]
  }
  if (!Array.isArray(args?.files)) return null
  return (
    <div className="patch-preview">
      {args.files.map(file => (
        <details key={file.path} open>
          <summary>{file.path}</summary>
          <p className="muted small">{file.expected_sha256
            ? '基线 SHA-256 ' + file.expected_sha256 : '新建文件'}</p>
          <pre>{file.diff}</pre>
        </details>
      ))}
    </div>
  )
}

export function TaskInspector() {
  const conversation = useStore($conversation)
  const busy = useStore($busy)
  const runtime = useStore($runtime)!
  const status = conversation?.status
  const sessionId = conversation?.id ?? ''
  const [tab, setTab] = useState('activity')
  return (
    <aside className="inspector">
      <header>
        <span className="eyebrow">TASK CONTEXT</span>
        <h3>任务上下文</h3>
      </header>
      <div className="workspace-path">
        <span>当前项目</span>
        <p>{runtime.settings.workspace}</p>
      </div>
      <div className="tabs">
        {[
          ['activity', '活动'],
          ['usage', '用量'],
          ['delegations', '子任务'],
          ['state', '状态']
        ].map(([key, label]) => (
          <button className={tab === key ? 'selected' : ''} onClick={() => {
            setTab(key)
            void refreshInspector(key).catch(report)
          }} key={key}>
            {label}
          </button>
        ))}
      </div>
      {status?.approvals.map(approval => (
        <section className="approval" key={approval.id}>
          <span className="eyebrow">WAITING FOR YOU</span>
          <h3>操作需要你的批准</h3>
          <PatchPreview proposal={approval.proposal} />
          <Json value={approval.proposal} />
          {proposalScope(approval.proposal) && (
            <p className="muted small">作用域 {proposalScope(approval.proposal)}</p>
          )}
          <p className="muted small">generation {approval.generation}</p>
          <div className="actions">
            {(['deny', 'approve'] as const).map(decision => (
              <button
                key={decision}
                className={decision === 'approve' ? 'primary' : ''}
                disabled={busy}
                onClick={() =>
                  void perform(async () => {
                    await api(`/sessions/${sessionId}/approvals`, 'POST', {
                      interaction_id: approval.id,
                      generation: approval.generation,
                      decision
                    })
                    await refreshConversation()
                  })
                }
              >
                {decision === 'approve' ? '批准这次操作' : '拒绝'}
              </button>
            ))}
          </div>
        </section>
      ))}
      {(() => {
        const blocked = (status?.active_admissions ?? []).filter(a =>
          ['unknown', 'interrupted', 'reconciled'].includes(a.status)
        )
        if (!blocked.length) return null
        return (
          <section className="approval blocked-notice">
            <span className="eyebrow">INPUT NEEDS ATTENTION</span>
            <h3>有输入未正常完成</h3>
            <p className="muted small">
              下面的输入没有可靠结果，已暂停后续执行。撤销审批复用不能解除此状态。
            </p>
            {blocked.map(a => (
              <p className="muted small" key={a.id}>
                · {a.status}
                {a.result ? `：${a.result.slice(0, 120)}` : ''}
              </p>
            ))}
          </section>
        )
      })()}
      {status?.session.permission_mode === 'auto' &&
        status.latest_admission &&
        !(status.active_admissions ?? []).some(a =>
          ['unknown', 'interrupted', 'reconciled'].includes(a.status)
        ) && (
        <button
          className="revoke-reuse"
          disabled={busy}
          onClick={() =>
            void perform(async () => {
              await api(`/sessions/${sessionId}/revoke-reuse`, 'POST', {
                admission_id: status.latest_admission?.id
              })
              await refreshConversation()
            })
          }
        >
          撤销本轮复用 · 后续同类操作重新逐次确认
        </button>
      )}
      {tab === 'activity' && (
        <div className="events">
          {!conversation && <p className="muted small">发送任务后，执行记录会显示在这里。</p>}
          {conversation?.events
            .slice(-100)
            .reverse()
            .map(event => (
              <details key={event.seq}>
                <summary>
                  <span className="event-dot" />
                  {eventLabel(event)}
                  <small>#{event.seq}</small>
                </summary>
                <Json value={event.payload} />
              </details>
            ))}
        </div>
      )}
      {tab === 'usage' && <Json value={conversation?.usage ?? { info: '尚无用量' }} />}
      {tab === 'delegations' && <Json value={conversation?.delegations ?? []} />}
      {tab === 'state' && <Json value={status ?? { info: '尚未创建会话' }} />}
      <div className="inspector-footer">
        <span className="dot" />
        本机 API · 私有连接
      </div>
    </aside>
  )
}
