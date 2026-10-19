// Measurement-only Claude Code Mod for LemonCrow.
//
// This module NEVER rewrites prompts, tools, effort, or models. It observes the
// exact model-request usage Claude Code exposes through turn.step and persists
// one compact session snapshot for LemonCrow reporting. The main conversation
// and subagents are separated by agentId so a subagent-heavy run is not
// presented as if all cost belonged to the parent.

let sessionId = ''
let rootPath = ''
let outputPath = ''
let startedAtMs = 0
let latestSessionUsage = null
let flushChain = Promise.resolve()
let totals = newTotals()
let agents = Object.create(null)
let models = Object.create(null)
let calibrationWindows = Object.create(null)
let calibrationSeq = 0
const CALIBRATION_MAX_MS = 15 * 60 * 1000

function newTotals() {
  return {
    steps: 0,
    turns: 0,
    duration_ms: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
  }
}

function emptyBucket(kind, agentId = null) {
  return {
    kind,
    agent_id: agentId,
    steps: 0,
    turns: 0,
    duration_ms: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
    models: Object.create(null),
    model_usage: Object.create(null),
  }
}

function resetSession(id, now) {
  sessionId = id
  outputPath = rootPath + '/claude-mod-usage-' + sessionId + '.json'
  startedAtMs = now
  latestSessionUsage = null
  totals = newTotals()
  agents = Object.create(null)
  models = Object.create(null)
  calibrationWindows = Object.create(null)
  calibrationSeq = 0
  flushChain = Promise.resolve()
}

async function ensureRoot($) {
  if (rootPath) return
  const configuredRoot = await $.env.get('LEMONCROW_ROOT')
  const home = await $.env.get('HOME')
  rootPath = configuredRoot || (home ? home + '/.lemoncrow' : '.lemoncrow')
}

function restoreSnapshot(raw) {
  if (!raw || raw.schema_version !== 1 || raw.source !== 'claude_mod') return false
  if (String(raw.session_id || '') !== sessionId) return false
  startedAtMs = Number(raw.started_at_ms || startedAtMs)
  latestSessionUsage = raw.session_usage || null
  totals = { ...newTotals(), ...(raw.totals || {}) }
  agents = Object.create(null)
  if (raw.main) agents.main = raw.main
  for (const [key, value] of Object.entries(raw.subagents || {})) agents[key] = value
  models = { ...(raw.models || {}) }
  calibrationWindows = raw.calibration_windows && typeof raw.calibration_windows === 'object'
    ? { ...raw.calibration_windows }
    : Object.create(null)
  return true
}

async function ensureSession($, { restore = true } = {}) {
  await ensureRoot($)
  const liveId = String(await $.session.id())
  if (liveId === sessionId && outputPath) return
  const now = await $.clock.now()
  resetSession(liveId, now)
  if (!restore) return
  try {
    if (await $.fs.exists(outputPath)) {
      restoreSnapshot(JSON.parse(await $.fs.read(outputPath)))
    }
  } catch {
    // A partial/old snapshot must never affect the coding session.
  }
}

function bucketFor(agentId) {
  const key = agentId || 'main'
  if (!agents[key]) agents[key] = emptyBucket(agentId ? 'subagent' : 'main', agentId || null)
  return agents[key]
}

function addUsage(bucket, usage) {
  if (!usage) return
  const input = Number(usage.input_tokens || 0)
  const output = Number(usage.output_tokens || 0)
  const cacheRead = Number(usage.cache_read_input_tokens || 0)
  const cacheWrite = Number(usage.cache_creation_input_tokens || 0)
  const model = String(usage.model || 'unknown')

  bucket.steps += 1
  bucket.input_tokens += input
  bucket.output_tokens += output
  bucket.cache_read_input_tokens += cacheRead
  bucket.cache_creation_input_tokens += cacheWrite
  bucket.models[model] = (bucket.models[model] || 0) + 1
  const modelUsage = bucket.model_usage[model] || {
    steps: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
  }
  modelUsage.steps += 1
  modelUsage.input_tokens += input
  modelUsage.output_tokens += output
  modelUsage.cache_read_input_tokens += cacheRead
  modelUsage.cache_creation_input_tokens += cacheWrite
  bucket.model_usage[model] = modelUsage

  totals.steps += 1
  totals.input_tokens += input
  totals.output_tokens += output
  totals.cache_read_input_tokens += cacheRead
  totals.cache_creation_input_tokens += cacheWrite
  models[model] = (models[model] || 0) + 1
}

function snapshot(updatedAtMs) {
  const main = agents.main || emptyBucket('main', null)
  const subagents = Object.fromEntries(Object.entries(agents).filter(([key]) => key !== 'main'))
  return {
    schema_version: 1,
    source: 'claude_mod',
    session_id: sessionId,
    started_at_ms: startedAtMs,
    updated_at_ms: updatedAtMs,
    session_usage: latestSessionUsage,
    totals: { ...totals },
    main: { ...main },
    subagents,
    models: { ...models },
    calibration_windows: { ...calibrationWindows },
  }
}

