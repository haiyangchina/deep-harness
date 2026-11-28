import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { Session } from './protocol'
import {
  $busy, $conversation, $error, $sessionCursor, $sessions, closeSession, loadMoreSessions,
  loadSessions, perform, report, storageKey, workspaceApi
} from './state'
import { HttpError } from './workspace-api'

type Deletion = {
  session_id: string; status: 'deleting' | 'deleted' | 'cleanup_required'; reason?: string
}
type Menu = { session: Session; button: HTMLButtonElement; x: number; y: number }
type Edit = { session: Session; action: 'rename' | 'delete' }

export const sessionTitle = (session: Session) => session.title || session.id.slice(0, 12)

function SessionDialog({ edit, submit, dismiss, busy, review }: {
  edit: Edit; submit: (title: string) => void; dismiss: () => void; busy: boolean
  review?: () => void
}) {
  const [title, setTitle] = useState(sessionTitle(edit.session))
  const error = useStore($error)
  const dialog = useRef<HTMLDialogElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  const deleting = edit.action === 'delete'
  const valid = Array.from(title.trim()).length > 0 && Array.from(title.trim()).length <= 80
  useEffect(() => {
    dialog.current?.showModal()
    if (deleting) cancel.current?.focus()
    else input.current?.select()
  }, [deleting])
  return <dialog ref={dialog} className="session-dialog" aria-labelledby="session-dialog-title"
    onCancel={event => { event.preventDefault(); if (!busy) dismiss() }}>
    <form onSubmit={event => { event.preventDefault(); if (deleting || valid) submit(title.trim()) }}>
      <h2 id="session-dialog-title">{deleting ? '彻底删除会话？' : '重命名会话'}</h2>
      {deleting ? <>
        <p><strong>{sessionTitle(edit.session)}</strong></p>
        <p>会取消排队及待审批的请求，永久清除对话、运行记录和此会话独占的附件、产物及任务目录。</p>
        <p>已应用的项目变更、外部系统结果、导出文件和既有备份仍由各自的位置管理。</p>
      </> : <label className="field">会话名称
        <input ref={input} value={title} maxLength={160} disabled={busy}
          onChange={event => setTitle(event.target.value)} />
      </label>}
      {error && <p role="alert" className="notice">{error}</p>}
      <div className="actions">
        <button ref={cancel} type="button" onClick={dismiss} disabled={busy}>取消</button>
        {review && <button type="button" onClick={review} disabled={busy}>打开会话核对</button>}
        <button type="submit" className={deleting ? 'session-danger' : 'primary'}
          disabled={busy || (!deleting && !valid)}>{deleting ? '彻底删除' : '保存'}</button>
      </div>
    </form>
  </dialog>
}

