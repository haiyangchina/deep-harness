import type { ApiRequest } from './protocol'

export class HttpError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) { super(message) }
}

type ErrorBody = { message?: string; code?: string; detail?: { msg?: string }[] | string }

export function workspacePath(workspace: string) {
  return `/api/v1/workspaces/${encodeURIComponent(workspace)}`
}

export function newRequestId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte =>
    byte.toString(16).padStart(2, '0')
  ).join('')
}

export async function requestJson<T>({ path, method, body, upload }: ApiRequest): Promise<T> {
  let result: { status: number; body: T | ErrorBody | null }
  if (window.deepHarness) {
    result = await window.deepHarness.request({ path, method, body, upload })
  } else {
    const headers: Record<string, string> | undefined = upload
      ? { 'Content-Type': 'application/octet-stream', 'X-Harness-Upload': '1',
          'X-Filename': encodeURIComponent(upload.name) }
      : method === 'GET' ? undefined : { 'Content-Type': 'application/json' }
    const response = await fetch(path, {
      method, headers, body: upload?.data ?? (body === undefined ? undefined : JSON.stringify(body))
    })
    result = {
      status: response.status,
      body: response.ok ? await response.json() : await response.json().catch(() => null)
    }
  }
  if (result.status < 200 || result.status >= 300) {
    const detail = result.body as ErrorBody | null
    const validation = Array.isArray(detail?.detail) ? detail.detail[0]?.msg : detail?.detail
    throw new HttpError(detail?.message || validation || `HTTP ${result.status}`, result.status, detail?.code)
  }
  return result.body as T
}

export async function downloadResource(path: string, filename: string) {
  let blob: Blob
  if (window.deepHarness) {
    const result = await window.deepHarness.request<{
      status: number; body: { data: ArrayBuffer; media_type: string; message?: string }
    }>({ path, method: 'GET', response: 'binary' })
    if (result.status >= 300) throw new HttpError(result.body.message || '下载失败', result.status)
    blob = new Blob([result.body.data], { type: result.body.media_type })
  } else {
    const result = await fetch(path)
    if (!result.ok) {
      const detail = await result.json().catch(() => null) as ErrorBody | null
      throw new HttpError(detail?.message || '下载失败', result.status, detail?.code)
    }
    blob = await result.blob()
  }
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.append(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function imageResource(path: string) {
  if (!window.deepHarness) return path
  const { status, body } = await window.deepHarness.request<{
    status: number; body: { data: ArrayBuffer; media_type: string }
  }>({ path, method: 'GET', response: 'binary' })
  if (status >= 300) throw new HttpError('图片不可用', status)
  return URL.createObjectURL(new Blob([body.data], { type: body.media_type }))
}
