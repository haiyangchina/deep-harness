import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { Markdown, readReportText } from '../src/markdown.ts'
import { webLink } from '../src/web-link.ts'

test('chat renders Markdown headings, lists, tables and literal code', () => {
  const text = [
    '# 项目总结', '', '- **入口** `main.py`', '',
    '| 文件 | 用途 |', '| --- | --- |', '| main.py | 启动 |', '',
    '```python', 'print("<tag>")', '```'
  ].join('\n')
  const html = renderToStaticMarkup(createElement(Markdown, { text }))
  assert.match(html, /<h1>项目总结<\/h1>/)
  assert.match(html, /<li><strong>入口<\/strong> <code>main\.py<\/code><\/li>/)
  assert.match(html, /<table>/)
  assert.match(html, /<pre><code class="language-python">print\(&quot;&lt;tag&gt;&quot;\)/)
})

test('Markdown permits HTTP links and drops raw HTML, URL schemes and remote images', () => {
  const text = [
    '[Docs](https://example.com/docs)', '[Local file](file:///C:/autoexec.bat)',
    '[Script](javascript:alert%281%29)', '![External image](https://example.com/image.png)',
    '<iframe src="https://example.com/"></iframe>'
  ].join('\n\n')
  const html = renderToStaticMarkup(createElement(Markdown, { text, openLink: () => {} }))
  assert.match(html, /<a href="https:\/\/example\.com\/docs">Docs<\/a>/)
  assert.match(html, /External image/)
  assert.doesNotMatch(html, /file:|javascript:|<iframe|<img|image\.png/)
  assert.equal(webLink('https://user:pass@example.com/'), undefined)
})

test('only report-shaped JSON becomes a report, including older fenced replies', () => {
  const body = {
    summary: 'Overview with `code`',
    evidence: [{ source: 'main.py:1', observation: 'Entry point' }],
    changes: [], tests: [], limitations: []
  }
  assert.deepEqual(readReportText(`\u0060\u0060\u0060json\r\n${JSON.stringify(body)}\r\n\u0060\u0060\u0060`), body)
  assert.deepEqual(readReportText(JSON.stringify(body)), body)
  assert.equal(readReportText('{"summary":"A normal JSON example"}'), null)
  assert.equal(readReportText({ ...body, evidence: [{ source: 2 }] }), null)
})
