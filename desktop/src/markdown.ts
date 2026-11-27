import { createElement } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { webLink } from './web-link.ts'

export function Markdown({ text, openLink }: {
  text: string
  openLink?: (href: string) => void
}) {
  const components: Components = {
    a: ({ href, children }) => href && openLink
      ? createElement('a', {
        href,
        onClick: event => {
          event.preventDefault()
          openLink(href)
        }
      }, children)
      : createElement('span', null, children),
    img: ({ alt }) => alt ? createElement('span', null, alt) : null,
    table: ({ children }) => createElement('div', { className: 'markdown-table' },
      createElement('table', null, children))
  }
  return createElement('div', { className: 'markdown-flow' }, createElement(ReactMarkdown, {
    remarkPlugins: [remarkGfm],
    skipHtml: true,
    urlTransform: url => webLink(url) ?? '',
    components,
    children: text
  }))
}

type Evidence = { source: string; observation: string }
export type ReportText = {
  summary: string
  evidence: Evidence[]
  changes: string[]
  tests: string[]
  limitations: string[]
}

export function readReportText(content: unknown): ReportText | null {
  let value: unknown = content
  if (typeof content === 'string') {
    const text = content.trim()
    const fence = /^```json[ \t]*\r?\n([\s\S]*?)\r?\n```$/i.exec(text)
    if (!fence && !text.startsWith('{')) return null
    try {
      value = JSON.parse(fence?.[1] ?? text)
    } catch {
      return null
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const row = value as Record<string, unknown>
  if (typeof row.summary !== 'string' || !Array.isArray(row.evidence)) return null
  if (!row.evidence.every(item => item && typeof item.source === 'string' &&
    typeof item.observation === 'string')) return null
  const sections = [row.changes, row.tests, row.limitations]
  const strings = (items: unknown): items is string[] =>
    Array.isArray(items) && items.every(item => typeof item === 'string')
  const [changes, tests, limitations] = sections.map(value => typeof value === 'string'
    ? (value.trim() ? [value] : []) : value)
  if (!strings(changes) || !strings(tests) || !strings(limitations)) return null
  return { summary: row.summary, evidence: row.evidence, changes, tests, limitations }
}
