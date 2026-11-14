import { useStore } from '@nanostores/react'
import { useState } from 'react'
import { Json } from './components'
import { $busy, $conversation, $runtime, api, perform, refreshConversation } from './state'

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
          <button className={tab === key ? 'selected' : ''} onClick={() => setTab(key)} key={key}>
            {label}
          </button>
        ))}
      </div>
      {status?.approvals.map(approval => (
        <section className="approval" key={approval.id}>
          <span className="eyebrow">WAITING FOR YOU</span>
          <h3>操作需要你的批准</h3>
          <Json value={approval.proposal} />
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
                  {event.type}
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
