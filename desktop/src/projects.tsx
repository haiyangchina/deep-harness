import { useEffect, useRef, useState } from 'react'
import { requestJson } from './workspace-api'

export type Workspace = { id: string; label?: string; mode: string }
type Folders = {
  path: string; parent: string | null; drives: string[]; truncated: boolean
  directories: { name: string; path: string }[]
}

export function AddProject({ sandbox, onClose, onAdd }: {
  sandbox: boolean; onClose: () => void; onAdd: (project: Workspace) => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const request = useRef(0)
  const [folders, setFolders] = useState<Folders>()
  const [path, setPath] = useState('')
  const [label, setLabel] = useState('')
  const [mode, setMode] = useState('read_only')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function browse(target = '') {
    const sequence = ++request.current
    setLoading(true)
    setError('')
    try {
      const next = await requestJson<Folders>({
        method: 'GET', path: `/api/v1/project-folders?path=${encodeURIComponent(target)}`
      })
      if (sequence !== request.current) return
      setFolders(next)
      setPath(next.path)
    } catch (error) {
      if (sequence === request.current) setError(String((error as Error).message))
    } finally {
      if (sequence === request.current) setLoading(false)
    }
  }

  useEffect(() => {
    dialog.current?.showModal()
    void browse()
    return () => { request.current++ }
  }, [])

  async function save() {
    setSaving(true)
    setError('')
    try {
      onAdd(await requestJson<Workspace>({
        method: 'POST', path: '/api/v1/workspaces', body: { path: path.trim(), label, mode }
      }))
    } catch (error) {
      setError(String((error as Error).message))
    } finally {
      setSaving(false)
    }
  }

  return <dialog ref={dialog} className="add-project" onCancel={event => {
    event.preventDefault()
    if (!saving) onClose()
  }}>
    <form onSubmit={event => { event.preventDefault(); void save() }}>
      <h2>新增项目</h2>
      <label>本机项目目录</label>
      <div className="project-directory">
        <input aria-label="本机项目目录" value={path} disabled={saving || loading}
          onChange={event => setPath(event.target.value)} required />
        <button type="button" disabled={saving || loading} onClick={() => void browse(path)}>打开目录</button>
      </div>
      {folders && <>
        <div className="project-drives">
          <button type="button" disabled={!folders.parent || loading || saving}
            onClick={() => void browse(folders.parent ?? '')}>↑ 上一级</button>
          {folders.drives.map(drive => <button type="button" key={drive} disabled={saving || loading}
            onClick={() => void browse(drive)}>{drive}</button>)}
        </div>
        <div className="project-folders" aria-label="子目录" aria-busy={loading}>
          {folders.directories.map(folder => <button type="button" key={folder.path} disabled={loading || saving}
            onClick={() => void browse(folder.path)}>▱ {folder.name}</button>)}
          {!folders.directories.length && <p>此处没有子目录。</p>}
        </div>
        {folders.truncated && <small>显示前 300 个目录，也可以直接输入目录路径。</small>}
      </>}
      <label>项目名称（可选）<input value={label} maxLength={120}
        onChange={event => setLabel(event.target.value)} disabled={saving} /></label>
      {sandbox && <label>工作区模式<select value={mode} disabled={saving}
        onChange={event => setMode(event.target.value)}>
        <option value="read_only">只读</option>
        <option value="sandbox">Docker 沙箱</option>
      </select></label>}
      {error && <p role="alert">{error}</p>}
      <div className="actions">
        <button type="button" onClick={onClose} disabled={saving}>取消</button>
        <button className="primary" disabled={saving || loading || !path.trim()}>添加项目</button>
      </div>
    </form>
  </dialog>
}
