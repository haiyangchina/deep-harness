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

export async function loadSessions() {
  const sessions: Session[] = []
  let cursor: string | null = ''
  do {
    const page: { sessions: Session[]; next_cursor: string | null } = await api(
      `/sessions?limit=100&after=${encodeURIComponent(cursor)}`
    )
    sessions.push(...page.sessions)
    cursor = page.next_cursor
  } while (cursor)
  $sessions.set(sessions)
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
  const [status, page, events] = await Promise.allSettled([
    api<SessionStatus>(`${route}/status?include_runtime=false`),
    api<MessagePage>(`${route}/messages/live${query}`),
    api<{ events: JournalEvent[] }>(`${route}/events?${eventQuery}`)
  ])
  const latest = $conversation.get()
  if (selection !== generation || version !== refreshVersion || !latest) return
  const next = { ...latest }
  if (status.status === 'fulfilled') {
    next.status = { ...status.value, runtime: latest.status?.runtime }
  }
  if (page.status === 'fulfilled') {
    if (page.value.items.length) Object.assign(next, mergeMessages(latest, page.value))
    next.messageCursor = Math.max(current.messageCursor ?? 0, ...page.value.items.map(row => row.seq))
    if (first) next.hasOlderMessages = page.value.has_more
  }
  let newEvents: JournalEvent[] = []
  if (events.status === 'fulfilled') {
    newEvents = events.value.events.filter(event => event.seq > (latest.eventCursor ?? 0))
    next.events = [...latest.events, ...newEvents].slice(-2000)
    next.eventCursor = newEvents.at(-1)?.seq ?? latest.eventCursor ?? 0
  }
  $conversation.set(next)
  if ((inspector === 'usage' && newEvents.some(e => e.type === 'message.committed')) ||
      (inspector === 'delegations' && newEvents.some(e => e.type.startsWith('delegation.')))) {
    void refreshInspector(inspector).catch(report)
  }
  for (const result of [status, page, events]) {
    if (result.status === 'rejected') report(result.reason)
  }
}

async function poll(generation: number) {
  if (generation !== selection) return
  try {
    await refreshConversation()
  } catch (error) {
    report(error)
  }
  if (generation === selection) timer = setTimeout(() => void poll(generation), 1200)
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
