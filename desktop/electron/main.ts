import { app, BrowserWindow, dialog, ipcMain } from 'electron'
import { readFile, writeFile, mkdir, realpath, rename, rm } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import type { DesktopProject, Settings } from '../src/protocol'
import { LocalRuntime } from './runtime'
import { validateSettings } from './settings'

const newRuntime = () => new LocalRuntime({
  electron: process.execPath,
  bundle: app.isPackaged ? path.join(process.resourcesPath, 'pi-search', 'search.cjs') : undefined
})
let runtime = newRuntime()
const runtimes = new Map<string, LocalRuntime>()
const runtimeKey = (value: Settings) => JSON.stringify([value.stateDir, value.profile, value.workspace])
let projects: DesktopProject[] = []
let projectId = ''
let changing = false
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
    const saved = JSON.parse(await readFile(configPath, 'utf8'))
    const { projects: savedProjects, projectId: savedId, ...savedSettings } = saved
    settings = { ...settings, ...savedSettings }
    if (Array.isArray(savedProjects)) projects = savedProjects
    projectId = typeof savedId === 'string' ? savedId : ''
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') runtime.error = '桌面配置读取失败，请重新选择运行环境。'
  }
  function remember(value: Settings) {
    if (!value.workspace) return
    const current = projects.find(row => row.workspace === value.workspace && row.profile === value.profile)
    const project = current ?? {
      id: randomUUID(), name: path.basename(value.workspace) || value.workspace,
      workspace: value.workspace, profile: value.profile
    }
    if (!current) projects = [...projects, project]
    projectId = project.id
  }
  remember(settings)
  runtimes.set(runtimeKey(settings), runtime)
  async function persist(value: Settings) {
    await mkdir(path.dirname(configPath), { recursive: true })
    const temporary = configPath + '.' + randomUUID()
    try {
      await writeFile(temporary, JSON.stringify({ ...value, projects, projectId }, null, 2), 'utf8')
      await rename(temporary, configPath)
    } finally {
      await rm(temporary, { force: true })
    }
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
  const info = () => ({ ready: runtime.ready, settings, error: runtime.error, projects, projectId })
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
        detail: '所有已打开项目的运行任务会排空或取消；结果不确定的操作仍需核对。'
      })
      if (result.response === 1) await Promise.all([...runtimes.values()].map(item => item.stop()))
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
      for (const [key, item] of runtimes) if (item === runtime) runtimes.delete(key)
      runtimes.set(runtimeKey(settings), runtime)
      remember(settings)
      await persist(settings)
      return info()
    },
    project: async id => {
      if (changing) throw new Error('正在打开项目。')
      changing = true
      try {
        return await openProject(id)
      } finally {
        changing = false
      }
    }
  }
  async function openProject(id: unknown) {
    let project = projects.find(row => row.id === id)
    if (id === null) {
      const chosen = await dialog.showOpenDialog(window, {
        title: '选择项目目录', properties: ['openDirectory'], defaultPath: settings.workspace || undefined
      })
      if (chosen.canceled) return info()
      const workspace = await realpath(chosen.filePaths[0])
      project = projects.find(row => row.workspace === workspace)
      if (!project) {
        const projectKey = randomUUID()
        project = { id: projectKey, workspace, name: path.basename(workspace) || workspace,
          profile: 'project-' + projectKey }
      }
    }
    if (!project) throw new Error('找不到所选项目。')
    const candidate = { ...settings, workspace: project.workspace, profile: project.profile }
    const key = runtimeKey(candidate)
    const next = runtimes.get(key) ?? newRuntime()
    const started = Boolean(candidate.python && !next.ready && (runtime.ready || settings.python))
    if (started) await next.start(validateSettings(candidate))
    const previousProjects = projects
    const previousId = projectId
    if (!projects.some(row => row.id === project.id)) projects = [...projects, project]
    projectId = project.id
    try {
      await persist(candidate)
    } catch (error) {
      projects = previousProjects
      projectId = previousId
      if (started) await next.stop()
      throw error
    }
    runtimes.set(key, next)
    runtime = next
    settings = candidate
    return info()
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
    void Promise.allSettled([...runtimes.values()].map(item => item.stop())).finally(() => app.quit())
  })
}
