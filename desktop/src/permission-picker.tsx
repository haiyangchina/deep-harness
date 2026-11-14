import { useEffect, useRef, useState } from 'react'
import { FullAccessConfirm, useAccessScopes } from './full-access-confirm'
import type { PermissionMode } from './protocol'
import { permissionLabel, permissionOptions } from './permissions'

interface PermissionPickerProps {
  value: PermissionMode
  disabled?: boolean
  direction?: 'up' | 'down'
  onChange: (value: Exclude<PermissionMode, 'legacy'>) => void
}

function Icon({ kind }: { kind: string }) {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
    {kind === 'hand'
      ? <path d="M8 13V5a1.5 1.5 0 0 1 3 0v6-8a1.5 1.5 0 0 1 3 0v8-6a1.5 1.5 0 0 1 3 0v7-4a1.5 1.5 0 0 1 3 0v7c0 4-3 7-7 7-3 0-5-2-6-4l-3-5a1.5 1.5 0 0 1 2-2l2 2Z" />
      : <><path d="m12 3 8 3v6c0 5-4 8-8 10-4-2-8-5-8-10V6l8-3Z" />{kind === 'full' ? <path d="M12 7v6m0 3v1" /> : <path d="m8 12 3 3 5-6" />}</>}
  </svg>
}

export function PermissionPicker({ value, disabled, direction = 'up', onChange }: PermissionPickerProps) {
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const scopes = useAccessScopes()
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (!open) return
    root.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus()
    function outside(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])
  useEffect(() => { if (disabled) setOpen(false) }, [disabled])
  function focusOption(index: number) {
    root.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')[index]?.focus()
  }
  return <div className={`permission-picker ${value === 'full' ? 'permission-full' : ''}`} ref={root}
    onKeyDown={event => {
      if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() }
      if (open && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault()
        const rows = [...root.current!.querySelectorAll('[role="menuitemradio"]')]
        const current = rows.indexOf(document.activeElement!)
        const step = event.key === 'ArrowDown' ? 1 : -1
        focusOption(current < 0 ? (step === 1 ? 0 : rows.length - 1) : (current + step + rows.length) % rows.length)
      }
    }}>
    <button type="button" className="permission-trigger" disabled={disabled} ref={trigger}
      aria-label={`权限模式：${permissionLabel(value)}`} aria-haspopup="menu" aria-expanded={open}
      onClick={() => setOpen(!open)}>
      <Icon kind={value === 'full' ? 'full' : value === 'auto' ? 'shield' : 'hand'} />
      <span>{permissionLabel(value)}</span><span aria-hidden="true">⌄</span>
    </button>
    {open && <div className={`permission-menu permission-menu-${direction}`} role="menu" aria-label="权限模式">
      {permissionOptions.map(row => <button type="button" key={row.value} role="menuitemradio"
        aria-checked={value === row.value || (value === 'legacy' && row.value === 'ask')}
        className={row.value === 'full' ? 'permission-danger' : ''}
        onClick={() => {
          setOpen(false)
          trigger.current?.focus()
          if (row.value === 'full' && value !== 'full') {
            setConfirming(true)
            return
          }
          onChange(row.value)
        }}>
        <Icon kind={row.icon} /><span><strong>{row.label}</strong><small>{row.description}</small></span>
        {(value === row.value || (value === 'legacy' && row.value === 'ask')) && <span aria-hidden="true">✓</span>}
      </button>)}
      <p>内置能力默认启用。除完全访问外，文件操作、终端命令与访问互联网均逐次确认；运行状态目录不开放。</p>
    </div>}
    {confirming && (
      <FullAccessConfirm
        scopes={scopes}
        onCancel={() => setConfirming(false)}
        onConfirm={() => { setConfirming(false); onChange('full') }}
      />
    )}
  </div>
}
