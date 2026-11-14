import type { PermissionMode } from './protocol'

export const permissionOptions = [
  { value: 'ask', label: '请求批准', description: '文件修改、外部文件及互联网操作始终询问', icon: 'hand' },
  { value: 'auto', label: '帮我批准', description: '自动批准普通编辑，仅对检测到的风险操作请求批准', icon: 'shield' },
  { value: 'full', label: '完全访问权限', description: '允许访问互联网和工作区外文件，无需逐项批准', icon: 'full' }
] as const

export function permissionLabel(mode: PermissionMode) {
  return mode === 'legacy' ? '请求批准 · 仅工作区' : permissionOptions.find(row => row.value === mode)!.label
}
