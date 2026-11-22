import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { apiPath } from '../electron/routes.ts'
import { LocalRuntime } from '../electron/runtime.ts'

test('native API routing stays inside one profile and rejects traversal and external URLs', () => {
  assert.equal(
    apiPath('work', { method: 'GET', path: '/sessions?limit=100&after=' }),
    '/profiles/work/sessions?limit=100&after='
  )
  assert.equal(
    apiPath('work', { method: 'GET', path: '/capabilities' }),
    '/profiles/work/capabilities'
  )
  assert.equal(
    apiPath('work', { method: 'POST', path: '/sessions/abc/revoke-reuse' }),
    '/profiles/work/sessions/abc/revoke-reuse'
  )
  for (const value of [
    'https://example.com',
    '//example.com',
    '/sessions/../health',
    '/sessions/%2e%2e/health',
    '/sessions/%5cfoo',
    '/sessions#foo'
  ]) {
    assert.throws(() => apiPath('work', { method: 'GET', path: value }))
  }
})

test(
  'desktop starts the real diagnostic backend without sign-in, approves once and resumes history',
  {
    skip: !process.env.Harness_PYTHON,
    timeout: 60_000
  },
  async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), 'deep-harness-desktop-'))
    const runtime = new LocalRuntime()
    const settings = {
      python: process.env.Harness_PYTHON!,
      workspace,
      stateDir: path.join(workspace, '.state'),
      profile: 'desktop-test',
      configFile: '',
      diagnostic: true
    }
    const request = <T = any>(url: string, method: 'GET' | 'POST' = 'GET', body?: unknown) =>
      runtime.request({ method, path: url, body }) as Promise<T>
    try {
      await writeFile(path.join(workspace, 'README.md'), 'Desktop integration evidence', 'utf8')
      await runtime.start(settings)
      const session = await request('/sessions', 'POST', { request_id: 'create', tools: ['read', 'write'] })
      const route = `/sessions/${session.id}`
      const receipt = await request(`${route}/inputs`, 'POST', {
        submission_id: 'write-one',
        text: '/write result.txt hello-from-desktop'
      })
      assert.equal(
        (
          await request(`${route}/inputs`, 'POST', {
            submission_id: 'write-one',
            text: '/write result.txt hello-from-desktop'
          })
        ).id,
        receipt.id
      )
      const waitStatus = async (status: string) => {
        const deadline = Date.now() + 15_000
        while (Date.now() < deadline) {
          const state = await request(`${route}/status`)
          if (state.latest_admission?.status === status) return state
          // Only read-side observation waits; no model or effect is retried.
          await new Promise(resolve => setTimeout(resolve, 50))
        }
        throw new Error(`Status did not become ${status}`)
      }
      const pending = await waitStatus('waiting_approval')
      await assert.rejects(readFile(path.join(workspace, 'result.txt')))
      const approval = pending.approvals[0]
      assert.ok(approval.proposal)
      await request(`${route}/approvals`, 'POST', {
        interaction_id: approval.id,
        generation: approval.generation,
        decision: 'approve'
      })
      await waitStatus('completed')
      assert.equal(await readFile(path.join(workspace, 'result.txt'), 'utf8'), 'hello-from-desktop')
      assert.ok((await request(`${route}/events`)).events.length > 0)
      await runtime.stop()
      await runtime.start(settings)
      assert.equal((await request('/sessions')).sessions[0].id, session.id)
      assert.ok((await request(route)).messages.length >= 2)
    } finally {
      await runtime.stop()
      await rm(workspace, { recursive: true, force: true })
    }
  }
)

