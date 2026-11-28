import { useStore } from '@nanostores/react'
import { $busy, $conversation, $runtime, api, perform, refreshConversation, report, workspaceApi } from './state'
import { dropFiles, FilePicker, ImageStrip, isSupportedImage, pasteImages } from './image-input'
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
  files: File[]
  setFiles: (files: File[]) => void
}
export function TaskComposer({
  draft, pending, paused, updateDraft, send, openModelSettings,
  images, loadImage, imagesDisabled, addImages, removeImage, files, setFiles
}: TaskComposerProps) {
  const conversation = useStore($conversation)
  const busy = useStore($busy)
  const runtime = useStore($runtime)!
  const sessionId = conversation?.id ?? ''
  const status = conversation?.status
  const modelLabel = status?.model_id ?? runtime.catalog?.default_model ?? (runtime.settings.diagnostic
    ? '离线诊断'
    : runtime.settings.configFile ? '模型配置' : 'qwen3.5:9b')
  const runs = status?.active_admissions ?? []
  const executing = runs.some(run => run.status === 'started')
  const approving = runs.some(run => run.status === 'waiting_approval')
  const resuming = runs.some(run => run.status === 'job_paused')
  const running = executing || approving || resuming || runs.some(run => run.status === 'queued')
  const pauseRequested = Boolean(status?.pause_requested)
  const pausing = executing && pauseRequested
  const controlling = running || pauseRequested
  const actionLabel = pauseRequested ? (pausing ? '正在暂停' : '继续任务')
    : executing || resuming ? '执行中' : approving ? '待审批' : '排队中'
  const buttonHint = pausing ? '等待当前步骤完成'
    : pauseRequested ? '从暂停处继续任务'
      : status?.pause_supported ? '在安全边界暂停任务' : actionLabel
  const togglePause = () => void perform(async () => {
    await workspaceApi(`/sessions/${sessionId}/task-control`, 'POST', {
      action: pauseRequested ? 'resume' : 'pause'
    })
    await refreshConversation()
  })
  const childRunning = conversation?.delegations.some(row => row !== null && typeof row === 'object'
    && 'status' in row && !['completed', 'failed', 'cancelled', 'stopped'].includes(String(row.status)))
  const pickerDisabled = !sessionId || pending || busy
  const addSelected = (selected: File[]) => {
    const pictures = imagesDisabled ? [] : selected.filter(isSupportedImage)
    const attachments = imagesDisabled ? selected : selected.filter(file => !isSupportedImage(file))
    if (attachments.length) {
      const limit = (runtime.catalog ? 64 : 8) * 1024 * 1024
      if (files.length + attachments.length > 8 || attachments.some(file => file.size > limit)) {
        report('每条消息最多选择 8 个附件，单个附件大小超出当前上限。')
        return
      }
      setFiles([...files, ...attachments])
    }
    if (pictures.length) addImages(pictures)
  }
  return (
    <footer className="composer-wrap">
      <div className="composer"
        onDragOver={event => { if (event.dataTransfer.types.includes('Files')) event.preventDefault() }}
        onDrop={event => dropFiles(event, addSelected, pickerDisabled)}>
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
              if (!busy && !paused && !controlling) send()
            }
          }}
        />
        <ImageStrip images={images} load={loadImage} remove={removeImage} disabled={pending || busy} />
        {files.map((file, index) => <button key={`${index}:${file.name}`} type="button"
          disabled={pending || busy} onClick={() => setFiles(files.filter((_, i) => i !== index))}>
          移除 {file.name}
        </button>)}
        <div className="composer-bottom">
          <div className="actions composer-tools">
            <FilePicker add={addSelected} disabled={pickerDisabled} />
            {status && !runtime.catalog && <PermissionPicker value={status.session.permission_mode}
              disabled={busy || pending || controlling || paused || Boolean(childRunning)}
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
            <button
              type="button"
              className="composer-model"
              title={`运行设置 · ${runtime.settings.profile}${sessionId ? status?.vision === false ? ' · 仅文字' : ' · 图文' : ''}`}
              aria-label={`模型设置：${modelLabel}`}
              onClick={openModelSettings}
            >
              {modelLabel}
            </button>
            {controlling ? (
              <button type="button" className={`send run-control${approving && !pauseRequested ? ' awaiting' : ''}`}
                title={buttonHint} aria-label={`${actionLabel}，${buttonHint}`}
                disabled={busy || pausing || !status?.pause_supported} onClick={togglePause}>
                <span className={`run-control-icon${!pauseRequested && !approving || pausing ? ' spinning' : ''}`}
                  aria-hidden="true">
                  {pauseRequested && !pausing ? '▶' : <span className="run-pause-bars" />}
                </span>
                <span>{actionLabel}</span>
              </button>
            ) : (
              <button type="button" className="primary send"
                disabled={busy || paused || (Boolean(sessionId) && !draft.trim() && !images.length && !pending)}
                onClick={send}>
                {pending ? '核对后重试相同请求' : sessionId ? '发送 ↑' : '新建会话 →'}
              </button>
            )}
          </div>
        </div>
      </div>
      <p className="composer-hint">
        {pausing
          ? '当前步骤结束后会暂停。'
          : pauseRequested
          ? '任务已暂停，可从发送区继续。'
          : approving
          ? '工具操作需要审批。'
          : paused
          ? '当前会话已暂停。请使用上方的恢复入口继续提问。'
          : pending
          ? '上次提交未确认。草稿已保留；手动重试沿用同一 submission_id。'
          : childRunning
            ? '后台子任务仍在执行，完成后可切换批准模式。'
            : runtime.catalog ? '操作权限由当前服务器工作区决定。' : '空闲时可切换批准模式。'}
      </p>
    </footer>
  )
}
