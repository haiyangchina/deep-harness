import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { createInterface } from 'node:readline'
import type { ApiRequest, Settings } from '../src/protocol.ts'
import { apiPath } from './routes.ts'
import { localJson } from './local-api.ts'

export class LocalRuntime {
  private readonly piWorker?: { electron: string; bundle?: string }
  private child: ChildProcessWithoutNullStreams | null = null
  private token = ''
  private base = ''
  private profile = ''
  error = ''

  constructor(piWorker?: { electron: string; bundle?: string }) {
    this.piWorker = piWorker
  }

  get ready() {
    return Boolean(this.child && this.base)
  }

  async start(settings: Settings): Promise<void> {
    if (this.child) throw new Error('后端已运行。修改运行配置前请关闭并重新打开桌面端。')
    this.error = ''
    this.profile = settings.profile
    this.token = randomBytes(32).toString('hex')
    const args = [
      '-u',
      '-m',
      'deep_harness.transports.desktop',
      '--state-dir',
      settings.stateDir,
      '--workspace',
      settings.workspace,
      '--profile',
      settings.profile
    ]
    if (settings.configFile) args.push('--config', settings.configFile)
    if (settings.diagnostic) args.push('--provider', 'diagnostic')
    args.push('serve')
    const env = { ...process.env }
    if (this.piWorker) {
      env.DEEP_HARNESS_PI_ELECTRON = this.piWorker.electron
      if (this.piWorker.bundle) env.DEEP_HARNESS_PI_BUNDLE = this.piWorker.bundle
    }
    const child = spawn(settings.python, args, { windowsHide: true, stdio: 'pipe', env })
    this.child = child
    child.stdin.on('error', () => {
      /* Exit is reported by the process owner below. */
    })
    let diagnostics = ''
    child.stderr.on('data', chunk => {
      diagnostics = (diagnostics + chunk.toString()).slice(-8000)
    })
    child.on('exit', () => {
      if (this.child !== child) return
      this.base = ''
      this.child = null
      this.error = diagnostics || '本机后端已停止，请检查环境后重新启动。'
    })
    try {
      const endpoint = await this.endpoint(child, () => diagnostics)
      const health = await localJson(`${endpoint}/health`, 'GET', this.token, undefined, 5000)
      if (!health || typeof health !== 'object' || !('status' in health) || health.status !== 'ok')
        throw new Error('本机后端健康检查未通过，未建立连接。')
      if (this.child !== child || child.exitCode !== null)
        throw new Error('本机后端在连接验证期间退出，请检查运行环境。')
      this.base = endpoint
    } catch (error) {
      await this.stop()
      this.error = error instanceof Error ? error.message : '本机后端连接验证失败。'
      throw error
    }
  }

  private endpoint(child: ChildProcessWithoutNullStreams, diagnostics: () => string): Promise<string> {
    return new Promise<string>((resolve, reject) => {
      const lines = createInterface({ input: child.stdout })
      const timer = setTimeout(() => {
        child.stdin.end('\n')
        reject(new Error('后端启动超过 60 秒。请检查 Python 环境与 profile owner。'))
      }, 60_000)
      const fail = (error: Error) => {
        clearTimeout(timer)
        lines.close()
        reject(error)
      }
      child.once('error', error => {
        this.child = null
        fail(error)
      })
      child.once('exit', () => fail(new Error(diagnostics() || '后端启动失败')))
      lines.on('line', line => {
        if (!line.startsWith('{')) return
        try {
          const ready = JSON.parse(line) as { port?: number }
          if (!Number.isInteger(ready.port) || ready.port! < 1 || ready.port! > 65535) return
          clearTimeout(timer)
          lines.close()
          resolve(`http://127.0.0.1:${ready.port}`)
        } catch {
          /* Other stdout is never interpreted as a capability. */
        }
      })
      child.stdin.write(JSON.stringify({ token: this.token }) + '\n')
    })
  }

  async request(request: ApiRequest): Promise<unknown> {
    if (!this.ready) throw new Error(this.error || '本机后端尚未就绪')
    const path = apiPath(this.profile, request)
    if (request.upload && (
      request.method !== 'POST' || !/^\/sessions\/[a-z0-9-]+\/images$/.test(request.path)
      || !(request.upload.data instanceof ArrayBuffer)
      || request.upload.data.byteLength > 8 * 1024 * 1024
      || typeof request.upload.name !== 'string' || request.upload.name.length > 255
    )) throw new Error('无效的图片上传请求')
    return localJson(this.base + path, request.method, this.token, request.body, undefined, request.upload)
  }

  async stop(): Promise<void> {
    const child = this.child
    if (!child) return
    await new Promise<void>(resolve => {
      const timer = setTimeout(() => {
        child.kill()
        resolve()
      }, 12_000)
      child.once('exit', () => {
        clearTimeout(timer)
        resolve()
      })
      child.stdin.end('\n')
    })
  }
}
