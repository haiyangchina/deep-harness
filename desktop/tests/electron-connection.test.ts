import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

test('native Electron can read the Python loopback API repeatedly', {
  skip: process.platform !== 'win32' || !process.env.Harness_PYTHON,
  timeout: 60_000
}, async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'harness-electron-connection-'))
  try {
    const entry = path.join(directory, 'probe.cjs')
    await build({
      entryPoints: [fileURLToPath(new URL('./fixtures/electron-connection.ts', import.meta.url))],
      outfile: entry, bundle: true, platform: 'node', format: 'cjs', external: ['electron']
    })
    const electron = createRequire(import.meta.url)('electron') as string
    const child = spawn(electron, [entry, directory, process.env.Harness_PYTHON!], {
      windowsHide: true, timeout: 40_000,
      env: { ...process.env, ELECTRON_RUN_AS_NODE: undefined },
      stdio: ['ignore', 'pipe', 'pipe']
    })
    let output = ''
    child.stdout.on('data', chunk => { output += chunk.toString() })
    child.stderr.on('data', chunk => { output += chunk.toString() })
    const [code] = await once(child, 'close')
    assert.equal(code, 0, output)
    assert.match(output, /ELECTRON_LOOPBACK_OK: 50 concurrent-group reads completed/)
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})
