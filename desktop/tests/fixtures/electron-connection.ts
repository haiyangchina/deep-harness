import { app } from 'electron'
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { LocalRuntime } from '../../electron/runtime.ts'

const [directory, python] = process.argv.slice(2)
const userData = path.join(directory, 'electron-data')
mkdirSync(userData, { recursive: true })
app.setPath('userData', userData)
app.disableHardwareAcceleration()

void app.whenReady().then(async () => {
  const runtime = new LocalRuntime()
  let code = 0
  try {
    await runtime.start({
      python, workspace: directory, stateDir: path.join(directory, 'agent-state'),
      profile: 'electron-connection', configFile: '', diagnostic: true
    })
    const base = '/api/v1/workspaces/electron-connection'
    const created = await runtime.request({
      method: 'POST', path: base + '/sessions', body: { request_id: 'probe' }
    }) as { status: number; body: { id: string } }
    if (created.status !== 200) throw Error(JSON.stringify(created))
    const session = created.body
    const paths = ['/sessions', '/skills', '/memories',
      `/sessions/${session.id}`, `/sessions/${session.id}/event-history?after=0`]
    for (let index = 0; index < 10; index++) {
      await Promise.all(paths.map(async endpoint => {
        const result = await runtime.request({ method: 'GET', path: base + endpoint }) as { status: number }
        if (result.status !== 200) throw Error(JSON.stringify(result))
      }))
    }
    console.log('ELECTRON_LOOPBACK_OK: 50 concurrent-group reads completed')
  } catch (error) {
    code = 1
    console.error(error)
  } finally {
    await runtime.stop()
    app.exit(code)
  }
})
