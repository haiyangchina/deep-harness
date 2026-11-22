export async function api<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(path, body === undefined ? undefined : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.message || detail?.detail?.[0]?.msg || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

export async function upload(path: string, file: File): Promise<{ id: string }> {
  const response = await fetch(path, {
    method: 'POST', body: file,
    headers: {
      'Content-Type': 'application/octet-stream',
      'X-Harness-Upload': '1',
      'X-Filename': encodeURIComponent(file.name)
    }
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.message || `上传失败（${response.status}）`)
  }
  return response.json()
}

export function requestId(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte =>
    byte.toString(16).padStart(2, '0')
  ).join('')
}
