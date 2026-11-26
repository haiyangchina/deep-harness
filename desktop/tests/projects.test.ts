import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

test('desktop adds a folder and switches between project sessions without losing either', {
  skip: process.platform !== 'win32' || !process.env.Harness_PYTHON,
  timeout: 65_000
}, async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'harness-desktop-projects-'))
  const source = fileURLToPath(new URL('../electron/', import.meta.url))
  const data = path.join(directory, 'user-data')
  const original = path.join(directory, 'original')
  const added = path.join(directory, 'my project')
  try {
    await Promise.all([data, original, added].map(dir => mkdir(dir)))
    await Promise.all(['main', 'preload'].map(name => build({
      entryPoints: [path.join(source, name + '.ts')],
      outfile: path.join(directory, name + '.cjs'),
      platform: 'node', bundle: true, external: ['electron']
    })))
    await writeFile(path.join(data, 'settings.json'), JSON.stringify({
      python: process.env.Harness_PYTHON, workspace: original, stateDir: path.join(directory, 'state'),
      profile: 'default', configFile: '', diagnostic: true
    }))
    await writeFile(path.join(directory, 'index.html'), '<!DOCTYPE html><title>Project integration</title>')
    const entry = path.join(directory, 'probe.cjs')
    await writeFile(entry, `
      const { app, dialog } = require('electron')
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [${JSON.stringify(added)}] })
      app.on('browser-window-created', (_, window) => {
        window.webContents.once('did-finish-load', async () => {
          try {
            await window.webContents.executeJavaScript(\`
              (async () => {
                const api = window.deepHarness
                const request = (path, method = 'GET', body) => api.request({ path, method, body })
                const first = await api.info()
                const original = await request('/sessions', 'POST', { request_id: 'first' })
                const second = await api.openProject(null)
                if (!second.ready || second.projectId === first.projectId ||
                    second.settings.profile === first.settings.profile) throw Error('Project did not switch')
                const empty = await request('/sessions')
                if (empty.sessions.length) throw Error('Sessions leaked across projects')
                await request('/sessions', 'POST', { request_id: 'second' })
                await api.openProject(first.projectId)
                const restored = await request('/sessions')
                if (restored.sessions[0]?.id !== original.id) throw Error('Original sessions not restored')
              })()
            \`)
            console.log('DESKTOP_PROJECTS_OK')
            app.quit()
          } catch (error) {
            console.error(error)
            app.exit(1)
          }
        })
      })
      require('./main.cjs')
    `)
    const electron = createRequire(import.meta.url)('electron') as string
    const child = spawn(electron, [entry, '--desktop-data', data], {
      windowsHide: true, timeout: 50_000,
      env: { ...process.env, ELECTRON_RUN_AS_NODE: undefined },
      stdio: ['ignore', 'pipe', 'pipe']
    })
    let output = ''
    child.stdout.on('data', chunk => { output += chunk.toString() })
    child.stderr.on('data', chunk => { output += chunk.toString() })
    const [code] = await once(child, 'close')
    assert.equal(code, 0, output)
    assert.match(output, /DESKTOP_PROJECTS_OK/)
    const saved = JSON.parse(await readFile(path.join(data, 'settings.json'), 'utf8'))
    assert.equal(saved.projects.length, 2)
    assert.equal(saved.workspace, original)
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})