export function SessionList({ ready, open }: { ready: boolean; open: (id: string) => void }) {
  const sessions = useStore($sessions)
  const selected = useStore($conversation)
  const cursor = useStore($sessionCursor)
  const busy = useStore($busy)
  const [menu, setMenu] = useState<Menu | null>(null)
  const [edit, setEdit] = useState<Edit | null>(null)
  const [needsReview, setNeedsReview] = useState(false)
  const [deletions, setDeletions] = useState<Deletion[]>([])
  const [deletionRefresh, refreshDeletions] = useState(0)
  const trigger = useRef<HTMLButtonElement | null>(null)
  const popup = useRef<HTMLDivElement>(null)

  function dismissMenu(restore = true) {
    setMenu(null)
    if (restore && trigger.current?.isConnected) trigger.current.focus()
  }

  function forgetView(id: string) {
    localStorage.removeItem(storageKey('draft', id))
    localStorage.removeItem(storageKey('pending', id))
    if ($conversation.get()?.id === id) closeSession()
  }

  useEffect(() => {
    if (!ready) return
    let alive = true
    let timer: ReturnType<typeof setTimeout>
    let previous = new Set<string>()
    async function poll() {
      let delay = 6000
      try {
        const result = await workspaceApi<{ deletions: Deletion[] }>('/session-deletions')
        if (!alive) return
        const next = new Set(result.deletions.map(item => item.session_id))
        const changed = previous.size !== next.size || [...previous].some(id => !next.has(id))
        previous = next
        for (const id of next) forgetView(id)
        setDeletions(result.deletions)
        if (changed) await loadSessions()
        if (result.deletions.some(item => item.status === 'deleting')) delay = 900
      } catch (error) {
        if (alive && previous.size) report(error)
      } finally {
        if (alive) timer = setTimeout(() => void poll(), delay)
      }
    }
    void poll()
    return () => { alive = false; clearTimeout(timer) }
  }, [ready, deletionRefresh])

  useEffect(() => {
    if (!menu) return
    popup.current?.querySelector('button')?.focus()
    function outside(event: PointerEvent) {
      if (event.target instanceof Node && !popup.current?.contains(event.target) &&
          !menu?.button.contains(event.target)) dismissMenu(false)
    }
    const close = () => dismissMenu()
    document.addEventListener('pointerdown', outside)
    window.addEventListener('resize', close)
    window.addEventListener('scroll', close, true)
    return () => {
      document.removeEventListener('pointerdown', outside)
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', close, true)
    }
  }, [menu])

  async function update(session: Session, changes: { title?: string; pinned?: boolean }) {
    try {
      const result = await workspaceApi<Session>(`/sessions/${session.id}`, 'PATCH', {
        expected_revision: session.metadata_revision ?? 0, ...changes
      })
      if (changes.pinned !== undefined) await loadSessions()
      else {
        $sessions.set($sessions.get().map(row => row.id === result.id ? result : row))
        const view = $conversation.get()
        if (view?.id === result.id && view.status) {
          $conversation.set({ ...view, status: { ...view.status, session: result } })
        }
      }
      setEdit(null)
      dismissMenu()
    } catch (error) {
      if (error instanceof HttpError && error.code === 'session_metadata_conflict') {
        setEdit(null)
        await loadSessions()
        throw new Error('会话已被其他客户端修改，列表已刷新，请重新操作。')
      }
      throw error
    }
  }

  async function remove(id: string) {
    const result = await workspaceApi<Deletion>(`/sessions/${id}`, 'DELETE').catch(error => {
      setNeedsReview(error instanceof HttpError &&
        error.code === 'session_deletion_reconciliation_required')
      throw error
    })
    forgetView(id)
    setEdit(null)
    $sessions.set($sessions.get().filter(session => session.id !== id))
    setDeletions(rows => [
      ...rows.filter(row => row.session_id !== id),
      ...(result.status === 'deleted' ? [] : [result])
    ])
    refreshDeletions(value => value + 1)
    await loadSessions()
  }

  return <>
    <div className="session-list">
      {ready && !sessions.length && <p className="muted small">暂无会话。</p>}
      {sessions.map(session => <div key={session.id}
        className={`session-row${selected?.id === session.id ? ' active-session' : ''}`}>
        <button type="button" className="session-open" onClick={() => open(session.id)}>
          <span aria-hidden="true">{session.pinned ? '⌖' : '◇'}</span>
          <div><span className="session-title" title={sessionTitle(session)}>{sessionTitle(session)}</span>
            <small>{session.harness}{session.pinned ? ' · 已置顶' : ''}</small></div>
        </button>
        <button type="button" className="session-more" aria-label={`${sessionTitle(session)} 的会话操作`}
          aria-haspopup="menu" aria-expanded={menu?.session.id === session.id} disabled={busy}
          onClick={event => {
            if (menu?.session.id === session.id) { dismissMenu(); return }
            const button = event.currentTarget
            const rect = button.getBoundingClientRect()
            trigger.current = button
            setMenu({ session, button, x: Math.max(8, Math.min(rect.right - 156, window.innerWidth - 164)),
              y: rect.bottom + 122 > window.innerHeight ? Math.max(8, rect.top - 122) : rect.bottom + 4 })
          }}>⋯</button>
      </div>)}
      {deletions.map(item => <div className="session-cleanup" key={item.session_id} role="status">
        <small>{item.session_id.slice(0, 12)} · {item.status === 'deleting' ? '正在清理'
          : item.reason === 'workspace_unknown' ? '待核对任务工作区'
          : item.reason === 'cleanup_path' ? '待核对受管资源路径' : '清理待处理'}</small>
        {item.status === 'cleanup_required' && <button disabled={busy}
          onClick={() => void perform(() => remove(item.session_id))}>继续清理</button>}
      </div>)}
      {cursor && <button onClick={() => void perform(loadMoreSessions)}>加载更多会话</button>}
      {ready && <button className="session-refresh" disabled={busy}
        onClick={() => void perform(loadSessions)}>↻ 刷新会话</button>}
    </div>
    {menu && createPortal(<div ref={popup} role="menu" aria-label="会话操作" className="session-menu"
      style={{ left: menu.x, top: menu.y }} onKeyDown={event => {
        if (event.key === 'Escape') { event.stopPropagation(); dismissMenu(); return }
        const items = Array.from(popup.current?.querySelectorAll('button') ?? [])
        const current = items.indexOf(document.activeElement as HTMLButtonElement)
        if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
          event.preventDefault()
          const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
            : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
          items[index]?.focus()
        } else if (event.key === 'Tab') dismissMenu(false)
      }}>
      <button role="menuitem" onClick={() => {
        dismissMenu()
        void perform(() => update(menu.session, { pinned: !menu.session.pinned }))
      }}>⌖ {menu.session.pinned ? '取消置顶' : '置顶'}</button>
      <button role="menuitem" onClick={() => {
        $error.set(''); dismissMenu(false); setEdit({ session: menu.session, action: 'rename' })
      }}>✎ 重命名</button>
      <button role="menuitem" className="session-danger" onClick={() => {
        $error.set(''); setNeedsReview(false); dismissMenu(false)
        setEdit({ session: menu.session, action: 'delete' })
      }}>彻底删除</button>
    </div>, document.body)}
    {edit && <SessionDialog edit={edit} busy={busy} review={
      edit.action === 'delete' && needsReview ? () => {
        setEdit(null)
        $error.set('')
        open(edit.session.id)
      } : undefined
    } dismiss={() => {
      setEdit(null)
      dismissMenu()
    }} submit={title => void perform(() => edit.action === 'delete'
      ? remove(edit.session.id) : update(edit.session, { title }))} />}
  </>
}
