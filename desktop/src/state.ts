import { atom } from 'nanostores'
import type { RuntimeInfo, Session, SessionStatus, Message, JournalEvent, ApiRequest } from './protocol'

export interface Conversation {
  id: string
  messages: Message[]
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

export async function refreshConversation() {
  const current = $conversation.get()
  if (!current) return
  const generation = selection
  const version = ++refreshVersion
  const route = `/sessions/${current.id}`
  const cursor = current.events.at(-1)?.seq ?? 0
  const [status, history, events, usage, delegations] = await Promise.all([
    api<SessionStatus>(`${route}/status`),
    api<{ messages: Message[] }>(route),
    api<{ events: JournalEvent[] }>(`${route}/events?after=${cursor}`),
    api(`${route}/usage`),
    api<{ delegations: unknown[] }>(`${route}/delegations`)
  ])
  if (selection !== generation || version !== refreshVersion) return
  $conversation.set({
    id: current.id,
    status,
    messages: history.messages,
    events: [...current.events, ...events.events].slice(-2000),
    usage,
    delegations: delegations.delegations
  })
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
  $conversation.set({ id, messages: [], status: null, events: [], usage: null, delegations: [] })
  $error.set('')
  await poll(generation)
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
