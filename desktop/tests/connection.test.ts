import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { LocalRuntime } from '../electron/runtime.ts'
import type { Settings } from '../src/protocol.ts'

async function isolatedRuntime(work: (runtime: LocalRuntime, settings: Settings) => Promise<void>) {
  const workspace = await mkdtemp(path.join(tmpdir(), 'deep-harness-connection-'))
  const runtime = new LocalRuntime()
  const settings: Settings = {
    python: process.env.Harness_PYTHON!, workspace, stateDir: path.join(workspace, 'state'),
    profile: 'connection-test', configFile: '', diagnostic: true
  }
  try {
    await work(runtime, settings)
  } finally {
    await runtime.stop()
    await rm(workspace, { recursive: true, force: true })
  }
}

test('startup does not report connected when the loopback health request fails', {
  skip: !process.env.Harness_PYTHON,
  timeout: 30_000
}, async t => {
  const failure = Object.assign(new TypeError('private provider detail'), {
    cause: { code: 'ECONNREFUSED' }
  })
  t.mock.method(globalThis, 'fetch', async () => { throw failure })
  await isolatedRuntime(async (runtime, settings) => {
    await assert.rejects(runtime.start(settings), error => {
      assert.ok(error instanceof Error)
      assert.match(error.message, /ECONNREFUSED/)
      assert.doesNotMatch(error.message, /private provider detail/)
      return true
    })
    assert.equal(runtime.ready, false)
  })
})

test('connection errors identify the local endpoint without retrying uncertain writes', {
  skip: !process.env.Harness_PYTHON,
  timeout: 30_000
}, async t => {
  await isolatedRuntime(async (runtime, settings) => {
    await runtime.start(settings)
    let calls = 0
    t.mock.method(globalThis, 'fetch', async () => {
      calls++
      throw Object.assign(new TypeError('Authorization: private-token'), {
        cause: { code: 'ECONNRESET' }
      })
    })
    await assert.rejects(runtime.request({ method: 'GET', path: '/sessions?after=private-query' }), error => {
      assert.ok(error instanceof Error)
      assert.match(error.message, /GET .*127\.0\.0\.1:\d+\/profiles\/connection-test\/sessions/)
      assert.match(error.message, /ECONNRESET/)
      assert.doesNotMatch(error.message, /private-token|private-query/)
      return true
    })
    for (const method of ['POST', 'DELETE'] as const) {
      await assert.rejects(runtime.request({ method, path: '/sessions', body: { private: 'body' } }), error => {
        assert.ok(error instanceof Error)
        assert.match(error.message, /结果未确认/)
        assert.match(error.message, /ECONNRESET/)
        assert.doesNotMatch(error.message, /private-token|body/)
        return true
      })
    }
    assert.equal(calls, 3)
  })
})
