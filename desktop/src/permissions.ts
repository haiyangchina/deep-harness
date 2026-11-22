import type { PermissionMode } from './protocol'

export const permissionOptions = [
  { value: 'ask', label: '请求批准', description: '文件操作、终端命令与访问互联网每次逐项询问', icon: 'hand' },
  { value: 'auto', label: '帮我批准', description: '同一轮任务内同类操作只问一次；工作区外与互联网仍逐次确认', icon: 'shield' },
  { value: 'full', label: '完全访问权限', description: '三类能力全部免批，启用前需确认风险', icon: 'full' }
] as const

export function permissionLabel(mode: PermissionMode) {
  return permissionOptions.find(row => row.value === mode)!.label
}
