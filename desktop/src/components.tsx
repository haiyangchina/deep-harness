import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'
import type { CapabilityManifest, CapabilitySpec, Settings, Session, PermissionMode } from './protocol'
import { PermissionPicker } from './permission-picker'
import { $busy, $runtime, api, initialize, loadSessions, openSession, perform, report, resetView, storageKey } from './state'

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

const policyLabels: Record<string, string> = {
  exempt: '免批',
  per_call: '逐次',
  per_round: '一轮一次'
}

function CapabilityCard({ cap }: { cap: CapabilitySpec }) {
  return (
    <div className="capability-card">
      <div className="capability-head">
        <strong>{cap.label}</strong>
        <span className="muted small">{cap.tools.join(' · ')}</span>
      </div>
      <p className="muted small">{cap.description}</p>
      <p className="muted small">{cap.approval_hint}</p>
    </div>
  )
}

interface CreateRequest {
  request_id: string
  engine: string
  harness: string
  tools?: string[]
  extensions?: string[]
  permission_mode?: PermissionMode
  risk_acknowledged?: boolean
}

export function CreateSession({ close }: { close: () => void }) {
  const [saved, setSaved] = useState<CreateRequest | null>(() => {
    const raw = localStorage.getItem(storageKey('create'))
    return raw ? JSON.parse(raw) : null
  })
  const [engine, setEngine] = useState(saved?.engine ?? 'standard')
  const [harness, setHarness] = useState(saved?.harness ?? 'general')
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(saved ? saved.permission_mode ?? 'legacy' : 'ask')
  const [extensions, setExtensions] = useState<string[]>(saved?.extensions ?? [])
  const [showExtensions, setShowExtensions] = useState(Boolean(saved?.extensions?.length))
  const [manifest, setManifest] = useState<CapabilityManifest | null>(null)
  const busy = useStore($busy)
  const [requestId] = useState(() => saved?.request_id ?? crypto.randomUUID())
  useEffect(() => {
    void api<CapabilityManifest>('/capabilities').then(setManifest).catch(report)
  }, [])
  const extensionOptions = [
    ['save_memory', '保存记忆'],
    ['delegate_task', 'Deep 子任务']
  ] as const
  const visibleExtensions = extensionOptions.filter(
    ([tool]) =>
      !(engine === 'standard' && tool === 'delegate_task') &&
      !(harness === 'research' && tool === 'save_memory')
  )
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
              onChange={e => setHarness(e.target.value)}
            >
              <option value="general">通用</option>
              <option value="coding">编程</option>
              <option value="research">研究（只读）</option>
            </select>
          </label>
        )}
        <p className="muted small">选择 Agent 如何申请批准。批准模式可在空闲时切换，内置能力默认启用。</p>
        <PermissionPicker value={permissionMode} direction="down" disabled={busy || Boolean(saved)} onChange={setPermissionMode} />
        <p className="muted small">内置能力默认授予，无需勾选；除完全访问外，文件操作、终端命令与访问互联网逐次确认。</p>
        {manifest
          ? manifest.capabilities.map(cap => <CapabilityCard cap={cap} key={cap.key} />)
          : <p className="muted small">正在读取能力清单…</p>}
        <button type="button" disabled={busy} onClick={() => setShowExtensions(!showExtensions)}>
          扩展能力（按需开启）{showExtensions ? ' ▴' : ' ▾'}
        </button>
        {showExtensions && visibleExtensions.map(([tool, label]) => (
          <label className="check" key={tool}>
            <input
              type="checkbox"
              disabled={busy || Boolean(saved)}
              checked={extensions.includes(tool)}
              onChange={e =>
                setExtensions(
                  e.target.checked
                    ? [...extensions, tool]
                    : extensions.filter(item => item !== tool)
                )
              }
            />
            {label}
          </label>
        ))}
        {harness === 'research' && (
          <p className="muted small">研究模式仅授予 3 个只读工具，没有命令执行能力。</p>
        )}
        <div className="actions">
          <button onClick={close} disabled={busy}>
            取消
          </button>
          <button
            className="primary"
            disabled={busy}
            onClick={() =>
              void perform(async () => {
                const request = saved ?? {
                  request_id: requestId,
                  engine,
                  harness,
                  permission_mode: permissionMode,
                  extensions
                }
                const payload = request.permission_mode === 'full'
                  ? { ...request, risk_acknowledged: true }
                  : request
                localStorage.setItem(storageKey('create'), JSON.stringify(request))
                setSaved(request)
                const created = await api<Session>('/sessions', 'POST', payload)
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

export function Capabilities() {
  const [manifest, setManifest] = useState<CapabilityManifest | null>(null)
  const busy = useStore($busy)
  useEffect(() => {
    void api<CapabilityManifest>('/capabilities').then(setManifest).catch(report)
  }, [])
  return (
    <section className="page">
      <div className="eyebrow">DEEP-HARNESS CAPABILITIES</div>
      <h1>内置能力</h1>
      <p className="muted">
        基本能力由 harness 默认提供，新建会话时无需逐项勾选；每次写入与命令仍按当前批准模式确认。
      </p>
      <button
        disabled={busy}
        onClick={() => void perform(async () => setManifest(await api<CapabilityManifest>('/capabilities')))}
      >
        刷新清单
      </button>
      {!manifest && <p className="muted small">正在读取本机后端的能力清单…</p>}
      {manifest && (
        <>
          <p className="muted small">
            清单版本 v{manifest.capability_version} · 核心工具 {manifest.core_tools.length} 项 ·
            只读集 {manifest.read_only_core.length} 项
          </p>
          {manifest.capabilities.map(cap => <CapabilityCard cap={cap} key={cap.key} />)}
          <h2>扩展能力</h2>
          <p className="muted small">
            按需在新建会话的「扩展能力」区开启：{manifest.extensions.join(' · ')}；
            MCP 工具由 Profile 的 mcp.toml 配置。
          </p>
          <h2>兼容工具</h2>
          <p className="muted small">
            {manifest.legacy_tools.join(' · ')} 仅供显式 CLI/HTTP 授予，不出现在新建会话清单中。
          </p>
          <h2>安全访问权限</h2>
          <p className="muted small">
            三档只是批准策略，不授予工具。仅「完全访问权限」免除逐项批准，且启用前必须勾选风险确认；
            其余档位调用三类能力时都要审批。
          </p>
          {manifest.permission_policy && (
            <div className="policy-matrix">
              <div className="policy-row policy-head">
                <span>作用域</span>
                {manifest.permission_policy.modes.map(mode => (
                  <span key={mode.key}>{mode.label}</span>
                ))}
              </div>
              {manifest.permission_policy.scope_keys.map(key => (
                <div className="policy-row" key={key}>
                  <span>{key}</span>
                  {manifest.permission_policy?.modes.map(mode => (
                    <span key={mode.key}>
                      {policyLabels[manifest.permission_policy?.matrix[mode.key]?.[key] ?? ''] ?? '—'}
                    </span>
                  ))}
                </div>
              ))}
            </div>
          )}
          {manifest.access_scopes?.map(scope => (
            <div className="capability-card" key={scope.key}>
              <div className="capability-head"><strong>{scope.label}</strong></div>
              <p className="muted small">{scope.description}</p>
              <p className="muted small">{scope.approval_hint}</p>
            </div>
          ))}
          <h2>策略边界</h2>
          <p className="muted small">
            批准模式、工作区与敏感路径、research 只读契约、冻结快照与上下文预算恒定生效，
            不受能力清单影响。全访问模式不会授予未启用的扩展能力。
          </p>
          {manifest.hard_boundaries?.map(boundary => (
            <p className="muted small" key={boundary}>· {boundary}</p>
          ))}
        </>
      )}
    </section>
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