test('permission modes control the real desktop backend and persist after restart', {
  skip: !process.env.Harness_PYTHON,
  timeout: 60_000
}, async () => {
  const temporary = await mkdtemp(path.join(tmpdir(), 'deep-harness-permissions-'))
  const workspace = path.join(temporary, 'workspace')
  const { mkdir } = await import('node:fs/promises')
  await mkdir(workspace)
  const outside = path.join(temporary, 'outside.txt')
  await writeFile(outside, 'outside evidence', 'utf8')
  const runtime = new LocalRuntime()
  const settings = {
    python: process.env.Harness_PYTHON!, workspace, stateDir: path.join(temporary, 'state'),
    profile: 'permissions-test', configFile: '', diagnostic: true
  }
  const request = <T = any>(url: string, method: 'GET' | 'POST' = 'GET', body?: unknown) =>
    runtime.request({ method, path: url, body }) as Promise<T>
  try {
    await runtime.start(settings)
    const session = await request('/sessions', 'POST', {
      request_id: 'create', tools: ['read', 'write'], permission_mode: 'auto'
    })
    const route = `/sessions/${session.id}`
    const wait = async (expected: string) => {
      const deadline = Date.now() + 15_000
      while (Date.now() < deadline) {
        const status = await request(`${route}/status`)
        if (status.latest_admission?.status === expected) return status
        await new Promise(resolve => setTimeout(resolve, 50))
      }
      throw new Error(`Did not reach ${expected}`)
    }
    await request(`${route}/inputs`, 'POST', {
      submission_id: 'auto-write',
      text: '/write-batch\n/write nested/auto.txt first\n/write nested/reused.txt second'
    })
    const first = (await wait('waiting_approval')).approvals[0]
    await assert.rejects(readFile(path.join(workspace, 'nested/auto.txt')))
    await request(`${route}/approvals`, 'POST', {
      interaction_id: first.id, generation: first.generation, decision: 'approve'
    })
    assert.deepEqual((await wait('completed')).approvals, [])
    assert.equal(await readFile(path.join(workspace, 'nested/auto.txt'), 'utf8'), 'first')
    assert.equal(await readFile(path.join(workspace, 'nested/reused.txt'), 'utf8'), 'second')
    await request(`${route}/inputs`, 'POST', {
      submission_id: 'new-round', text: '/write repeated.txt still-needs-approval'
    })
    const second = (await wait('waiting_approval')).approvals[0]
    await request(`${route}/approvals`, 'POST', {
      interaction_id: second.id, generation: second.generation, decision: 'deny'
    })
    await wait('completed')
    await assert.rejects(readFile(path.join(workspace, 'repeated.txt')))
    await request(`${route}/permissions`, 'POST', {permission_mode: 'ask', expected_revision: 0})
    await request(`${route}/inputs`, 'POST', {submission_id: 'ask-write', text: '/write denied.txt deny-me'})
    const pending = await wait('waiting_approval')
    await assert.rejects(request(`${route}/permissions`, 'POST', {permission_mode: 'full', expected_revision: 1}))
    await assert.rejects(readFile(path.join(workspace, 'denied.txt')))
    const approval = pending.approvals[0]
    await request(`${route}/approvals`, 'POST', {
      interaction_id: approval.id, generation: approval.generation, decision: 'deny'
    })
    await wait('completed')
    await assert.rejects(request(`${route}/permissions`, 'POST', {
      permission_mode: 'full', expected_revision: 1
    }))
    await request(`${route}/permissions`, 'POST', {
      permission_mode: 'full', expected_revision: 1, risk_acknowledged: true
    })
    await request(`${route}/inputs`, 'POST', {submission_id: 'external-read', text: `/read ${outside}`})
    const full = await wait('completed')
    assert.ok(full.latest_admission.result.includes('outside evidence'))
    assert.equal(full.session.permission_mode, 'full')
    await runtime.stop()
    await runtime.start(settings)
    assert.equal((await request(`${route}/status`)).session.permission_mode, 'full')
    await assert.rejects(readFile(path.join(workspace, 'denied.txt')))
  } finally {
    await runtime.stop()
    await rm(temporary, {recursive: true, force: true})
  }
})
