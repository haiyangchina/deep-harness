import { useStore } from '@nanostores/react'
import { $busy, $conversation, $runtime, api, perform, refreshConversation } from './state'
import { PermissionPicker } from './permission-picker'

interface TaskComposerProps {
  draft: string
  pending: boolean
  updateDraft: (value: string) => void
  send: () => void
}
export function TaskComposer({ draft, pending, updateDraft, send }: TaskComposerProps) {
  const conversation = useStore($conversation)
  const busy = useStore($busy)
  const runtime = useStore($runtime)!
  const sessionId = conversation?.id ?? ''
  const status = conversation?.status
  const latest = status?.latest_admission
  const running = status?.active_admissions.some(a => ['queued', 'started', 'waiting_approval'].includes(a.status))
  // Also block after a deterministic interruption: the worker stops on a non-completed
  // admission, so the user must start a fresh session (see the Chat notice).
  const unknown = status?.active_admissions.some(a => ['unknown', 'interrupted', 'reconciled'].includes(a.status))
  const childRunning = conversation?.delegations.some(row => row !== null && typeof row === 'object'
    && 'status' in row && !['completed', 'failed', 'cancelled', 'stopped'].includes(String(row.status)))
  return (
    <footer className="composer-wrap">
      <div className="composer">
        <textarea
          aria-label="任务输入"
          placeholder={sessionId ? '描述你的任务，Shift + Enter 换行…' : '先新建会话，选择本次任务权限…'}
          value={draft}
          disabled={Boolean(pending)}
          onChange={e => updateDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              send()
            }
          }}
        />
        <div className="composer-bottom">
          <span className="model-label">
            ◉{' '}
            {runtime.settings.diagnostic
              ? '离线诊断（非模型）'
              : runtime.settings.configFile
                ? '模型配置'
                : 'qwen3.5:9b'}
            <span> · {runtime.settings.profile}</span>
          </span>
          <div className="actions">
            {status && <PermissionPicker value={status.session.permission_mode ?? 'legacy'}
              disabled={busy || pending || Boolean(running) || Boolean(unknown) || Boolean(childRunning)}
              onChange={mode => void perform(async () => {
                await api(`/sessions/${sessionId}/permissions`, 'POST', {
                  permission_mode: mode,
                  expected_revision: status.session.permission_revision ?? 0,
                  risk_acknowledged: mode === 'full'
                })
                await refreshConversation()
              })} />}
            {running && latest && (
              <button
                onClick={() =>
                  void perform(async () => {
                    await api(`/sessions/${sessionId}/admissions/${latest.id}/cancel`, 'POST')
                    await refreshConversation()
                  })
                }
                disabled={busy}
              >
                停止
              </button>
            )}
            <button
              className="primary send"
              disabled={busy || Boolean(unknown) || (Boolean(sessionId) && !draft.trim() && !pending)}
              onClick={send}
            >
              {pending ? '核对后重试相同请求' : sessionId ? '发送 ↑' : '新建会话 →'}
            </button>
          </div>
        </div>
      </div>
      <p className="composer-hint">
        {pending
          ? '上次提交未确认。草稿已保留；手动重试沿用同一 submission_id。'
          : childRunning
            ? '后台子任务仍在执行，完成后可切换批准模式。'
            : '内置能力默认启用 · 除完全访问外三类能力逐次确认 · 空闲时可切换批准模式'}
      </p>
    </footer>
  )
}
