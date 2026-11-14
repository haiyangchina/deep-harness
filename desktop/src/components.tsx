import { useStore } from '@nanostores/react'
import { useState } from 'react'
import type { Settings, Session, PermissionMode } from './protocol'
import { PermissionPicker } from './permission-picker'
import { $busy, $runtime, api, initialize, loadSessions, openSession, perform, resetView, storageKey } from './state'

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>
}

export function Setup({ onClose }: { onClose?: () => void }) {
  const runtime = useStore($runtime)
  const busy = useStore($busy)
  const [form, setForm] = useState<Settings>(runtime!.settings)
  const fields = [
    ['python', 'deep-harness 的 Python 3.12'],
    ['workspace', '项目工作目录'],
    ['stateDir', '独立运行状态目录'],
    ['configFile', '模型 TOML 配置（可选）']
  ] as const
  return (
    <div className="settings page">
      <div className="eyebrow">LOCAL FIRST · NO SIGN-IN</div>
      <h1>连接你的 deep-harness</h1>
      <p className="muted">本机运行，直接使用。默认 Ollama · qwen3.5:9b，无需桌面账号。</p>
      {runtime?.ready && (
        <p className="notice">
          当前配置已绑定运行中的后端。更换工作区请使用新 Profile 或状态目录。
          <br />
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void perform(async () => {
                const info = await window.deepHarness.stop()
                $runtime.set(info)
                if (!info.ready) resetView()
              })
            }
          >
            停止本机后端并修改配置
          </button>
        </p>
      )}
      <form
        onSubmit={event => {
          event.preventDefault()
          void perform(async () => {
            $runtime.set(await window.deepHarness.start(form))
            await initialize()
            onClose?.()
          })
        }}
      >
        {fields.map(([key, label]) => (
          <label className="field" key={key}>
            {label}
            <div className="picker">
              <input
                value={form[key]}
                disabled={runtime?.ready}
                onChange={e => setForm({ ...form, [key]: e.target.value })}
                placeholder={key === 'python' ? '选择 deep-harness Conda 环境中的 python.exe' : '绝对路径'}
              />
              <button
                type="button"
                disabled={runtime?.ready}
                onClick={() =>
                  void perform(async () => {
                    const value = await window.deepHarness.choose(key)
                    if (value) setForm(previous => ({ ...previous, [key]: value }))
                  })
                }
              >
                选择
              </button>
            </div>
          </label>
        ))}
        <label className="field">
          Profile
          <input
            value={form.profile}
            disabled={runtime?.ready}
            onChange={e => setForm({ ...form, profile: e.target.value })}
          />
        </label>
        <label className="check">
          <input
            type="checkbox"
            disabled={runtime?.ready}
            checked={form.diagnostic}
            onChange={e => setForm({ ...form, diagnostic: e.target.checked })}
          />
          显式离线诊断模式（不是大模型）
        </label>
        {!runtime?.ready && (
          <button className="primary" disabled={busy} type="submit">
            {busy ? '正在启动…' : '启动本机 Agent →'}
          </button>
        )}
        {onClose && (
          <button type="button" onClick={onClose}>
            返回工作台
          </button>
        )}
      </form>
      <p className="muted small">首次配置后自动记住环境。文件与命令的批准方式可在会话中选择。</p>
      {runtime?.error && <p className="notice">{runtime.error}</p>}
    </div>
  )
}

