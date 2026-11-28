import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { apiPath } from '../electron/routes.ts'
import { LocalRuntime } from '../electron/runtime.ts'

test('native API routing stays inside one profile and rejects traversal and external URLs', () => {
  const root = '/api/v1/workspaces/work'
  assert.equal(
    apiPath('work', { method: 'GET', path: root + '/sessions?limit=100&after=' }),
    root + '/sessions?limit=100&after='
  )
  assert.equal(
    apiPath('work', { method: 'GET', path: root + '/capabilities' }),
    root + '/capabilities'
  )
  assert.equal(
    apiPath('work', { method: 'POST', path: root + '/sessions/abc/revoke-reuse' }),
    root + '/sessions/abc/revoke-reuse'
  )
  const approval = '/api/v1/workspaces/work/runs/abc/approvals/abc%3Acall_1/decision'
  assert.equal(apiPath('work', { method: 'POST', path: approval }), approval)
  for (const value of [
    'https://example.com',
    '//example.com',
    '/sessions/../health',
    '/sessions/%2e%2e/health',
    '/sessions/%5cfoo',
    '/sessions#foo',
    '/api/v1/workspaces/other/sessions',
    '/api/v1/workspaces/work/sessions/abc/%2e%2e/sessions'
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
    const raw = <T = any>(url: string, method: 'GET' | 'POST' | 'PATCH' | 'DELETE' = 'GET', body?: unknown) =>
      runtime.request({ method, path: '/api/v1/workspaces/desktop-test' + url, body }) as
        Promise<{ status: number; body: T }>
    const request = async <T = any>(
      url: string, method: 'GET' | 'POST' | 'PATCH' | 'DELETE' = 'GET', body?: unknown
    ) => {
      const response = await raw<T>(url, method, body)
      assert.ok(response.status >= 200 && response.status < 300, JSON.stringify(response))
      return response.body
    }
    try {
      await writeFile(path.join(workspace, 'README.md'), 'Desktop integration evidence', 'utf8')
      await runtime.start(settings)
      const session = await request('/sessions', 'POST', { request_id: 'create', tools: ['read', 'write'] })
      const route = `/sessions/${session.id}`
      const receipt = await request(`${route}/runs`, 'POST', {
        submission_id: 'write-one',
        text: '/write result.txt hello-from-desktop'
      })
      assert.equal(
        (
          await request(`${route}/runs`, 'POST', {
            submission_id: 'write-one',
            text: '/write result.txt hello-from-desktop'
          })
        ).run_id,
        receipt.run_id
      )
      const waitStatus = async (status: string) => {
        const deadline = Date.now() + 15_000
        while (Date.now() < deadline) {
          const state = await request(route)
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
      const decisionPath = `/runs/${receipt.run_id}/approvals/${encodeURIComponent(approval.id)}/decision`
      const decision = {
        proposal_hash: approval.proposal_hash,
        generation: approval.generation,
        decision: 'approve'
      }
      assert.equal((await raw(decisionPath, 'POST', { ...decision, proposal_hash: '0'.repeat(64) })).status, 409)
      await request(decisionPath, 'POST', decision)
      await waitStatus('completed')
      assert.equal(await readFile(path.join(workspace, 'result.txt'), 'utf8'), 'hello-from-desktop')
      assert.ok((await request(`${route}/event-history`)).events.length > 0)
      let artifact
      for (let attempt = 0; attempt < 20 && !artifact; attempt++) {
        artifact = (await request(`/runs/${receipt.run_id}/artifacts`))[0]
        if (!artifact) await new Promise(resolve => setTimeout(resolve, 50))
      }
      assert.ok(artifact)
      const download = await runtime.request({
        method: 'GET', path: `/api/v1/workspaces/desktop-test/artifacts/${artifact.id}`,
        response: 'binary'
      }) as { status: number; body: { data: ArrayBuffer } }
      assert.equal(download.status, 200)
      assert.ok(download.body.data.byteLength > 0)
      await runtime.stop()
      await runtime.start(settings)
      assert.equal((await request('/sessions')).sessions[0].id, session.id)
      assert.ok((await request(`${route}/messages`)).items.length >= 2)
      await request(route, 'PATCH', { expected_revision: 0, title: 'Native session', pinned: true })
      assert.equal((await request('/sessions')).sessions[0].title, 'Native session')
      assert.equal((await raw(route, 'DELETE')).status, 202)
      for (let attempt = 0; attempt < 40; attempt++) {
        if ((await request(`${route}/deletion`)).status === 'deleted') break
        await new Promise(resolve => setTimeout(resolve, 100))
      }
      assert.equal((await request(`${route}/deletion`)).status, 'deleted')
      assert.equal((await raw(route)).status, 404)
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
  const request = async <T = any>(url: string, method: 'GET' | 'POST' = 'GET', body?: unknown) => {
    const response = await runtime.request({
      method, path: '/api/v1/workspaces/permissions-test' + url, body
    }) as { status: number; body: T }
    assert.ok(response.status >= 200 && response.status < 300, JSON.stringify(response))
    return response.body
  }
  const decide = (approval: {
    id: string; admission_id: string; generation: number; proposal_hash: string
  }, decision: 'approve' | 'deny') =>
    request(`/runs/${approval.admission_id}/approvals/${encodeURIComponent(approval.id)}/decision`, 'POST', {
      proposal_hash: approval.proposal_hash, generation: approval.generation, decision
    })
  try {
    await runtime.start(settings)
    const session = await request('/sessions', 'POST', {
      request_id: 'create', tools: ['read', 'write'], permission_mode: 'auto'
    })
    const route = `/sessions/${session.id}`
    const wait = async (expected: string) => {
      const deadline = Date.now() + 15_000
      while (Date.now() < deadline) {
        const status = await request(route)
        if (status.latest_admission?.status === expected) return status
        await new Promise(resolve => setTimeout(resolve, 50))
      }
      throw new Error(`Did not reach ${expected}`)
    }
    await request(`${route}/runs`, 'POST', {
      submission_id: 'auto-write',
      text: '/write-batch\n/write nested/auto.txt first\n/write nested/reused.txt second'
    })
    const first = (await wait('waiting_approval')).approvals[0]
    await assert.rejects(readFile(path.join(workspace, 'nested/auto.txt')))
    await decide(first, 'approve')
    assert.deepEqual((await wait('completed')).approvals, [])
    assert.equal(await readFile(path.join(workspace, 'nested/auto.txt'), 'utf8'), 'first')
    assert.equal(await readFile(path.join(workspace, 'nested/reused.txt'), 'utf8'), 'second')
    await request(`${route}/runs`, 'POST', {
      submission_id: 'new-round', text: '/write repeated.txt still-needs-approval'
    })
    const second = (await wait('waiting_approval')).approvals[0]
    await decide(second, 'deny')
    await wait('completed')
    await assert.rejects(readFile(path.join(workspace, 'repeated.txt')))
    await request(`${route}/permissions`, 'POST', {permission_mode: 'ask', expected_revision: 0})
    await request(`${route}/runs`, 'POST', {submission_id: 'ask-write', text: '/write denied.txt deny-me'})
    const pending = await wait('waiting_approval')
    await assert.rejects(request(`${route}/permissions`, 'POST', {permission_mode: 'full', expected_revision: 1}))
    await assert.rejects(readFile(path.join(workspace, 'denied.txt')))
    const approval = pending.approvals[0]
    await decide(approval, 'deny')
    await wait('completed')
    await assert.rejects(request(`${route}/permissions`, 'POST', {
      permission_mode: 'full', expected_revision: 1
    }))
    await request(`${route}/permissions`, 'POST', {
      permission_mode: 'full', expected_revision: 1, risk_acknowledged: true
    })
    await request(`${route}/runs`, 'POST', {submission_id: 'external-read', text: `/read ${outside}`})
    const full = await wait('completed')
    assert.ok(full.latest_admission.result.includes('outside evidence'))
    assert.equal(full.session.permission_mode, 'full')
    await runtime.stop()
    await runtime.start(settings)
    assert.equal((await request(route)).session.permission_mode, 'full')
    await assert.rejects(readFile(path.join(workspace, 'denied.txt')))
  } finally {
    await runtime.stop()
    await rm(temporary, {recursive: true, force: true})
  }
})
