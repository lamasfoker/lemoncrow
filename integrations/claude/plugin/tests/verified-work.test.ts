import { expect, test } from 'claude-code/testing'

test('native Bash success is passed through without bridge work', async ($, on) => {
  on('tool.call', () => ({ result: 'ok', isError: false }))

  const answer = await $.tool.call({ tool: 'Bash', command: 'npm run typecheck' })

  expect(answer).toMatchObject({ result: 'ok', isError: false })
})

test('enforce rewrites a failed native Bash result only after bridge promotion', async ($, on) => {
  let written = ''
  let argv: string[] = []
  on('tool.call', () => ({ result: "src/a.ts(1,1): error TS2304: Cannot find name 'x'.", isError: true }))
  on('env.get', ($, e) => ({
    value:
      e.name === 'LEMONCROW_VERIFIED_WORK_MODE'
        ? 'enforce'
        : e.name === 'LEMONCROW_ROOT'
          ? '/lc'
          : e.name === 'HOME'
            ? '/home/test'
            : undefined,
  }))
  on('session.id', () => ({ value: 'session-1' }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('session.root', () => ({ value: '/repo' }))
  on('fs.write', ($, e) => {
    written = String(e.data ?? e.content ?? '')
    return { value: undefined }
  })
  on('process.run', ($, e) => {
    argv = [...e.argv]
    return {
      value: {
        exitCode: 0,
        stdout: JSON.stringify({ promoted: true, message: 'LemonCrow verified local repair applied.' }) + '\n',
        stderr: '',
      },
    }
  })

  const answer = await $.tool.call({ tool: 'Bash', command: 'npm run typecheck' })

  expect(argv).toEqual(['lc', 'runtime', 'work', 'after-bash', '--input', '/lc/verified-work-mod-session-1-1.json'])
  expect(written).toContain('npm run typecheck')
  expect(written).toContain('session-1')
  expect(answer.isError).toBe(false)
  expect(answer.result).toBe('LemonCrow verified local repair applied.')
})

test('enforce leaves original failure when bridge does not promote', async ($, on) => {
  on('tool.call', () => ({ result: 'typecheck failed', isError: true }))
  on('env.get', ($, e) => ({
    value: e.name === 'LEMONCROW_VERIFIED_WORK_MODE' ? 'enforce' : e.name === 'LEMONCROW_ROOT' ? '/lc' : '',
  }))
  on('session.id', () => ({ value: 'session-2' }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('session.root', () => ({ value: '/repo' }))
  on('fs.write', () => ({ value: undefined }))
  on('process.run', () => ({
    value: { exitCode: 0, stdout: JSON.stringify({ promoted: false, reason: 'base_hash_conflict' }) + '\n', stderr: '' },
  }))

  const answer = await $.tool.call({ tool: 'Bash', command: 'npm run typecheck' })

  expect(answer).toMatchObject({ result: 'typecheck failed', isError: true })
})
