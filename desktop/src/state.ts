import { atom } from 'nanostores'
import type { RuntimeInfo, Session, SessionStatus, Message, JournalEvent, ApiRequest, ServerCatalog } from './protocol'
import { HttpError, requestJson, workspacePath } from './workspace-api'

interface LiveMessage {
  seq: number
  turn: number
  message: Message
}
interface MessagePage {
  items: LiveMessage[]
  has_more: boolean
}

export interface Conversation {
  id: string
  messages: Message[]
  messageRows: LiveMessage[]
  messageCursor: number | null
  hasOlderMessages: boolean
  eventCursor: number | null
  status: SessionStatus | null
  events: JournalEvent[]
  usage: unknown
  delegations: unknown[]
}
export const $runtime = atom<RuntimeInfo | null>(null)
export const $sessions = atom<Session[]>([])
export const $sessionCursor = atom<string | null>(null)
export const $conversation = atom<Conversation | null>(null)
export const $error = atom('')
export const $busy = atom(false)
let selection = 0
let refreshVersion = 0
let timer: ReturnType<typeof setTimeout> | undefined
let eventRefresh: ReturnType<typeof setTimeout> | undefined
let inspector = 'activity'

export function resetView() {
  closeSession()
  $sessions.set([])
  $sessionCursor.set(null)
}

export function closeSession() {
  clearTimeout(timer)
  clearTimeout(eventRefresh)
  selection++
  $conversation.set(null)
}

export function api<T>(path: string, method: ApiRequest['method'] = 'GET', body?: unknown): Promise<T> {
  return workspaceApi<T>(path, method, body)
}
export function resourcePath(path: string): string {
  const workspace = $runtime.get()?.settings.profile
  if (!workspace) throw new Error('工作区尚未就绪')
  return workspacePath(workspace) + path
}
export async function workspaceApi<T>(
  path: string, method: ApiRequest['method'] = 'GET', body?: unknown, upload?: ApiRequest['upload']
): Promise<T> {
  return requestJson({ path: resourcePath(path), method, body, upload })
}
export function report(error: unknown) {
  $error.set(error instanceof Error ? error.message : String(error))
}
export function storageKey(kind: string, session = '') {
  const settings = $runtime.get()?.settings
  return JSON.stringify(['deep-harness', settings?.stateDir, settings?.workspace, settings?.profile, kind, session])
}

async function sessionPage(cursor: string) {
  return workspaceApi<{ sessions: Session[]; next_cursor: string | null }>(
    `/sessions?limit=100&after=${encodeURIComponent(cursor)}`
  )
}

export async function loadSessions() {
  const runtime = $runtime.get()
  const page = await sessionPage('')
  if ($runtime.get() !== runtime) return
  $sessions.set(page.sessions)
  $sessionCursor.set(page.next_cursor)
}

export async function loadMoreSessions() {
  const cursor = $sessionCursor.get()
  if (!cursor) return
  const runtime = $runtime.get()
  const page = await sessionPage(cursor).catch(async error => {
    if (!(error instanceof HttpError) || error.code !== 'session_list_changed') throw error
    if ($runtime.get() === runtime) await loadSessions()
    return null
  })
  if (!page) return
  if ($runtime.get() !== runtime || $sessionCursor.get() !== cursor) return
  const sessions = new Map($sessions.get().map(session => [session.id, session]))
  for (const session of page.sessions) sessions.set(session.id, session)
  $sessions.set([...sessions.values()])
  $sessionCursor.set(page.next_cursor)
}

function mergeMessages(current: Conversation, page: MessagePage) {
  const rows = new Map(current.messageRows.map(row => [row.seq, row]))
  for (const row of page.items) rows.set(row.seq, row)
  const messageRows = [...rows.values()].sort((a, b) => a.turn - b.turn || a.seq - b.seq)
  return { messageRows, messages: messageRows.map(row => row.message) }
}

export async function loadOlderMessages() {
  const current = $conversation.get()
  if (!current?.hasOlderMessages) return
  const generation = selection
  const before = current.messageRows.reduce((min, row) => Math.min(min, row.seq), Infinity)
  const page = await workspaceApi<MessagePage>(`/sessions/${current.id}/messages?before=${before}`)
  const latest = $conversation.get()
  if (generation !== selection || !latest) return
  $conversation.set({ ...latest, ...mergeMessages(latest, page), hasOlderMessages: page.has_more })
}

export async function refreshInspector(tab: string) {
  inspector = tab
  const current = $conversation.get()
  if (!current || tab === 'activity') return
  const generation = selection
  const route = `/sessions/${current.id}`
  if (tab === 'state') {
    const status = await workspaceApi<SessionStatus>(`${route}?include_runtime=true`)
    const latest = $conversation.get()
    if (generation === selection && latest?.status) {
      $conversation.set({ ...latest, status: { ...latest.status, runtime: status.runtime } })
    }
    return
  }
  if (tab === 'usage') {
    const usage = await api(`${route}/usage`)
    const latest = $conversation.get()
    if (generation === selection && latest) $conversation.set({ ...latest, usage })
  }
  if (tab === 'delegations') {
    const { delegations } = await api<{ delegations: unknown[] }>(`${route}/delegations`)
    const latest = $conversation.get()
    if (generation === selection && latest) $conversation.set({ ...latest, delegations })
  }
}