export function CreateSession({ close }: { close: () => void }) {
  const [saved, setSaved] = useState<{ request_id: string; engine: string; harness: string; tools: string[]; permission_mode?: PermissionMode } | null>(
    () => {
      const raw = localStorage.getItem(storageKey('create'))
      return raw ? JSON.parse(raw) : null
    }
  )
  const [engine, setEngine] = useState(saved?.engine ?? 'standard')
  const [harness, setHarness] = useState(saved?.harness ?? 'general')
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(saved ? saved.permission_mode ?? 'legacy' : 'ask')
  const [tools, setTools] = useState(saved?.tools ?? ['list_files', 'search_files', 'read', 'write', 'edit', 'bash'])
  const busy = useStore($busy)
  const [requestId] = useState(() => saved?.request_id ?? crypto.randomUUID())
  const options = [
    ['write', '创建或覆盖文件'],
    ['edit', '精确编辑文件'],
    ['bash', '执行 Shell 命令'],
    ['save_memory', '保存记忆'],
    ['delegate_task', 'Deep 子任务']
  ] as const
  return (
    <div className="modal-backdrop">
      <section className="modal" role="dialog" aria-modal="true" aria-label="新建会话">
        <div className="eyebrow">NEW SESSION</div>
        <h2>为这次任务设置边界</h2>
        {saved && (
          <p className="notice">
            上次创建的结果未确认。以下配置保持原样，点击创建会话会使用原 request_id 核对同一请求。
          </p>
        )}
        <label className="field">
          执行引擎
          <select
            disabled={busy || Boolean(saved)}
            value={engine}
            onChange={e => {
              setEngine(e.target.value)
              setHarness('general')
              setTools(['list_files', 'search_files', 'read', 'write', 'edit', 'bash'])
            }}
          >
            <option value="standard">Standard · 标准 Agent</option>
            <option value="deep">Deep · 深度任务</option>
          </select>
        </label>
        {engine === 'deep' && (
          <label className="field">
            任务模式
            <select
              disabled={busy || Boolean(saved)}
              value={harness}
              onChange={e => {
                setHarness(e.target.value)
                setTools(e.target.value === 'research'
                  ? ['list_files', 'search_files', 'read']
                  : ['list_files', 'search_files', 'read', 'write', 'edit', 'bash'])
              }}
            >
              <option value="general">通用</option>
              <option value="coding">编程</option>
              <option value="research">研究（只读）</option>
            </select>
          </label>
        )}
        <p className="muted small">选择 Agent 如何申请批准。批准模式可在空闲时切换，工具在创建后保持固定。</p>
        <PermissionPicker value={permissionMode} direction="down" disabled={busy || Boolean(saved)} onChange={setPermissionMode} />
        {permissionMode === 'full' && <p className="notice">完全访问会允许已启用的工具访问互联网及工作区外文件，并免除逐项批准。</p>}
        <p className="muted small">本次会话启用的工具</p>
        {options
          .filter(
            ([tool]) =>
              !(engine === 'standard' && tool === 'delegate_task') &&
              !(harness === 'research' && ['write', 'edit', 'bash', 'write_file', 'run_command', 'save_memory'].includes(tool))
          )
          .map(([tool, label]) => (
            <label className="check" key={tool}>
              <input
                type="checkbox"
                disabled={busy || Boolean(saved)}
                checked={tools.includes(tool)}
                onChange={e =>
                  setTools(
                    e.target.checked
                      ? [...tools, tool, ...(tool === 'delegate_task' ? ['check_delegation'] : [])]
                      : tools.filter(t => t !== tool && !(tool === 'delegate_task' && t === 'check_delegation'))
                  )
                }
              />
              {label}
            </label>
          ))}
        <div className="actions">
          <button onClick={close} disabled={busy}>
            取消
          </button>
          <button
            className="primary"
            disabled={busy}
            onClick={() =>
              void perform(async () => {
                const request = saved ?? { request_id: requestId, engine, harness, tools, permission_mode: permissionMode }
                localStorage.setItem(storageKey('create'), JSON.stringify(request))
                setSaved(request)
                const created = await api<Session>('/sessions', 'POST', request)
                localStorage.removeItem(storageKey('create'))
                const draft = localStorage.getItem(storageKey('draft'))
                if (draft) {
                  localStorage.setItem(storageKey('draft', created.id), draft)
                  localStorage.removeItem(storageKey('draft'))
                }
                await loadSessions()
                await openSession(created.id)
                close()
              })
            }
          >
            创建会话
          </button>
        </div>
      </section>
    </div>
  )
}

export function Knowledge({ kind }: { kind: 'skills' | 'memories' | 'mcp/tools' }) {
  const [records, setRecords] = useState<unknown>(null)
  const [text, setText] = useState('')
  const busy = useStore($busy)
  const titles = { skills: '技能库', memories: 'Profile 记忆', 'mcp/tools': 'MCP 工具' }
  return (
    <section className="page" key={kind}>
      <div className="eyebrow">DEEP-HARNESS CAPABILITIES</div>
      <h1>{titles[kind]}</h1>
      <p className="muted">从当前 Profile 的真实后端读取。新技能与记忆仅在后续新会话中生效。</p>
      <button onClick={() => void perform(async () => setRecords(await api(`/${kind}`)))} disabled={busy}>
        刷新列表
      </button>
      {kind !== 'mcp/tools' && (
        <form
          onSubmit={e => {
            e.preventDefault()
            void perform(async () => {
              await api(`/${kind}`, 'POST', kind === 'skills' ? { document: text } : { content: text })
              setText('')
              setRecords(await api(`/${kind}`))
            })
          }}
        >
          <label className="field">
            {kind === 'skills' ? '技能文档（含 name / description frontmatter）' : '添加一条记忆'}
            <textarea
              value={text}
              maxLength={kind === 'skills' ? 20000 : 1000}
              onChange={e => setText(e.target.value)}
              required
              rows={7}
            />
          </label>
          <button className="primary" disabled={busy || !text.trim()}>
            添加到当前 Profile
          </button>
        </form>
      )}
      {kind === 'mcp/tools' && (
        <p className="notice">服务器由当前 Profile 的 mcp.toml 配置；变更后重启后端，新会话按工具名授予权限。</p>
      )}
      {records !== null && <Json value={records} />}
    </section>
  )
}
