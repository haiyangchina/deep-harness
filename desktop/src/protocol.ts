export interface Settings {
  python: string
  workspace: string
  stateDir: string
  profile: string
  configFile: string
  diagnostic: boolean
}
export interface RuntimeInfo {
  ready: boolean
  settings: Settings
  error?: string
}
export interface ApiRequest {
  method: 'GET' | 'POST' | 'DELETE'
  path: string
  body?: unknown
}
export interface DesktopBridge {
  info(): Promise<RuntimeInfo>
  start(settings: Settings): Promise<RuntimeInfo>
  stop(): Promise<RuntimeInfo>
  choose(kind: 'python' | 'workspace' | 'stateDir' | 'configFile'): Promise<string | null>
  request<T = unknown>(request: ApiRequest): Promise<T>
}
export interface Session {
  id: string
  engine: string
  harness: string
  tools: string[]
  permission_mode?: PermissionMode
  permission_revision?: number
}
export type PermissionMode = 'legacy' | 'ask' | 'auto' | 'full'
export interface CapabilitySpec {
  key: string
  label: string
  description: string
  tier: 'core' | 'extension'
  risk: string
  tools: string[]
  approval_hint: string
}
export interface AccessScope {
  key: string
  label: string
  description: string
  scope_keys: string[]
  approval_hint: string
}
export interface PermissionModeSpec {
  key: string
  label: string
  description: string
  approval: string
  requires_acknowledgement: boolean
}
export interface PermissionPolicy {
  modes: PermissionModeSpec[]
  access_scopes: AccessScope[]
  scope_keys: string[]
  matrix: Record<string, Record<string, string>>
  reusable_scopes: string[]
  always_per_call: string[]
  read_only_scopes: string[]
  hard_boundaries: string[]
  requires_acknowledgement: string[]
}
export interface CapabilityManifest {
  capability_version: number
  capabilities: CapabilitySpec[]
  core_tools: string[]
  read_only_core: string[]
  legacy_tools: string[]
  extensions: string[]
  access_scopes?: AccessScope[]
  permission_modes?: PermissionModeSpec[]
  permission_policy?: PermissionPolicy
  hard_boundaries?: string[]
}
export interface Admission {
  id: string
  text: string
  status: string
  generation: number
  result?: string
}
export interface Approval {
  id: string
  generation: number
  proposal: unknown
}
export interface SessionStatus {
  session: Session
  latest_admission: Admission | null
  active_admissions: Admission[]
  approvals: Approval[]
  runtime: { compatible: boolean; current_fingerprint: string }
  active_effects: unknown[]
}
export interface JournalEvent {
  seq: number
  type: string
  payload: Record<string, unknown>
}
export interface Message {
  id: string
  role: string
  content: unknown
}
declare global {
  interface Window {
    deepHarness: DesktopBridge
  }
}
