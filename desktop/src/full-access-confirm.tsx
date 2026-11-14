import { useEffect, useRef, useState } from 'react'
import type { AccessScope } from './protocol'
import { api } from './state'

const FALLBACK_SCOPES: AccessScope[] = [
  {
    key: 'file_operations',
    label: '文件操作',
    description: '读取、创建、修改、上传、删除此计算机上任意位置的文件。',
    scope_keys: [],
    approval_hint: ''
  },
  {
    key: 'terminal_commands',
    label: '终端命令',
    description: '运行命令、安装软件或修改系统设置。',
    scope_keys: [],
    approval_hint: ''
  },
  {
    key: 'internet_access',
    label: '访问互联网',
    description: '访问网站、发送数据或使用已启用的连接器。',
    scope_keys: [],
    approval_hint: ''
  }
]

let cachedScopes: AccessScope[] | null = null

export function useAccessScopes(): AccessScope[] {
  const [scopes, setScopes] = useState<AccessScope[]>(cachedScopes ?? FALLBACK_SCOPES)
  useEffect(() => {
    if (cachedScopes) return
    void api<{ access_scopes?: AccessScope[] }>('/capabilities')
      .then(data => {
        if (data.access_scopes?.length) {
          cachedScopes = data.access_scopes
          setScopes(data.access_scopes)
        }
      })
      .catch(() => {})
  }, [])
  return scopes
}

interface FullAccessConfirmProps {
  scopes: AccessScope[]
  onCancel: () => void
  onConfirm: () => void
}

export function FullAccessConfirm({ scopes, onCancel, onConfirm }: FullAccessConfirmProps) {
  const [checked, setChecked] = useState(false)
  const checkbox = useRef<HTMLInputElement>(null)
  const cancelRef = useRef(onCancel)
  cancelRef.current = onCancel
  useEffect(() => {
    setChecked(false)
    checkbox.current?.focus()
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.stopPropagation()
        cancelRef.current()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])
  return (
    <div className="modal-backdrop modal-backdrop-nested">
      <section className="modal full-access-modal" role="dialog" aria-modal="true" aria-label="允许完全访问">
        <div className="eyebrow">FULL ACCESS</div>
        <h2>允许完全访问</h2>
        <p className="muted">
          完全访问权限会放开以下三类能力，且不再逐项请求批准。它只对当前会话已启用的工具生效。
        </p>
        <div className="full-access-scopes">
          {scopes.map(scope => (
            <div className="full-access-scope" key={scope.key}>
              <strong>{scope.label}</strong>
              <p className="muted small">{scope.description}</p>
            </div>
          ))}
        </div>
        <p className="notice">
          这不是操作系统级沙箱。Shell 一旦执行就能访问本机账号权限内的文件与网络；
          运行状态目录仍不向文件工具开放。
        </p>
        <label className="check full-access-check">
          <input
            ref={checkbox}
            type="checkbox"
            checked={checked}
            onChange={event => setChecked(event.target.checked)}
          />
          我已了解风险，并对自己的数据安全负责
        </label>
        <div className="actions">
          <button type="button" onClick={onCancel}>取消</button>
          <button
            type="button"
            className="primary danger"
            disabled={!checked}
            onClick={onConfirm}
          >
            允许完全访问
          </button>
        </div>
      </section>
    </div>
  )
}
