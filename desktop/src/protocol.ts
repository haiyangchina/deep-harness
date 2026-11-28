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
  projects?: DesktopProject[]
  projectId?: string
  catalog?: ServerCatalog
}
export interface ServerWorkspace { id: string; label?: string; mode: string; manage_knowledge?: boolean }
export interface ServerCatalog {
  default_workspace: string
  default_model: string
  workspaces: ServerWorkspace[]
  models: { id: string; label: string; configured: boolean }[]
  agents: { id: string; label: string; harness: string; workflow: string | null }[]
  tools: string[]
  local_projects: boolean
  project_sandbox: boolean
}
export interface DesktopProject { id: string; name: string; workspace: string; profile: string }
export interface ApiRequest {
  method: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  path: string
  body?: unknown
  upload?: { name: string; data: ArrayBuffer }
  response?: 'binary'
}
export interface DesktopBridge {
  info(): Promise<RuntimeInfo>
  start(settings: Settings): Promise<RuntimeInfo>
  stop(): Promise<RuntimeInfo>
  openProject(id: string | null): Promise<RuntimeInfo>
  choose(kind: 'python' | 'workspace' | 'stateDir' | 'configFile'): Promise<string | null>
  request<T = unknown>(request: ApiRequest): Promise<T>
  openLink(href: string): Promise<void>
}
export interface Session {
  id: string
  title?: string | null
  pinned?: boolean
  pinned_at?: string | null
  metadata_revision?: number
  engine: string
  harness: string
  tools: string[]
  permission_mode: PermissionMode
  permission_revision?: number
}
export type PermissionMode = 'ask' | 'auto' | 'full'
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
  admission_id: string
  proposal_hash: string
  generation: number
  proposal: unknown
}
export interface RunEffect { id: string; admission_id: string; proposal_hash: string; status: string }
export interface SessionStatus {
  job_id?: string
  session: Session
  latest_admission: Admission | null
  image_context?: string[] | null
  vision?: boolean
  model_id?: string
  managed_task?: boolean
  pause_supported?: boolean
  pause_requested?: boolean
  active_admissions: Admission[]
  approvals: Approval[]
  runtime?: { compatible: boolean; current_fingerprint: string; changed_fields?: string[] }
  active_effects: RunEffect[]
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
  image_ids?: string[]
}
declare global {
  interface Window {
    deepHarness: DesktopBridge
  }
}