export async function refreshConversation() {
  const current = $conversation.get()
  if (!current) return
  const generation = selection
  const version = ++refreshVersion
  const route = `/sessions/${current.id}`
  const first = current.messageCursor === null
  const query = first ? '' : `?after=${current.messageCursor}`
  let backlog = false
  function latest() {
    return selection === generation && version === refreshVersion ? $conversation.get() : null
  }
  function refreshFailed(error: unknown) {
    if (latest()) report(error)
  }
  await Promise.all([
    workspaceApi<SessionStatus>(route).then(status => {
      const view = latest()
      if (view) $conversation.set({ ...view, status: { ...status, runtime: view.status?.runtime } })
    }).catch(refreshFailed),
    workspaceApi<MessagePage>(`${route}/messages${query}`).then(page => {
      const view = latest()
      if (!view) return
      backlog ||= !first && page.has_more
      if (page.items.length || first) {
        $conversation.set({
          ...view,
          ...mergeMessages(view, page),
          messageCursor: Math.max(view.messageCursor ?? 0, ...page.items.map(row => row.seq)),
          hasOlderMessages: first ? page.has_more : view.hasOlderMessages
        })
      }
    }).catch(refreshFailed)
  ])
  return backlog
}

async function watchEvents(generation: number, route: string) {
  while (generation === selection) {
    const cursor = $conversation.get()?.eventCursor
    const query = cursor == null ? 'tail=true' : `after=${cursor}&wait=12`
    const requestedAt = performance.now()
    try {
      const { events } = await workspaceApi<{ events: JournalEvent[] }>(`${route}/event-history?${query}`)
      const view = $conversation.get()
      if (generation !== selection || !view) return
      if (!events.length && cursor != null) {
        const delay = 1000 - (performance.now() - requestedAt)
        if (delay > 0) await new Promise(resolve => setTimeout(resolve, delay))
        continue
      }
      $conversation.set({
        ...view,
        events: [...view.events, ...events].slice(-2000),
        eventCursor: events.at(-1)?.seq ?? cursor ?? 0
      })
      if (events.some(event => event.type === 'admission.accepted' ||
          event.type === 'message.committed' || event.type.startsWith('execution.') ||
          event.type.startsWith('approval.') || event.type.startsWith('interaction.') ||
          event.type === 'artifact.ready')) {
        if (!eventRefresh) eventRefresh = setTimeout(() => {
          eventRefresh = undefined
          if (generation === selection) void refreshConversation()
        }, 60)
      }
      if ((inspector === 'usage' && events.some(e => e.type === 'message.committed')) ||
          (inspector === 'delegations' && events.some(e => e.type.startsWith('delegation.')))) {
        void refreshInspector(inspector).catch(error => {
          if (generation === selection) report(error)
        })
      }
    } catch (error) {
      if (generation !== selection) return
      if (error instanceof HttpError && error.status === 404) return
      await new Promise(resolve => setTimeout(resolve, 1200))
    }
  }
}

async function poll(generation: number) {
  if (generation !== selection) return
  const backlog = await refreshConversation()
  if (generation !== selection) return
  const active = $conversation.get()?.status?.active_admissions.some(
    row => ['queued', 'started', 'waiting_approval'].includes(row.status)
  )
  const delay = backlog ? 100 : document.hidden ? 8000 : active ? 1200 : 4000
  timer = setTimeout(() => void poll(generation), delay)
}

export async function openSession(id: string) {
  clearTimeout(timer)
  clearTimeout(eventRefresh)
  eventRefresh = undefined
  const generation = ++selection
  $conversation.set({
    id, messages: [], messageRows: [], messageCursor: null, hasOlderMessages: false,
    status: null, events: [], eventCursor: null, usage: null, delegations: []
  })
  $error.set('')
  void watchEvents(generation, `/sessions/${id}`)
  await poll(generation)
  if (generation === selection) void refreshInspector(inspector).catch(report)
}

export async function initialize() {
  const info = window.deepHarness ? await window.deepHarness.info() : await browserRuntime()
  $runtime.set(info)
  if (info.ready) await loadSessions()
}

export async function browserRuntime(selected?: string): Promise<RuntimeInfo> {
  const catalog = await requestJson<ServerCatalog>({ method: 'GET', path: '/api/v1/instance' })
  const preferred = selected ?? localStorage.getItem('deep-harness-web:project')
  const workspace = catalog.workspaces.find(item => item.id === preferred) ??
    catalog.workspaces.find(item => item.id === catalog.default_workspace)
  const id = workspace?.id ?? ''
  if (id) localStorage.setItem('deep-harness-web:project', id)
  return {
    ready: Boolean(id), projectId: id, catalog,
    projects: catalog.workspaces.map(item => ({
      id: item.id, name: item.label || item.id, workspace: item.id, profile: item.id
    })),
    settings: { python: '', configFile: '', diagnostic: false, stateDir: location.origin,
      profile: id, workspace: workspace?.label || id }
  }
}

/** No write retry is automatic, including durable submissions with an unknown ACK. */
export async function perform(work: () => Promise<void>) {
  if ($busy.get()) return
  $busy.set(true)
  $error.set('')
  try {
    await work()
  } catch (error) {
    report(error)
  } finally {
    $busy.set(false)
  }
}