function eventCommand(e) {
  if (typeof e?.command === 'string') return e.command
  for (const container of [e?.input, e?.args, e?.parameters]) {
    if (container && typeof container.command === 'string') return container.command
  }
  return ''
}

function normalizedCommand(command) {
  return String(command || '').trim().replace(/\s+/g, ' ')
}

function calibrationKey(e, command) {
  return String(e?.agentId || 'main') + '\u0000' + normalizedCommand(command)
}

function cloneModelUsage(bucket) {
  const result = Object.create(null)
  for (const [model, usage] of Object.entries(bucket?.model_usage || {})) {
    result[model] = { ...usage }
  }
  return result
}

async function calibrationSnapshot($, agentId) {
  const bucket = bucketFor(agentId || null)
  let sessionUsage = null
  try { sessionUsage = await $.session.usage() } catch {}
  return {
    total_steps: totals.steps,
    total_turns: totals.turns,
    agent_steps: bucket.steps,
    agent_turns: bucket.turns,
    model_usage: cloneModelUsage(bucket),
    session_usage: sessionUsage,
  }
}

async function startCalibrationWindow($, e, command) {
  await ensureSession($)
  const normalized = normalizedCommand(command)
  if (!normalized) return
  const key = calibrationKey(e, normalized)
  const now = await $.clock.now()
  const existing = calibrationWindows[key]
  if (existing && now - Number(existing.started_at_ms || 0) <= CALIBRATION_MAX_MS) return
  if (existing) delete calibrationWindows[key]
  calibrationWindows[key] = {
    command: normalized,
    agent_id: e?.agentId || null,
    started_at_ms: now,
    start: await calibrationSnapshot($, e?.agentId || null),
  }
  await flush($)
}

async function finishCalibrationWindow($, e, command) {
  await ensureSession($)
  const normalized = normalizedCommand(command)
  if (!normalized) return
  const key = calibrationKey(e, normalized)
  const window = calibrationWindows[key]
  if (!window) return
  delete calibrationWindows[key]
  try {
    await ensureRoot($)
    const endedAtMs = await $.clock.now()
    if (endedAtMs - Number(window.started_at_ms || 0) > CALIBRATION_MAX_MS) {
      await flush($)
      return
    }
    const payloadPath = rootPath + '/verified-work-baseline-' + sessionId + '-' + (++calibrationSeq) + '.json'
    const payload = {
      schema_version: 1,
      host: 'claude',
      session_id: sessionId,
      agent_id: window.agent_id,
      command: window.command,
      started_at_ms: window.started_at_ms,
      ended_at_ms: endedAtMs,
      start: window.start,
      end: await calibrationSnapshot($, window.agent_id),
    }
    await $.fs.write(payloadPath, JSON.stringify(payload) + '\n')
    await $.process.spawn(['lc', 'runtime', 'work', 'record-baseline', '--input', payloadPath])
  } catch {
    // Calibration must never affect a coding session.
  }
  await flush($)
}

async function observeShadowVerifier($, e, result) {
  const command = eventCommand(e)
  if (!command) return
  const text = toolResultText(result)
  const exitCode = toolResultExitCode(result, text)
  if (exitCode === 0) await finishCalibrationWindow($, e, command)
  else await startCalibrationWindow($, e, command)
}

