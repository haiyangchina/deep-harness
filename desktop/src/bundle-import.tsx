import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'
import { $busy, api, perform, report, storageKey } from './state'

type Bundle = {
  id: string; revision: string; diff: string; diff_truncated: boolean
  verification: { command?: string; exit_code?: number } | null
  delivery: { status: string; observations?: Record<string, string[]> } | null
  files: {
    path: string; before: { sha256: string } | null; after: { sha256: string } | null
    conflict: boolean
  }[]
}

export function BundleImport({ workspace }: { workspace: string }) {
  const busy = useStore($busy)
  const [bundle, setBundle] = useState<Bundle>()
  useEffect(() => {
    const id = localStorage.getItem(storageKey('change-bundle'))
    if (!id) return
    let mounted = true
    void api<Bundle>(`/bundles/${id}`).then(value => {
      if (mounted) setBundle(value)
    }).catch(error => { if (mounted) report(error) })
    return () => { mounted = false }
  }, [workspace])

  const action = (command: 'apply' | 'undo' | 'reconcile') => void perform(async () => {
    if (!bundle) return
    const route = `/bundles/${bundle.id}`
    await api(route, 'POST', { action: command })
    setBundle(await api<Bundle>(route))
  })

  return <section className="page">
    <h1>导入变更包</h1>
    <p>目标项目 <code>{workspace}</code></p>
    <label className="field">选择 Web 导出的变更包
      <input type="file" accept=".json,application/json" disabled={busy}
        onChange={event => {
          const file = event.currentTarget.files?.[0]
          event.currentTarget.value = ''
          if (!file) return
          void perform(async () => {
            if (file.size > 8 * 1024 * 1024) throw new Error('变更包超过 8 MiB。')
            const preview = await api<Bundle>('/bundles', 'POST', { package: await file.text() })
            localStorage.setItem(storageKey('change-bundle'), preview.id)
            setBundle(preview)
          })
        }} />
    </label>
    {bundle && <>
      <p>变更包 <code>{bundle.id.slice(0, 16)}</code></p>
      {bundle.verification && <p>包内验收记录 <code>{bundle.verification.command}</code>
        {' · '}退出码 {bundle.verification.exit_code}</p>}
      <ul>{bundle.files.map(file => <li key={file.path}>
        <code>{file.path}</code> · {file.before?.sha256.slice(0, 10) ?? '新增'}
        {' → '}{file.after?.sha256.slice(0, 10) ?? '删除'}
        {file.conflict && !bundle.delivery && <strong> · 基线冲突</strong>}
        {bundle.delivery?.observations?.[file.path] && (
          <small> · 核对：{bundle.delivery.observations[file.path].map(value =>
            value === 'before' ? '交付前' : '交付后').join('、') || '与两端都不匹配'}</small>
        )}
      </li>)}</ul>
      {bundle.diff && <pre className="coding-diff">{bundle.diff}</pre>}
      {bundle.diff_truncated && <p>文本差异仅显示可比较的部分。</p>}
      {bundle.delivery?.status === 'applied' ?
        <button disabled={busy} onClick={() => action('undo')}>撤销已应用变更</button> :
        ['applying', 'undoing'].includes(bundle.delivery?.status ?? '') ?
          <button disabled={busy} onClick={() => action('reconcile')}>核对交付</button> :
          (!bundle.delivery || bundle.delivery.status === 'not_applied') ?
            <button className="primary" disabled={busy || bundle.files.some(file => file.conflict)}
              onClick={() => action('apply')}>应用到当前项目</button> :
            <p>交付状态 · {bundle.delivery.status}</p>}
    </>}
  </section>
}
