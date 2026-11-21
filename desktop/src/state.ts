import { atom } from 'nanostores'
import type { RuntimeInfo, Session, SessionStatus, Message, JournalEvent, ApiRequest } from './protocol'

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
let inspector = 'activity'

export function resetView() {
  clearTimeout(timer)
  selection++
  $conversation.set(null)
  $sessions.set([])
  $sessionCursor.set(null)
}

export function api<T>(path: string, method: ApiRequest['method'] = 'GET', body?: unknown): Promise<T> {
  return window.deepHarness.request<T>({ path, method, body })
}
export function report(error: unknown) {
  $error.set(error instanceof Error ? error.message : String(error))
}
export function storageKey(kind: string, session = '') {
  const settings = $runtime.get()?.settings
  return JSON.stringify(['deep-harness', settings?.stateDir, settings?.workspace, settings?.profile, kind, session])
}

async function sessionPage(cursor: string) {
  return api<{ sessions: Session[]; next_cursor: string | null }>(
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
  const page = await sessionPage(cursor)
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
  const page = await api<MessagePage>(`/sessions/${current.id}/messages/live?before=${before}`)
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
    const status = await api<SessionStatus>(`${route}/status`)
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
  const eventQuery = current.eventCursor === null ? 'tail=true' : `after=${current.eventCursor}`
  let backlog = false
  function latest() {
    return selection === generation && version === refreshVersion ? $conversation.get() : null
  }
  function refreshFailed(error: unknown) {
    if (latest()) report(error)
  }
  await Promise.all([
    api<SessionStatus>(`${route}/status?include_runtime=false`).then(status => {
      const view = latest()
      if (view) $conversation.set({ ...view, status: { ...status, runtime: view.status?.runtime } })
    }).catch(refreshFailed),
    api<MessagePage>(`${route}/messages/live${query}`).then(page => {
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
    }).catch(refreshFailed),
    api<{ events: JournalEvent[] }>(`${route}/events?${eventQuery}`).then(({ events }) => {
      const view = latest()
      if (!view) return
      const newEvents = events.filter(event => event.seq > (view.eventCursor ?? 0))
      backlog ||= newEvents.length >= 500
      if (newEvents.length || view.eventCursor === null) {
        $conversation.set({
          ...view,
          events: [...view.events, ...newEvents].slice(-2000),
          eventCursor: newEvents.at(-1)?.seq ?? view.eventCursor ?? 0
        })
      }
      if ((inspector === 'usage' && newEvents.some(e => e.type === 'message.committed')) ||
          (inspector === 'delegations' && newEvents.some(e => e.type.startsWith('delegation.')))) {
        void refreshInspector(inspector).catch(refreshFailed)
      }
    }).catch(refreshFailed)
  ])
  return backlog
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
  const generation = ++selection
  $conversation.set({
    id, messages: [], messageRows: [], messageCursor: null, hasOlderMessages: false,
    status: null, events: [], eventCursor: null, usage: null, delegations: []
  })
  $error.set('')
  await poll(generation)
  if (generation === selection) void refreshInspector(inspector).catch(report)
}

export async function initialize() {
  const info = await window.deepHarness.info()
  $runtime.set(info)
  if (info.ready) await loadSessions()
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
