import assert from 'node:assert/strict'
import { test } from 'node:test'
import { webResultStatus } from '../src/web-result.ts'

test('web tool summaries distinguish evidence, partial results, and backend failures', () => {
  assert.equal(webResultStatus('# 联网检索：文档\n检索状态: ok\n来源数：1'), '已取得证据')
  assert.equal(webResultStatus('# 深度联网研究：问题\n检索状态: partial'), '部分取得证据')
  assert.equal(webResultStatus('# 网页读取\n检索状态: no_results'), '无相关证据')
  assert.equal(webResultStatus('Tool error [web_timeout]: 请求超时'), '未能取得证据')
  assert.equal(webResultStatus('ordinary tool output\n检索状态: partial'), undefined)
})
