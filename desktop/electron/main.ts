import { app, BrowserWindow, dialog, ipcMain } from 'electron'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import type { Settings } from '../src/protocol'
import { LocalRuntime } from './runtime'
import { validateSettings } from './settings'

const runtime = new LocalRuntime()
let settings: Settings
let closing = false
const page = pathToFileURL(path.join(__dirname, 'index.html')).href
const dataOption = process.argv.indexOf('--desktop-data')
if (dataOption >= 0) {
  const directory = process.argv[dataOption + 1]
  if (!directory || !path.isAbsolute(directory)) throw new Error('--desktop-data requires an absolute path')
  app.setPath('userData', directory)
}

async function boot() {
  const configPath = path.join(app.getPath('userData'), 'settings.json')
  const conda = process.env.CONDA_PREFIX
  settings = {
    python: conda && path.basename(conda) === 'deep-harness' ? path.join(conda, 'python.exe') : '',
    workspace: app.isPackaged ? '' : path.resolve(__dirname, '../..'),
    stateDir: path.join(app.getPath('userData'), 'agent-state'),
    profile: 'default',
    configFile: '',
    diagnostic: false
  }
  try {
    settings = { ...settings, ...JSON.parse(await readFile(configPath, 'utf8')) }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') runtime.error = '桌面配置读取失败，请重新选择运行环境。'
  }
  const window = new BrowserWindow({
    title: 'deep-harness',
    width: 1440,
    height: 920,
    minWidth: 960,
    minHeight: 680,
    backgroundColor: '#101317',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', (event, url) => {
    if (url !== page) event.preventDefault()
  })
  const info = () => ({ ready: runtime.ready, settings, error: runtime.error })
  const handlers: Record<string, (value: any) => unknown> = {
    info,
    request: value => runtime.request(value),
    stop: async () => {
      const result = await dialog.showMessageBox(window, {
        type: 'question',
        buttons: ['保留运行', '停止并修改配置'],
        defaultId: 0,
        cancelId: 0,
        message: '停止本机 Agent 后端？',
        detail: '运行中的任务会排空或取消；不确定外部操作仍需核对。再次启动不会自动重做未知操作。'
      })
      if (result.response === 1) await runtime.stop()
      return info()
    },
    choose: async value => {
      if (!['python', 'workspace', 'stateDir', 'configFile'].includes(value)) throw new Error('Invalid picker')
      const result = await dialog.showOpenDialog(window, {
        properties: value === 'workspace' || value === 'stateDir' ? ['openDirectory', 'createDirectory'] : ['openFile']
      })
      return result.canceled ? null : result.filePaths[0]
    },
    start: async value => {
      const candidate = validateSettings(value)
      await runtime.start(candidate)
      settings = candidate
      await mkdir(path.dirname(configPath), { recursive: true })
      await writeFile(configPath, JSON.stringify(settings, null, 2), 'utf8')
      return info()
    }
  }
  for (const [name, handle] of Object.entries(handlers)) {
    ipcMain.handle(`deep-harness:${name}`, (event, value) => {
      if (event.sender !== window.webContents || event.senderFrame?.url !== page)
        throw new Error('Untrusted desktop caller')
      return handle(value)
    })
  }
  if (settings.python && settings.workspace) {
    try {
      await runtime.start(settings)
    } catch (error) {
      runtime.error = (error as Error).message
    }
  }
  await window.loadFile(path.join(__dirname, 'index.html'))
  console.log(`[deep-harness] Desktop ready; local backend ${runtime.ready ? 'connected' : 'awaiting configuration'}`)
}

if (!app.requestSingleInstanceLock()) app.quit()
else {
  app.on('second-instance', () => {
    const window = BrowserWindow.getAllWindows()[0]
    window?.restore()
    window?.focus()
  })
  app
    .whenReady()
    .then(boot)
    .catch(error => {
      dialog.showErrorBox('deep-harness 启动失败', String(error))
      app.quit()
    })
  app.on('window-all-closed', () => app.quit())
  app.on('before-quit', event => {
    if (closing) return
    event.preventDefault()
    closing = true
    void runtime.stop().finally(() => app.quit())
  })
}