async function flush($) {
  if (!outputPath || !sessionId) return
  try {
    const now = await $.clock.now()
    const body = JSON.stringify(snapshot(now), null, 2) + '\n'
    // Subagents can finish concurrently. Serialize whole-file snapshots so an
    // older write can never land after a newer one and roll counters backward.
    flushChain = flushChain.then(() => $.fs.write(outputPath, body)).catch(() => {})
    await flushChain
  } catch {
    // Measurement must never affect the coding session.
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    await ensureSession($)
    try {
      latestSessionUsage = await $.session.usage()
      await flush($)
    } catch {
      // Fail open. A later session.measure can populate the snapshot.
    }
    return next(e)
  })

  // session.start does not fire after /clear, /resume, or /branch. Re-anchor
  // before each turn so those transitions cannot keep writing the old session.
  on('turn.start', async ($, e, next) => {
    await ensureSession($)
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    await ensureSession($)
    const result = yield* next(e)
    if (result && result.usage) addUsage(bucketFor(e.agentId || null), result.usage)
    return result
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    // Preserve Claude's permission semantics: native Bash always runs through
    // the normal chain first. Verified Local Work only observes its result.
    const result = await next(e)
    const mode = await verifiedWorkMode($)
    if (mode === 'off') return result

    const command = eventCommand(e)
    const text = toolResultText(result)
    const exitCode = toolResultExitCode(result, text)
    if (mode === 'shadow') {
      await observeShadowVerifier($, e, result)
      if (exitCode === 0) return result
      let inputPath = ''
      try {
        inputPath = await writeVerifiedWorkPayload($, e, result)
        // Queue ownership is transferred to a short-lived child. It persists
        // the payload before spawning the detached worker, so Claude exiting
        // cannot discard qualification work.
        await $.process.spawn(['lc', 'runtime', 'work', 'enqueue-bash', '--input', inputPath])
      } catch {}
      return result
    }

    if (exitCode === 0) return result
    let inputPath = ''
    try {
      inputPath = await writeVerifiedWorkPayload($, e, result)
      const bridge = await $.process.run(['lc', 'runtime', 'work', 'after-bash', '--input', inputPath])
      if (!bridge || Number(bridge.exitCode || 0) !== 0) return result
      const lines = String(bridge.stdout || '').trim().split(/\r?\n/).filter(Boolean)
      if (!lines.length) return result
      const payload = JSON.parse(lines[lines.length - 1])
      if (!payload?.promoted) {
        // Auto without this repository's opt-in stays Learning; the bridge asks
        // once (`notice`). Append it so the original failure stays intact.
        if (payload?.notice && typeof result?.result === 'string') {
          return { ...result, result: result.result + '\n' + String(payload.notice) }
        }
        return result
      }
      return {
        ...result,
        isError: false,
        result: String(payload.message || 'LemonCrow verified local repair applied.'),
      }
    } catch {
      // Host adapter is strictly fail-open. If the bridge is unavailable or a
      // local model fails, Claude sees the exact native Bash result it produced.
      return result
    }
  })

  on('tool.call', { tool: 'mcp__lc__bash' }, async ($, e, next) => {
    // MCP Bash already owns repair execution in Python. The Mod only measures
    // Learning-mode host work between a failed verifier and the same command
    // later passing, so the dominant LemonCrow Bash path gets paired baselines.
    const result = await next(e)
    if ((await verifiedWorkMode($)) === 'shadow') {
      await observeShadowVerifier($, e, result)
    }
    return result
  })

  on('turn.complete', async ($, e, next) => {
    await ensureSession($)
    const bucket = bucketFor(e.agentId || null)
    bucket.turns += 1
    bucket.duration_ms += Number(e.durationMs || 0)
    totals.turns += 1
    totals.duration_ms += Number(e.durationMs || 0)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    await ensureSession($)
    const result = await next(e)
    try {
      latestSessionUsage = await $.session.usage()
      await flush($)
    } catch {
      // Fail open.
    }
    return result
  })

  on('session.end', async ($, e, next) => {
    await ensureSession($)
    try {
      latestSessionUsage = await $.session.usage()
      await flush($)
    } catch {
      // Fail open.
    }
    return next(e)
  })
}

let verifiedWorkBridgeSeq = 0

async function verifiedWorkMode($) {
  try {
    const explicit = String((await $.env.get('LEMONCROW_VERIFIED_WORK_MODE')) || '').trim().toLowerCase()
    if (['off', 'shadow', 'enforce'].includes(explicit)) return explicit
  } catch {}
  try {
    await ensureRoot($)
    const path = rootPath + '/plugin_settings.json'
    if (!(await $.fs.exists(path))) return 'off'
    const raw = JSON.parse(await $.fs.read(path))
    const nested = raw && typeof raw.lemoncrow === 'object' ? raw.lemoncrow : null
    const value = String(raw?.['verified_work.mode'] ?? nested?.['verified_work.mode'] ?? 'off').trim().toLowerCase()
    return ['off', 'shadow', 'enforce'].includes(value) ? value : 'off'
  } catch {
    return 'off'
  }
}

function toolResultText(result) {
  if (!result || typeof result !== 'object') return String(result || '')
  if (typeof result.result === 'string') return result.result
  if (typeof result.stdout === 'string' || typeof result.stderr === 'string') {
    return [result.stdout || '', result.stderr || ''].filter(Boolean).join('\n')
  }
  if (Array.isArray(result.content)) {
    return result.content
      .filter(block => block && typeof block === 'object' && block.type === 'text')
      .map(block => String(block.text || ''))
      .filter(Boolean)
      .join('\n')
  }
  try { return JSON.stringify(result) } catch { return '' }
}

function toolResultExitCode(result, text) {
  const direct = Number(result?.exitCode ?? result?.exit_code)
  if (Number.isFinite(direct)) return direct
  const match = String(text || '').match(/(?:exit(?:ed)?(?: with)? code|exit_code)\s*[:=]?\s*(-?\d+)/i)
  if (match) return Number(match[1])
  const bracket = String(text || '').match(/\[exit\s+(-?\d+)\]/i)
  if (bracket) return Number(bracket[1])
  return result?.isError ? 1 : 0
}

async function writeVerifiedWorkPayload($, e, result) {
  await ensureRoot($)
  const text = toolResultText(result)
  const id = String(await $.session.id())
  const cwd = String(await $.session.cwd())
  const workspace = String((await $.session.root()) || cwd)
  const path = rootPath + '/verified-work-mod-' + id + '-' + (++verifiedWorkBridgeSeq) + '.json'
  await $.fs.write(path, JSON.stringify({
    schema_version: 1,
    host: 'claude',
    session_id: id,
    workspace,
    cwd,
    command: String(e.command || ''),
    exit_code: toolResultExitCode(result, text),
    stdout: text,
    stderr: '',
  }) + '\n')
  return path
}
