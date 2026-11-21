import type { ApiRequest } from '../src/protocol.ts'

/** A renderer request can only address this desktop's profile on its own child. */
export function apiPath(profile: string, request: ApiRequest): string {
  if (!['GET', 'POST', 'DELETE'].includes(request.method)) throw new Error('Unsupported method')
  if (!/^\/(sessions|jobs|skills|memories|memory-candidates|mcp\/tools|capabilities)([/?][A-Za-z0-9_/?=&.%~-]*)?$/.test(request.path)) {
    throw new Error('Unsupported local API path')
  }
  const decoded = decodeURIComponent(request.path)
  if (decoded.includes('..') || decoded.includes('\\') || decoded.includes('#')) throw new Error('Invalid API path')
  return `/profiles/${encodeURIComponent(profile)}${request.path}`
}
