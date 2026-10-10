// One finite, tool-free native turn. The durable session ID is recorded by Thoth first.
export async function completeIntake(request, invoke) {
  const { sessionId, requestId, provider, model, workspaceId, title, content, timeoutSeconds } = request
  if (!/^thoth-local-(text|vision|ocr)$/.test(provider) ||
      !/^[a-f0-9-]{36}$/.test(sessionId) || requestId !== sessionId ||
      !Number.isFinite(timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 900) {
    throw new Error('Invalid Thoth ingestion request')
  }
  const rpc = async (method, body) => {
    const result = await invoke(method, body)
    if (!result.ok) throw Object.assign(new Error(result.error?.message || 'Harness operation failed'), { code: result.error?.code })
    return result.value
  }
  const deadline = Date.now() + timeoutSeconds * 1000
  let submitted = false
  let owned = false
  try {
    const catalog = await rpc('modelCatalog', {})
    if (!catalog.groups.some(group => group.id === provider && group.models.some(row => row.id === model))) {
      throw Object.assign(new Error('The configured local model is unavailable in Harness'), { code: 'local_model_unconfigured' })
    }
    await rpc('create', { sessionId, workspaceId, agentPreset: 'thoth-ingestion' })
    owned = true
    await rpc('rename', { sessionId, title })
    let cursor = -1
    while (Date.now() < deadline) {
      const projection = await rpc('projections', { sessionId })
      if (!projection) throw new Error('Harness ingestion session is missing')
      const throughSeq = projection.asOfSeq
      if (throughSeq !== cursor) {
        cursor = throughSeq
        const page = await rpc('page', {
          address: { kind: 'session', sessionId }, throughSeq, maxMessages: 20,
          turnWindow: { minMessages: 1, minTurns: 1 },
        })
        const events = page.records.map(row => row.event)
        const prompts = events.filter(event => event.type === 'user/message')
        if (prompts.some(event => event.data.source?.rpcId !== requestId)) {
          throw new Error('Harness ingestion session contains another request')
        }
        const end = events.findLast(event => event.type === 'turn/end')
        if (end) {
          const reason = end.data.reason
          if (!['completed', 'max-tokens'].includes(reason.kind)) {
            throw Object.assign(new Error(reason.error?.message || `Harness turn ended: ${reason.kind}`), { code: reason.error?.code || reason.kind })
          }
          if (reason.kind === 'max-tokens' && projection.values?.tokenUsage?.outputTokens <= 1) {
            throw Object.assign(new Error('Local model context limit left no room for the answer; reduce input'), { code: 'harness_context_limit' })
          }
          const last = events.findLast(event => event.type === 'assistant/message')
          const text = (last?.data.message.content || []).filter(part => part.type === 'text').map(part => part.text).join('\n')
          if (!text.trim()) throw new Error('Harness ingestion returned no text')
          return { ok: true, value: { text, sessionId, cursor, endReason: reason.kind,
            modelSelection: projection.values?.modelSelection, tokenUsage: projection.values?.tokenUsage,
            sessionStats: projection.values?.sessionStats } }
        }
        if (!submitted && prompts.length === 0) {
          await rpc('selectModel', { sessionId, provider, model, reasoningEffort: 'off' })
          await rpc('prompt', { sessionId, requestId, mode: 'queue', content })
          submitted = true
        } else if (prompts.length) submitted = true
      }
      await new Promise(resolve => setTimeout(resolve, Math.min(1000, Math.max(0, deadline - Date.now()))))
    }
    throw Object.assign(new Error('Harness ingestion exceeded its deadline'), { code: 'harness_timeout' })
  } catch (error) {
    if (owned) {
      try { await rpc('cancel', { sessionId }) } catch { /* Retain the original failure. */ }
    }
    return { ok: false, error: { code: error.code || 'harness_intake_error', message: error.message } }
  }
}
