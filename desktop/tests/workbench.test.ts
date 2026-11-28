import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { copyFile, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

test('the browser workbench can browse project sessions, rename, pin and delete', {
  skip: process.platform !== 'win32' || !process.env.Harness_PYTHON,
  timeout: 65_000
}, async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'harness-web-workbench-'))
  const source = fileURLToPath(new URL('../src/', import.meta.url))
  const assets = path.join(root, 'assets')
  const workspace = path.join(root, 'project')
  const secondWorkspace = path.join(root, 'second')
  try {
    await Promise.all([assets, workspace, secondWorkspace].map(directory => mkdir(directory)))
    await build({
      entryPoints: [path.join(source, 'main.tsx')], outfile: path.join(assets, 'app.js'),
      platform: 'browser', bundle: true, define: { __APP_VERSION__: '"test"' }
    })
    await copyFile(path.join(source, 'index.html'), path.join(assets, 'index.html'))
    const portProbe = createServer().listen(0, '127.0.0.1')
    await once(portProbe, 'listening')
    const address = portProbe.address()
    assert.ok(address && typeof address !== 'string')
    const port = address.port
    await new Promise<void>(resolve => portProbe.close(() => resolve()))
    const url = `http://127.0.0.1:${port}`
    const config = path.join(root, 'instance.yaml')
    await writeFile(config, JSON.stringify({
      web: { port, public_origin: url },
      workspaces: { project: { path: workspace }, second: { path: secondWorkspace } },
      default_workspace: 'project',
      models: { diagnostic: { adapter: 'diagnostic', model: 'smoke', capabilities: ['text', 'tool_calls'] } },
      default_model: 'diagnostic'
    }))
    const fixture = path.join(root, 'streaming_backend.py')
    await writeFile(fixture, `
import asyncio
from functools import partial

from fastapi.staticfiles import StaticFiles

from deep_harness.cli import main
from deep_harness.runtime.models import DiagnosticModel
from deep_harness.transports import web_launcher

released = asyncio.Event()
create_web_api = web_launcher.create_web_api

class StreamingFixture(DiagnosticModel):
    async def stream(self, prompt, messages, tools):
        yield {"kind": "delta", "text": "Streaming preview"}
        await asyncio.wait_for(released.wait(), 25)
        yield {"kind": "result", "response": {
            "role": "assistant", "content": "Streaming preview complete"
        }}

def create_fixture_api(instance, assets):
    app = create_web_api(instance, assets=None)

    @app.post("/__test_release")
    async def release():
        released.set()
        return {}

    app.mount("/", StaticFiles(directory=assets, html=True))
    return app

web_launcher.InstanceHost = partial(
    web_launcher.InstanceHost, model_factory=lambda _: StreamingFixture()
)
web_launcher.create_web_api = create_fixture_api
main()
`)
    const backend = spawn(process.env.Harness_PYTHON!, [
      fixture, '--state-dir', path.join(root, 'state'), 'web',
      '--instance-config', config, '--assets', assets
    ], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    const closed = once(backend, 'close')
    let serverLog = ''
    backend.stdout.on('data', value => { serverLog += String(value) })
    backend.stderr.on('data', value => { serverLog += String(value) })
    try {
      for (let attempt = 0; ; attempt++) {
        if (await fetch(url + '/health').then(response => response.ok, () => false)) break
        assert.ok(attempt < 100 && backend.exitCode === null, serverLog)
        await new Promise(resolve => setTimeout(resolve, 100))
      }
      const probe = path.join(root, 'browser.cjs')
      await writeFile(probe, `
        const { app, BrowserWindow } = require('electron')
        app.setPath('userData', ${JSON.stringify(path.join(root, 'browser-data'))})
        app.disableHardwareAcceleration()
        app.whenReady().then(async () => {
          const window = new BrowserWindow({ width: 1100, height: 760, show: false })
          try {
            await window.loadURL(${JSON.stringify(url)})
            await window.webContents.executeJavaScript(\`
              (async () => {
                const wait = async (lookup) => {
                  for (let i = 0; i < 150; i++) {
                    const result = lookup()
                    if (result) return result
                    await new Promise(resolve => setTimeout(resolve, 100))
                  }
                  throw Error(document.body.innerText)
                }
                const button = value => [...document.querySelectorAll('button')].find(
                  item => !item.disabled && item.textContent.includes(value))
                if (window.deepHarness) throw Error('Browser unexpectedly has a desktop bridge')
                const newSession = await wait(() => button('新建会话'))
                const projectsHeading = document.querySelector('.project-heading')
                if (!(newSession.compareDocumentPosition(projectsHeading) & Node.DOCUMENT_POSITION_FOLLOWING)) {
                  throw Error('New session must appear above Projects')
                }
                newSession.click()
                ;(await wait(() => document.querySelector('[role=dialog] button.primary:not(:disabled)'))).click()
                const input = await wait(() => {
                  const item = document.querySelector('textarea[aria-label="任务输入"]')
                  return item?.placeholder.includes('Shift') && item
                })
                Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, 'shared workbench smoke')
                input.dispatchEvent(new Event('input', { bubbles: true }))
                const pickers = document.querySelectorAll('.composer-tools input[type=file]')
                if (pickers.length !== 1) throw Error('Expected a single file picker')
                const attachment = document.querySelector('input[aria-label="选择图片或附件"]')
                const transfer = new DataTransfer()
                transfer.items.add(new File(['attachment evidence'], 'evidence.txt', {type: 'text/plain'}))
                attachment.files = transfer.files
                attachment.dispatchEvent(new Event('change', { bubbles: true }))
                ;(await wait(() => button('发送'))).click()
                await wait(() => [...document.querySelectorAll('.message.assistant')].some(item =>
                  item.textContent.includes('生成中') && item.textContent.includes('Streaming preview')))
                if (!(await fetch('/__test_release', { method: 'POST' })).ok) {
                  throw Error('Could not release the streaming fixture')
                }
                await wait(() => [...document.querySelectorAll('.message.assistant')].some(item =>
                  item.textContent.includes('Streaming preview complete')))
                await wait(() => document.querySelector('.message.assistant'))
                await wait(() => document.body.innerText.includes('产物'))
                const listing = await fetch('/api/v1/workspaces/project/sessions').then(response => response.json())
                const sessionPath = '/api/v1/workspaces/project/sessions/' + listing.sessions[0].id
                const pause = await fetch(sessionPath + '/task-control', {
                  method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ action: 'pause' })
                })
                if (!pause.ok) throw Error('Pause request failed')
                ;(await wait(() => button('继续任务'))).click()
                await wait(() => document.querySelector('.composer-submit .send')?.textContent.includes('发送'))
                if (document.querySelector('.chat-header button')?.textContent.includes('暂停')) {
                  throw Error('The pause control remained in the header')
                }
                const search = document.querySelector('input[aria-label="搜索历史"]')
                search.closest('details').open = true
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(search, 'shared workbench smoke')
                search.dispatchEvent(new Event('input', { bubbles: true }))
                ;(await wait(() => button('搜索'))).click()
                await wait(() => search.closest('details').querySelector('article'))
                const more = () => document.querySelector('.session-more:not(:disabled)')
                ;(await wait(more)).click()
                const menu = await wait(() => document.querySelector('[role=menu]'))
                if (document.activeElement?.getAttribute('role') !== 'menuitem') throw Error('Menu focus')
                menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
                await wait(() => !document.querySelector('[role=menu]'))
                ;(await wait(more)).click()
                ;(await wait(() => button('重命名'))).click()
                const title = await wait(() => document.querySelector('.session-dialog input'))
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(title, '共享会话验证')
                title.dispatchEvent(new Event('input', { bubbles: true }))
                ;(await wait(() => button('保存'))).click()
                await wait(() => document.querySelector('.chat-header').textContent.includes('共享会话验证'))
                const project = name => [...document.querySelectorAll('.project-toggle')]
                  .find(item => item.textContent.trim() === name && !item.disabled)
                const firstProject = await wait(() => project('project'))
                const icon = firstProject.querySelector('path').getAttribute('d')
                firstProject.click()
                await wait(() => firstProject.getAttribute('aria-expanded') === 'false' &&
                  document.querySelector('.project-sessions').hidden)
                if (firstProject.querySelector('path').getAttribute('d') === icon) throw Error('Folder icon did not close')
                if (!document.querySelector('.chat-header').textContent.includes('共享会话验证')) {
                  throw Error('Folding closed the active conversation')
                }
                firstProject.click()
                await wait(() => firstProject.getAttribute('aria-expanded') === 'true' &&
                  !document.querySelector('.project-sessions').hidden)
                const group = document.querySelector('.project-group-toggle')
                group.click()
                await wait(() => document.querySelector('#sidebar-projects').hidden)
                group.click()
                await wait(() => !document.querySelector('#sidebar-projects').hidden)
                ;(await wait(() => project('second'))).click()
                await wait(() => project('second')?.getAttribute('aria-expanded') === 'true' &&
                  !document.querySelector('.session-row'))
                ;(await wait(() => project('project'))).click()
                const restored = await wait(() => document.querySelector('.project-sessions .session-open'))
                restored.click()
                ;(await wait(more)).click()
                ;(await wait(() => button('置顶'))).click()
                await wait(() => document.querySelector('.session-row').textContent.includes('已置顶'))
                ;(await wait(more)).click()
                ;(await wait(() => button('彻底删除'))).click()
                ;(await wait(() => document.querySelector('.session-dialog button[type=submit]'))).click()
                await wait(() => !document.querySelector('.session-row') && !document.querySelector('.session-cleanup'))
                if (!document.querySelector('.chat-header').textContent.includes('开始新的工作')) {
                  throw Error('Deleting the selected session did not reset the workbench')
                }
              })()
            \`)
            app.exit(0)
          } catch (error) {
            console.error(error)
            app.exit(1)
          }
        })
      `)
      const electron = createRequire(import.meta.url)('electron') as string
      const browser = spawn(electron, [probe], {
        windowsHide: true, timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe']
      })
      let browserOutput = ''
      browser.stdout.on('data', value => { browserOutput += String(value) })
      browser.stderr.on('data', value => { browserOutput += String(value) })
      const [code] = await once(browser, 'close')
      assert.equal(code, 0, browserOutput + '\n' + serverLog)
    } finally {
      backend.kill()
      await closed
    }
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
