import { useStore } from '@nanostores/react'
import { $busy, $conversation, $runtime, api, perform, refreshConversation } from './state'
import { dropImages, ImagePicker, ImageStrip, pasteImages } from './image-input'
import type { ImageDraft, ImageSource } from './image-input'
import { PermissionPicker } from './permission-picker'

interface TaskComposerProps {
  draft: string
  pending: boolean
  paused: boolean
  updateDraft: (value: string) => void
  send: () => void
  openModelSettings: () => void
  images: ImageDraft[]
  loadImage: ImageSource
  imagesDisabled: boolean
  addImages: (files: File[]) => void
  removeImage: (index: number) => void
}
export function TaskComposer({
  draft, pending, paused, updateDraft, send, openModelSettings,
  images, loadImage, imagesDisabled, addImages, removeImage
}: TaskComposerProps) {
  const conversation = useStore($conversation)
  const busy = useStore($busy)
  const runtime = useStore($runtime)!
  const modelLabel = runtime.settings.diagnostic
    ? '离线诊断'
    : runtime.settings.configFile ? '模型配置' : 'qwen3.5:9b'
  const sessionId = conversation?.id ?? ''
  const status = conversation?.status
  const latest = status?.latest_admission
  const running = status?.active_admissions.some(a => ['queued', 'started', 'waiting_approval'].includes(a.status))
  const childRunning = conversation?.delegations.some(row => row !== null && typeof row === 'object'
    && 'status' in row && !['completed', 'failed', 'cancelled', 'stopped'].includes(String(row.status)))
  return (
    <footer className="composer-wrap">
      <div className="composer"
        onDragOver={event => { if (event.dataTransfer.types.includes('Files')) event.preventDefault() }}
        onDrop={event => dropImages(event, addImages, imagesDisabled || pending || busy)}>
        <textarea
          aria-label="任务输入"
          placeholder={sessionId ? '描述你的任务，Shift + Enter 换行…' : '先新建会话，选择本次任务权限…'}
          value={draft}
          disabled={Boolean(pending)}
          onChange={e => updateDraft(e.target.value)}
          onPaste={event => { if (!imagesDisabled) pasteImages(event, addImages) }}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              if (!busy && !paused) send()
            }
          }}
        />
        <ImageStrip images={images} load={loadImage} remove={removeImage} disabled={pending || busy} />
        <div className="composer-bottom">
          <div className="actions composer-tools">
            <ImagePicker iconOnly add={addImages} disabled={imagesDisabled || pending || busy} />
            {status && <PermissionPicker value={status.session.permission_mode}
              disabled={busy || pending || Boolean(running) || paused || Boolean(childRunning)}
              onChange={mode => void perform(async () => {
                await api(`/sessions/${sessionId}/permissions`, 'POST', {
                  permission_mode: mode,
                  expected_revision: status.session.permission_revision ?? 0,
                  risk_acknowledged: mode === 'full'
                })
                await refreshConversation()
              })} />}
          </div>
          <div className="actions composer-submit">
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
              type="button"
              className="composer-model"
              title={`运行设置 · ${runtime.settings.profile}${sessionId ? status?.vision === false ? ' · 仅文字' : ' · 图文' : ''}`}
              aria-label={`模型设置：${modelLabel}`}
              onClick={openModelSettings}
            >
              {modelLabel}
            </button>
            <button
              className="primary send"
              disabled={busy || paused || (Boolean(sessionId) && !draft.trim() && !images.length && !pending)}
              onClick={send}
            >
              {pending ? '核对后重试相同请求' : sessionId ? '发送 ↑' : '新建会话 →'}
            </button>
          </div>
        </div>
      </div>
      <p className="composer-hint">
        {paused
          ? '当前会话已暂停。请使用上方的恢复入口继续提问。'
          : pending
          ? '上次提交未确认。草稿已保留；手动重试沿用同一 submission_id。'
          : childRunning
            ? '后台子任务仍在执行，完成后可切换批准模式。'
            : '内置能力默认启用 · 除完全访问外三类能力逐次确认 · 空闲时可切换批准模式'}
      </p>
    </footer>
  )
}
