// The compact mod's rules, run by `claude plugin test` with Claude Code's answers stubbed (plugins/mods/test.md).
import { expect, mock, test } from 'claude-code/testing'

import { quote } from '../hooks/compact.js'

const TOOL = 'mcp__orchestra__compact'
const COMMON = '/work/.git'
const STATE = '/state/fleet'
const POINTER = `${COMMON}/orchestra/main`
// A conversation of one message: a compaction runs over at least one and leaves at least one.
const MESSAGES = [{ role: 'user', text: 'the work so far', toolUses: [] }]

type Seen = { commands: string[]; prompts: string[]; logs: string[] }

// Claude Code's answers for a checkout whose primary branch is `main`: `pointed` is whether that branch has a state,
// `skip` a compaction's veto, and `git` false where no git starts.
function stub(
  t: any,
  on: any,
  { pointed = true, skip, git = true }: { pointed?: boolean; skip?: string; git?: boolean } = {},
) {
  const clock = mock.clock(on)
  const seen: Seen = { commands: [], prompts: [], logs: [] }
  on('tool.register', () => ({ value: { tool: TOOL } }))
  on('session.start', () => ({ cwd: '/work' }))
  on('session.root', () => ({ value: '/work' }))
  on('process.run', ($: any, e: any) => {
    if (!git) return { deny: 'git: not found' }
    const listed = e.argv.includes('worktree')
    const stdout = listed ? 'worktree /work\nHEAD abc\nbranch refs/heads/main\n\nworktree /w2\nbranch refs/heads/b\n' : `${COMMON}\n`
    return { value: { exitCode: 0, stdout, stderr: '' } }
  })
  // A path reaches the stub made absolute, on Windows with a drive and backslashes, so its end is compared.
  const at = (path: string, wanted: string) => path.replaceAll('\\', '/').endsWith(wanted)
  on('fs.exists', ($: any, e: any) => ({ value: pointed && (at(e.path, POINTER) || at(e.path, STATE)) }))
  on('fs.read', () => ({ value: `${STATE}\n` }))
  // The queued `/compact`, its compaction fired through the mod's `session.compact` hook as Claude Code's own is.
  on('command.run', async ($: any, e: any) => {
    seen.commands.push(e.command)
    await t.session.compact({ trigger: 'manual', messages: MESSAGES })
    return { text: '' }
  })
  on('session.compact', () => (skip === undefined ? { messages: MESSAGES } : { skip }))
  on('prompt.submit', ($: any, e: any) => {
    seen.prompts.push(e.text)
    return { text: e.text }
  })
  on('ui.log', ($: any, e: any) => {
    seen.logs.push(e.text)
    return { value: undefined }
  })
  on('turn.complete', () => ({ text: '' }))
  return { clock, seen }
}

const end = (reason = 'answer', agentId?: string) => ({
  turnId: 't1',
  answer: 'done',
  durationMs: 1,
  isAborted: reason === 'aborted',
  reason,
  ...(agentId === undefined ? {} : { agentId }),
})

test("the main loop's call compacts once its turn ends, then wakes the session", async ($, on) => {
  const { clock, seen } = stub($, on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const answer = await $.tool.call({ tool: TOOL })
  expect(answer).toEqual({ result: 'scheduled: end your turn, and the conversation compacts once it has ended' })
  expect(seen.commands).toEqual([])
  await $.turn.complete(end())
  await clock.settle()
  expect(seen.commands).toEqual(['compact'])
  expect(seen.prompts).toEqual(['The compaction scheduled with the compact tool has run.'])
})

test('an agent is refused, and its turn end compacts nothing', async ($, on) => {
  const { clock, seen } = stub($, on)
  const answer = await $.tool.call({ tool: TOOL, agentId: 'a1' })
  expect(answer).toEqual({ deny: `${TOOL} compacts the main conversation alone; agent a1 called it.` })
  await $.turn.complete(end())
  await clock.settle()
  expect(seen.commands).toEqual([])
})

test('a session whose branch holds no orchestra state is refused', async ($, on) => {
  const { clock, seen } = stub($, on, { pointed: false })
  const answer = await $.tool.call({ tool: TOOL })
  expect(answer).toEqual({
    deny: `${TOOL} compacts an orchestra coordinator, and this session's checkout holds no orchestra session on its branch.`,
  })
  await $.turn.complete(end())
  await clock.settle()
  expect(seen.commands).toEqual([])
})

test('a check that cannot run git is refused with its reason, and schedules nothing', async ($, on) => {
  const { clock, seen } = stub($, on, { git: false })
  const answer: any = await $.tool.call({ tool: TOOL })
  expect(answer.deny).toMatch(/^mcp__orchestra__compact could not check for an orchestra session, so nothing was scheduled: .*git: not found/)
  await $.turn.complete(end())
  await clock.settle()
  expect(seen.commands).toEqual([])
})

test('a turn the owner interrupts drops the compaction and sends no prompt', async ($, on) => {
  const { clock, seen } = stub($, on)
  await $.tool.call({ tool: TOOL })
  await $.turn.complete(end('aborted'))
  await clock.settle()
  expect(seen.commands).toEqual([])
  expect(seen.prompts).toEqual([])
  expect(seen.logs).toEqual(['the turn ended (aborted), so the compaction it scheduled was dropped'])
})

test("an agent's turn end leaves the main loop's request for the main loop's", async ($, on) => {
  const { clock, seen } = stub($, on)
  await $.tool.call({ tool: TOOL })
  await $.turn.complete(end('answer', 'a1'))
  await clock.settle()
  expect(seen.commands).toEqual([])
  await $.turn.complete(end())
  await clock.settle()
  expect(seen.commands).toEqual(['compact'])
})

test('a compaction a hook vetoes still wakes the session, saying why', async ($, on) => {
  const { clock, seen } = stub($, on, { skip: 'blocked by a PreCompact hook' })
  await $.tool.call({ tool: TOOL })
  await $.turn.complete(end())
  await clock.settle()
  expect(seen.prompts).toEqual([
    'The compaction scheduled with the compact tool did not run: blocked by a PreCompact hook.',
  ])
})

test("the pointer's name is quoted as Python's quote(branch, safe='') quotes it", () => {
  // Printed by `python -c "from urllib.parse import quote; print(quote('fix/a b!(x)*~é', safe=''))"`.
  expect(quote('fix/a b!(x)*~é')).toBe('fix%2Fa%20b%21%28x%29%2A~%C3%A9')
})
