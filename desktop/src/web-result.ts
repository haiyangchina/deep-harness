const labels = {
  ok: '已取得证据',
  partial: '部分取得证据',
  no_results: '无相关证据',
  unavailable: '未能取得证据',
}

export function webResultStatus(content: unknown): string | undefined {
  if (typeof content !== 'string') return
  if (/^Tool error \[web_(?:unavailable|timeout|backend|disabled|error)\]:/.test(content)) {
    return labels.unavailable
  }
  const status = content.match(
    /^# (?:联网检索：[^\n]*|深度联网研究：[^\n]*|网页读取|网页批量读取)\n检索状态: (ok|partial|no_results|unavailable)(?:\n|$)/
  )?.[1]
  return status ? labels[status as keyof typeof labels] : undefined
}
