import path from 'node:path'
import type { Settings } from '../src/protocol'

export function validateSettings(value: unknown): Settings {
  const candidate = value as Settings | null
  if (
    !candidate ||
    !['python', 'workspace', 'stateDir', 'configFile', 'profile'].every(
      key => typeof candidate[key as keyof Settings] === 'string'
    ) ||
    typeof candidate.diagnostic !== 'boolean'
  )
    throw new Error('运行配置格式不正确。')
  if (
    ![candidate.python, candidate.workspace, candidate.stateDir].every(value => path.isAbsolute(value)) ||
    (candidate.configFile && !path.isAbsolute(candidate.configFile)) ||
    !/^[A-Za-z0-9_-]{1,64}$/.test(candidate.profile)
  ) {
    throw new Error('请选择绝对路径，profile 只允许字母、数字、下划线与短横线。')
  }
  return candidate
}
