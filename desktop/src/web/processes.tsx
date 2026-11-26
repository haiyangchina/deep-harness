import { useEffect, useState } from 'react'
import { api, requestId } from './api'

type Process = { id: string; command: string; status: string; stdout: string; stderr: string }

export function WebProcesses({ sessionPath }: { sessionPath: string }) {
  const route = sessionPath + '/processes'
  const [open, setOpen] = useState(false)
  const [command, setCommand] = useState('')
  const [request, setRequest] = useState<{ command: string; request_id: string }>()
  const [rows, setRows] = useState<Process[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    let mounted = true
    const refresh = () => void api<Process[]>(route).then(processes => {
      if (mounted) setRows(processes)
    }).catch(problem => { if (mounted) setError(problem.message) })
    refresh()
    const timer = setInterval(refresh, 1800)
    return () => { mounted = false; clearInterval(timer) }
  }, [open, route])

  async function perform(action: () => Promise<void>) {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await action()
      setRows(await api<Process[]>(route))
    } catch (problem) {
      setError((problem as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return <details className="artifacts" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>沙箱开发进程</summary>
    <form className="actions" onSubmit={event => {
      event.preventDefault()
      void perform(async () => {
        const operation = request ?? { command: command.trim(), request_id: requestId() }
        setRequest(operation)
        await api(route, operation)
        setRequest(undefined)
        setCommand('')
      })
    }}>
      <input aria-label="沙箱开发命令" value={command} disabled={busy || !!request}
        onChange={event => setCommand(event.target.value)} />
      <button disabled={busy || !command.trim()}>{request ? '核对启动' : '启动'}</button>
    </form>
    {error && <p role="alert">{error}</p>}
    {rows.map(row => <details key={row.id}>
      <summary>{row.command} · {row.status}</summary>
      <pre>{row.stdout}{row.stderr}</pre>
      {row.status === 'running' && <button disabled={busy}
        onClick={() => void perform(async () => { await api(`${route}/${row.id}/stop`, {}) })}>停止</button>}
    </details>)}
  </details>
}
