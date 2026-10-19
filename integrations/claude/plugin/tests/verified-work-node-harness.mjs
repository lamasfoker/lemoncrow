import assert from 'node:assert/strict'
import { register } from '../hooks/measure.js'

const registrations = []
register((event, selector, handler) => {
  if (typeof selector === 'function') {
    handler = selector
    selector = null
  }
  registrations.push({ event, selector, handler })
})
const tool = registrations.find(row => row.event === 'tool.call' && row.selector?.tool === 'Bash')
const mcpTool = registrations.find(row => row.event === 'tool.call' && row.selector?.tool === 'mcp__lc__bash')
assert.ok(tool, 'Bash tool.call handler must be registered')
assert.ok(mcpTool, 'mcp__lc__bash tool.call handler must be registered')

function baseApi(mode, bridgePayload = null) {
  let written = ''
  const writes = new Map()
  let spawned = null
  const spawns = []
  let ran = null
  let now = 1000
  let usageCost = 1.0
  return {
    api: {
      env: { get: async name => name === 'LEMONCROW_VERIFIED_WORK_MODE' ? mode : name === 'LEMONCROW_ROOT' ? '/lc' : '/home/test' },
      session: {
        id: async () => 'session-node',
        cwd: async () => '/repo',
        root: async () => '/repo',
        usage: async () => ({ cost: { total_cost_usd: usageCost } }),
      },
      fs: {
        write: async (path, data) => { written = String(data); writes.set(String(path), String(data)) },
        exists: async path => writes.has(String(path)),
        read: async path => writes.get(String(path)) || '',
      },
      process: {
        run: async argv => {
          ran = [...argv]
          return { exitCode: 0, stdout: JSON.stringify(bridgePayload ?? { promoted: false }) + '\n', stderr: '' }
        },
        spawn: async argv => { spawned = [...argv]; spawns.push([...argv]); return {} },
      },
      clock: { now: async () => ++now },
    },
    state: () => ({ written, writes, spawned, spawns, ran }),
    setUsageCost: value => { usageCost = value },
    advance: value => { now += value },
  }
}

// Successful native Bash is untouched and does not invoke the bridge.
{
  const { api, state } = baseApi('enforce', { promoted: true })
  const original = { isError: false, result: 'ok' }
  const result = await tool.handler(api, { command: 'npm run typecheck' }, async () => original)
  assert.equal(result, original)
  assert.equal(state().ran, null)
}

// Enforce changes the returned tool result only after the Python bridge reports promotion.
{
  const { api, state } = baseApi('enforce', { promoted: true, message: 'verified repair' })
  const original = { isError: true, result: 'typecheck failed' }
  const result = await tool.handler(api, { command: 'npm run typecheck' }, async () => original)
  assert.equal(result.isError, false)
  assert.equal(result.result, 'verified repair')
  assert.deepEqual(state().ran.slice(0, 4), ['lc', 'runtime', 'work', 'after-bash'])
  const payload = JSON.parse(state().written)
  assert.equal(payload.command, 'npm run typecheck')
  assert.equal(payload.session_id, 'session-node')
}

// A non-promoted bridge result is fail-open to the exact original tool result.
{
  const { api } = baseApi('enforce', { promoted: false, reason: 'base_hash_conflict' })
  const original = { isError: true, result: 'typecheck failed' }
  const result = await tool.handler(api, { command: 'npm run typecheck' }, async () => original)
  assert.equal(result, original)
}

// Auto without this repository's opt-in stays Learning: the one-time question is
// appended to the original failure, which is otherwise unchanged.
{
  const notice = '[lc] Local Savings Auto is paused in this repository: run `lc rt savings auto`'
  const { api } = baseApi('enforce', { promoted: false, queued: true, reason: 'repository_not_opted_in', notice })
  const original = { isError: true, result: 'typecheck failed' }
  const result = await tool.handler(api, { command: 'npm run typecheck' }, async () => original)
  assert.equal(result.isError, true)
  assert.equal(result.result, 'typecheck failed\n' + notice)
}

// A promotion carries the undo command from the bridge message verbatim.
{
  const message = 'LemonCrow verified local repair applied.\nVerifier passed after repair · 1 attempt(s) · src/a.ts\nUndo: lc rt savings undo abc123def456\n'
  const { api } = baseApi('enforce', { promoted: true, message })
  const result = await tool.handler(api, { command: 'npm run typecheck' }, async () => ({ isError: true, result: 'x' }))
  assert.equal(result.result, message)
}
// Shadow launches detached observation and never changes the native Bash result.
{
  const { api, state } = baseApi('shadow')
  const original = { isError: true, result: 'lint failed' }
  const result = await tool.handler(api, { command: 'ruff check .' }, async () => original)
  assert.equal(result, original)
  assert.deepEqual(state().spawned.slice(0, 4), ['lc', 'runtime', 'work', 'enqueue-bash'])
  assert.equal(state().ran, null)
}

// Learning mode records a paired host interval when the same verifier later passes.
{
  const fixture = baseApi('shadow')
  const failed = { isError: true, result: 'typecheck failed [exit 2]' }
  const passed = { isError: false, result: 'typecheck passed [exit 0]' }
  await tool.handler(fixture.api, { command: 'npm run typecheck' }, async () => failed)
  fixture.setUsageCost(1.4)
  await tool.handler(fixture.api, { command: 'npm run typecheck' }, async () => passed)

  assert.ok(fixture.state().spawns.some(argv => argv.slice(0, 4).join(' ') === 'lc runtime work enqueue-bash'))
  const baselineSpawn = fixture.state().spawns.find(argv => argv.slice(0, 4).join(' ') === 'lc runtime work record-baseline')
  assert.ok(baselineSpawn, 'paired baseline bridge must be spawned')
  const baselinePath = baselineSpawn[baselineSpawn.length - 1]
  const baseline = JSON.parse(fixture.state().writes.get(baselinePath))
  assert.equal(baseline.command, 'npm run typecheck')
  assert.equal(baseline.session_id, 'session-node')
  assert.equal(baseline.start.session_usage.cost.total_cost_usd, 1.0)
  assert.equal(baseline.end.session_usage.cost.total_cost_usd, 1.4)
}

// Stale Learning windows are discarded rather than creating hours-long fake savings baselines.
{
  const fixture = baseApi('shadow')
  await tool.handler(
    fixture.api,
    { command: 'npm run typecheck' },
    async () => ({ isError: true, result: 'failed [exit 2]' }),
  )
  fixture.advance(16 * 60 * 1000)
  await tool.handler(
    fixture.api,
    { command: 'npm run typecheck' },
    async () => ({ isError: false, result: 'passed [exit 0]' }),
  )
  assert.equal(
    fixture.state().spawns.some(argv => argv.slice(0, 4).join(' ') === 'lc runtime work record-baseline'),
    false,
  )
}

// The dominant LemonCrow MCP Bash path participates in the same Learning calibration.
{
  const fixture = baseApi('shadow')
  await mcpTool.handler(
    fixture.api,
    { input: { command: 'ruff check .' } },
    async () => ({ isError: true, result: 'lint failed [exit 1]' }),
  )
  fixture.setUsageCost(1.2)
  await mcpTool.handler(
    fixture.api,
    { input: { command: 'ruff check .' } },
    async () => ({ isError: false, result: 'lint passed [exit 0]' }),
  )
  assert.ok(fixture.state().spawns.some(argv => argv.slice(0, 4).join(' ') === 'lc runtime work record-baseline'))
}

console.log('verified-work Mod node harness: ok')
