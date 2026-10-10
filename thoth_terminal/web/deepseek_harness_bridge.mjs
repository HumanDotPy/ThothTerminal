// Pinned DSH 0.1.7-rc.1 unary bridge. Secrets arrive on stdin, never argv or stdout.
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { join } from 'node:path'
import { completeIntake } from './deepseek_harness_intake.mjs'

const input = JSON.parse(readFileSync(0, 'utf8'))
const packageRoot = join(input.installRoot, 'node_modules', '@deepseek-ai')
const load = async (relative) => import(pathToFileURL(join(packageRoot, relative)).href)
// The published client-connection bundle is browser-loader only. Keep the
// fixed unary envelope here and use the release's generated Typert schemas.
const allowed = new Set(['list', 'create', 'prompt', 'page', 'projections', 'rename', 'selectModel', 'modelCatalog', 'cancel', 'workspace/create', 'workspace/rename'])
async function rpc(operation, body) {
  if (!allowed.has(operation)) throw new Error('Unsupported Harness operation')
  const [namespace, method] = operation.includes('/') ? operation.split('/') : ['session', operation]
  const { TYPERT_REMOTE } = await load(`dsh-api-${namespace}-controller/lib/typert.remote-client.js`)
  const descriptor = TYPERT_REMOTE.descriptors.find(
    item => item.namespace === namespace && item.method === method && item.invocation.kind === 'direct',
  )
  if (!descriptor || descriptor.parameters.length > 1 ||
      descriptor.parameters.length === 1 && !['request', '_request'].includes(descriptor.parameters[0].wire)) {
    throw new Error('Installed DSH contract does not match the pinned bridge')
  }
  const args = descriptor.parameters.length
    ? { [descriptor.parameters[0].wire]: descriptor.parameters[0].codec.create().parse(body) }
    : {}
  const origin = new URL(input.origin)
  if (origin.hostname !== '127.0.0.1' || origin.protocol !== 'http:' || origin.pathname !== '/') {
    throw new Error('Invalid local DSH origin')
  }
  const rpcId = crypto.randomUUID()
  const response = await fetch(new URL(`api/${namespace}/${method}`, origin), {
    method: 'POST', redirect: 'manual', signal: AbortSignal.timeout(20000),
    headers: { 'content-type': 'application/json', cookie: input.cookie },
    body: JSON.stringify({ type: 'client-request', rpcId, method: `${namespace}/${method}`, payload: { args } }),
  })
  if (!response.ok) throw new Error(`DSH RPC returned HTTP ${response.status}`)
  const envelope = await response.json()
  if (envelope?.type !== 'server-response' || envelope.rpcId !== rpcId || typeof envelope.result?.ok !== 'boolean') {
    throw new Error('DSH RPC envelope did not match the pinned connection contract')
  }
  const result = envelope.result
  if (result.ok) result.value = descriptor.result.create().parse(result.value)
  return result
}
const result = input.method === 'intake'
  ? await completeIntake(input.request, rpc)
  : await rpc(input.method, input.request)
process.stdout.write(JSON.stringify(result))
