/** Connection error diagnostics omit request bodies, credentials and query text. */
function errorCode(error: unknown): string {
  const detail = error as { code?: unknown; name?: string; cause?: { code?: unknown } } | null
  for (const code of [detail?.code, detail?.cause?.code]) {
    if (typeof code === 'string' && /^(E[A-Z0-9_]+|UND_ERR_[A-Z0-9_]+)$/.test(code)) return code
  }
  if (detail?.name === 'TimeoutError' || detail?.name === 'AbortError') return 'TIMEOUT'
  return error instanceof SyntaxError ? 'INVALID_JSON' : 'NETWORK_ERROR'
}

export async function localJson(
  endpoint: string,
  method: string,
  token: string,
  body?: unknown,
  timeoutMs = 20_000,
  upload?: { name: string; data: ArrayBuffer },
  structured = false,
  binary = false
): Promise<unknown> {
  let response: Response
  let result: unknown
  try {
    response = await fetch(endpoint, {
      method, redirect: 'error',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': upload ? 'application/octet-stream' : 'application/json',
        ...(upload ? { 'X-Filename': encodeURIComponent(upload.name), 'X-Harness-Upload': '1' } : {})
      },
      body: upload?.data ?? (body === undefined ? undefined : JSON.stringify(body)),
      signal: AbortSignal.timeout(timeoutMs)
    })
    result = binary && response.ok
      ? { data: await response.arrayBuffer(), media_type: response.headers.get('content-type') }
      : await response.json()
  } catch (error) {
    const url = new URL(endpoint)
    const outcome = method === 'GET' ? '读取失败' : '请求结果未确认；先核对状态，不要自动重发'
    throw new Error(`${outcome}（${errorCode(error)}）：${method} ${url.origin}${url.pathname}`)
  }
  if (structured) return { status: response.status, body: result }
  if (!response.ok) throw new Error(`本机 API 返回 HTTP ${response.status}：${JSON.stringify(result)}`)
  return result
}
