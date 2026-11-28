import type { ApiRequest } from '../src/protocol.ts'

const workspaceRoutes = {
  GET: [
    /^\/sessions(?:\/[a-z0-9-]+(?:\/(?:messages|event-history|submission|task-control|processes|deletion))?)?$/,
    /^\/session-deletions$/,
    /^\/sessions\/[a-z0-9-]+\/(?:usage|delegations|messages\/search|bundle|delivery|images\/[a-z0-9-]+)$/,
    /^\/runs\/[a-z0-9-]+(?:\/(?:artifacts|diff|effects\/[A-Za-z0-9:._-]+\/status))?$/,
    /^\/artifacts\/[a-z0-9-]+$/,
    /^\/(?:capabilities|mcp\/tools|skills|memories(?:\/search)?|memory-candidates|bundles\/[a-f0-9]{64})$/,
    /^\/jobs(?:\/[a-z0-9-]+)?$/
  ],
  POST: [
    /^\/sessions(?:\/[a-z0-9-]+\/(?:runs|images|attachments|task-control|processes(?:\/[a-f0-9]{64}\/stop)?))?$/,
    /^\/sessions\/[a-z0-9-]+\/(?:forks|permissions|revoke-reuse|delivery|curation|delegations\/[A-Za-z0-9:._-]+\/(?:steers|stop))$/,
    /^\/runs\/[a-z0-9-]+\/(?:cancel|retry-workspace|retry-summary|reconciliation|approvals\/[A-Za-z0-9:._-]+\/decision)$/,
    /^\/(?:jobs(?:\/[a-z0-9-]+\/commands)?|skills|memories|memory-candidates\/[a-z0-9-]+\/decision|bundles(?:\/[a-f0-9]{64})?)$/
  ],
  PATCH: [/^\/sessions\/[a-z0-9-]+$/],
  DELETE: [/^\/(?:skills|memories|sessions)\/[A-Za-z0-9_-]+$/]
}

/** A renderer request can only address this desktop's profile on its own child. */
export function apiPath(profile: string, request: ApiRequest): string {
  if (!Object.hasOwn(workspaceRoutes, request.method)) throw new Error('Unsupported method')
  const prefix = `/api/v1/workspaces/${encodeURIComponent(profile)}`
  if (request.path.startsWith(prefix + '/')) {
    const url = new URL(request.path, 'http://127.0.0.1')
    const route = decodeURIComponent(url.pathname.slice(prefix.length))
    const decoded = decodeURIComponent(request.path.split('?', 1)[0])
    if (url.hash || decoded.includes('\\') || decoded.includes('..') ||
        !url.pathname.startsWith(prefix + '/') ||
        !workspaceRoutes[request.method].some(pattern => pattern.test(route))) {
      throw new Error('Unsupported local workspace API path')
    }
    return url.pathname + url.search
  }
  throw new Error('Unsupported local workspace')
}
