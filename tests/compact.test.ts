// The compact mod's rules, run by `claude plugin test` with Claude Code's answers stubbed (plugins/mods/test.md).
import { expect, mock, test } from 'claude-code/testing'

import { quote } from '../hooks/compact.js'

const TOOL = 'mcp__orchestra__compact'
const SCHEDULED = { result: 'scheduled: end your turn, and the conversation compacts once it has ended' }
const REFUSED = {
  deny: `${TOOL} schedules nothing until the owner's next prompt or a compaction it sees run, since the one it last scheduled was not seen to run.`,
}
const COMMON = '/work/.git'
const STATE = '/state/fleet'
// The primary checkout's branch needs quoting, so a pointer read without `quote` names no file.
const POINTER = `${COMMON}/orchestra/fix%2Fa%20b`
// A conversation of one message: a compaction runs over at least one and leaves at least one.
const MESSAGES = [{ role: 'user', text: 'the work so far', toolUses: [] }]
// The model's whole context, which `percent` is of, and the smaller compaction window the breakdown measures against.
const WINDOW = 1_000_000
const RAW = 500_000

type Seen = { commands: string[]; prompts: string[]; logs: string[] }
type Options = {
  pointed?: boolean
  stale?: boolean
  skip?: string
  git?: boolean
  drop?: string
  others?: boolean
  tokens?: number | null
  guarded?: boolean
  stays?: boolean
  rejects?: boolean
}

// Claude Code's answers for a checkout whose primary branch is `fix/a b`: `pointed` is whether that branch has a
// state, `stale` a pointer whose state folder is gone, `skip` the veto of the compaction `/compact` runs, `git`
// false where no git starts, `drop` a refusal of the mod's prompt, and `others` an agent's and a precompute
// compaction settling after the main one while `/compact` runs. `tokens` is the context's last reading, null for
// none; `guarded` runs `/compact` with no `session.compact` hook, as where the built-in guard loads, and `stays`
// keeps the context's reading after it; `rejects` makes `/compact` reject.
function stub(
  t: any,
  on: any,
  {
    pointed = true,
    stale = false,
    skip,
    git = true,
    drop,
    others = false,
    tokens = 400_000,
    guarded = false,
    stays = false,
    rejects = false,
  }: Options = {},
) {
  const clock = mock.clock(on)
  const seen: Seen = { commands: [], prompts: [], logs: [] }
  // Whether the test's turn end is in flight, during which Claude Code's `$.command.run` rejects.
  let ending = false
  // The context's reading, which a compaction that ran clears until the next response (SessionContextUsage).
  let live: number | undefined = tokens ?? undefined
  on('tool.register', () => ({ value: { tool: TOOL } }))
  on('session.start', () => ({ cwd: '/work' }))
  on('session.root', () => ({ value: '/work' }))
  on('process.run', ($: any, e: any) => {
    if (!git) return { deny: 'git: not found' }
    const listed = e.argv.includes('worktree')
    const stdout = listed
      ? 'worktree /work\nHEAD abc\nbranch refs/heads/fix/a b\n\nworktree /w2\nbranch refs/heads/b\n'
      : `${COMMON}\n`
    return { value: { exitCode: 0, stdout, stderr: '' } }
  })
  // A path reaches the stub made absolute, on Windows with a drive and backslashes, so its end is compared.
  const at = (path: string, wanted: string) => path.replaceAll('\\', '/').endsWith(wanted)
  on('fs.exists', ($: any, e: any) => ({ value: pointed && (at(e.path, POINTER) || (!stale && at(e.path, STATE))) }))
  on('fs.read', () => ({ value: `${STATE}\n` }))
  // The breakdown comes only when asked for, as `$.session.usage({ breakdown })` gives it.
  on('session.usage', ($: any, e: any) => ({
    value: {
      startedAt: 0,
      context: {
        tokens: live,
        window: WINDOW,
        percent: live === undefined ? undefined : Math.round((100 * live) / WINDOW),
        breakdown: e?.breakdown === undefined ? undefined : { rawMaxTokens: RAW, maxTokens: RAW },
      },
      rateLimits: [],
    },
  }))
  // The queued `/compact`, its compaction fired through the mod's `session.compact` hook as Claude Code's own is.
  on('command.run', async ($: any, e: any) => {
    // `$.command.run` "Rejects ... inside a hook the turn or the session's start waits on" (claude-code.d.ts). A
    // stub's `{ deny }` here resolves in the mod; one that throws makes the call reject, as Claude Code's does.
    if (ending || rejects) throw new Error('a command cannot run inside a hook the turn waits on')
    seen.commands.push(e.command)
    if (guarded) {
      if (!stays) live = undefined
      return { text: '' }
    }
    const result = await t.session.compact({ trigger: 'manual', messages: MESSAGES })
    if (result.skip === undefined) live = undefined
    if (others) {
      await t.session.compact({ trigger: 'auto', agentId: 'a1', messages: MESSAGES })
      await t.session.compact({ trigger: 'precompute', messages: MESSAGES })
    }
    return { text: '' }
  })
  // Only the main conversation's `/compact` is vetoed, so the others compact.
  on('session.compact', ($: any, e: any) =>
    skip !== undefined && e.trigger === 'manual' && e.agentId === undefined ? { skip } : { messages: MESSAGES },
  )
  on('prompt.submit', ($: any, e: any) => {
    seen.prompts.push(e.text)
    return drop === undefined ? { text: e.text } : { drop }
  })
  on('ui.log', ($: any, e: any) => {
    seen.logs.push(e.text)
    return { value: undefined }
  })
  on('turn.complete', () => ({ text: '' }))
  // Ends a turn as Claude Code does, waiting on the mod's `turn.complete` hook.
  const complete = async (reason = 'answer', agentId?: string) => {
    ending = true
    try {
      await t.turn.complete(end(reason, agentId))
    } finally {
      ending = false
    }
  }
  return { clock, seen, complete }
}

const end = (reason: string, agentId?: string) => ({
  turnId: 't1',
  answer: 'done',
  durationMs: 1,
  isAborted: reason === 'aborted',
  reason,
  ...(agentId === undefined ? {} : { agentId }),
})

// A prompt as Claude Code raises it, from `kind` (PromptOrigin).
const prompt = (kind: string) => ({ text: 'carry on', wait: false, origin: { kind } })

test("the main loop's call compacts once its turn ends, then wakes the session", async ($, on) => {
  const { clock, seen, complete } = stub($, on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  expect(await $.tool.call({ tool: TOOL })).toEqual(SCHEDULED)
  expect(seen.commands).toEqual([])
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual(['compact'])
  expect(seen.prompts).toEqual(['The compaction scheduled with the compact tool has run.'])
  // The next turn's end compacts nothing more: the request was the turn's that made it.
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual(['compact'])
  expect(seen.prompts).toHaveLength(1)
})

test('an agent is refused, and its turn end compacts nothing', async ($, on) => {
  const { clock, seen, complete } = stub($, on)
  const answer = await $.tool.call({ tool: TOOL, agentId: 'a1' })
  expect(answer).toEqual({ deny: `${TOOL} compacts the main conversation alone; agent a1 called it.` })
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual([])
})

const NO_STATE = {
  deny: `${TOOL} compacts an orchestra coordinator, and this session's checkout holds no orchestra session on its branch.`,
}

test('a session whose branch holds no orchestra state is refused', async ($, on) => {
  const { clock, seen, complete } = stub($, on, { pointed: false })
  expect(await $.tool.call({ tool: TOOL })).toEqual(NO_STATE)
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual([])
})

test("a pointer whose state folder is gone is refused as no state", async ($, on) => {
  const { clock, seen, complete } = stub($, on, { stale: true })
  expect(await $.tool.call({ tool: TOOL })).toEqual(NO_STATE)
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual([])
})

test('a check that cannot run git is refused with its reason, and schedules nothing', async ($, on) => {
  const { clock, seen, complete } = stub($, on, { git: false })
  const answer: any = await $.tool.call({ tool: TOOL })
  expect(answer.deny).toMatch(/^mcp__orchestra__compact could not check this session, so nothing was scheduled: .*git: not found/)
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual([])
})

test('a turn the owner interrupts drops the compaction and sends no prompt', async ($, on) => {
  const { clock, seen, complete } = stub($, on)
  await $.tool.call({ tool: TOOL })
  await complete('aborted')
  await clock.settle()
  expect(seen.commands).toEqual([])
  expect(seen.prompts).toEqual([])
  expect(seen.logs).toEqual(['the turn ended (aborted), so the compaction it scheduled was dropped'])
})

test("an agent's turn end leaves the main loop's request for the main loop's", async ($, on) => {
  const { clock, seen, complete } = stub($, on)
  await $.tool.call({ tool: TOOL })
  await complete('answer', 'a1')
  await clock.settle()
  expect(seen.commands).toEqual([])
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual(['compact'])
})

test('a compaction a hook vetoes still wakes the session, saying why', async ($, on) => {
  const { clock, seen, complete } = stub($, on, { skip: 'blocked by a PreCompact hook' })
  await $.tool.call({ tool: TOOL })
  await complete()
  await clock.settle()
  expect(seen.prompts).toEqual([
    'The compaction scheduled with the compact tool did not run: blocked by a PreCompact hook.',
  ])
})

test("an agent's or a precompute compaction during the queued /compact leaves the main one's outcome", async ($, on) => {
  const { clock, seen, complete } = stub($, on, { skip: 'blocked by a PreCompact hook', others: true })
  await $.tool.call({ tool: TOOL })
  await complete()
  await clock.settle()
  expect(seen.prompts).toEqual([
    'The compaction scheduled with the compact tool did not run: blocked by a PreCompact hook.',
  ])
})

test("after a compaction that did not run, the tool schedules nothing until the owner's next prompt", async ($, on) => {
  const { clock, seen, complete } = stub($, on, { skip: 'blocked by a PreCompact hook' })
  await $.tool.call({ tool: TOOL })
  await complete()
  await clock.settle()
  expect(await $.tool.call({ tool: TOOL })).toEqual(REFUSED)
  // An agent's report is no owner at the prompt.
  await $.prompt.submit(prompt('task-notification'))
  expect(await $.tool.call({ tool: TOOL })).toEqual(REFUSED)
  await $.prompt.submit(prompt('composer'))
  expect(await $.tool.call({ tool: TOOL })).toEqual(SCHEDULED)
  expect(seen.commands).toEqual(['compact'])
})

test('after a compaction that did not run, a main compaction that runs lets the tool schedule again', async ($, on) => {
  const { clock, complete } = stub($, on, { skip: 'blocked by a PreCompact hook' })
  await $.tool.call({ tool: TOOL })
  await complete()
  await clock.settle()
  // An agent's own compaction shows nothing of the main conversation's.
  await $.session.compact({ trigger: 'auto', agentId: 'a1', messages: MESSAGES })
  expect(await $.tool.call({ tool: TOOL })).toEqual(REFUSED)
  await $.session.compact({ trigger: 'auto', messages: MESSAGES })
  expect(await $.tool.call({ tool: TOOL })).toEqual(SCHEDULED)
})

test('a resume prompt Claude Code refuses leaves a line saying why', async ($, on) => {
  const { clock, seen, complete } = stub($, on, { drop: 'the run reached its --max-turns limit' })
  await $.tool.call({ tool: TOOL })
  await complete()
  await clock.settle()
  expect(seen.prompts).toEqual(['The compaction scheduled with the compact tool has run.'])
  expect(seen.logs).toEqual(["the compaction's resume prompt was refused: the run reached its --max-turns limit"])
})

test('below 60% of the compaction window, the tool schedules nothing and says when to call again', async ($, on) => {
  const { clock, seen, complete } = stub($, on, { tokens: 299_999 })
  expect(await $.tool.call({ tool: TOOL })).toEqual({
    result:
      'not needed yet: the context is 59% of the 500,000-token compaction window, below the 60% from which this' +
      ' tool compacts. Nothing was scheduled; call it again at a later seam.',
  })
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual([])
})

test('at 60% of the compaction window the tool schedules', async ($, on) => {
  const { clock, seen, complete } = stub($, on, { tokens: 300_000 })
  expect(await $.tool.call({ tool: TOOL })).toEqual(SCHEDULED)
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual(['compact'])
})

test("above 60% of the compaction window the tool schedules, though under 60% of the model's", async ($, on) => {
  // 400,000 tokens are 80% of the compaction window and 40% of the model's whole context, which `percent` is of.
  const { clock, seen, complete } = stub($, on, { tokens: 400_000 })
  expect(await $.tool.call({ tool: TOOL })).toEqual(SCHEDULED)
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual(['compact'])
})

test('a context with no reading schedules, as at a seam before the tool read one', async ($, on) => {
  const { clock, seen, complete } = stub($, on, { tokens: null })
  expect(await $.tool.call({ tool: TOOL })).toEqual(SCHEDULED)
  await complete()
  await clock.settle()
  expect(seen.commands).toEqual(['compact'])
})

test('a /compact that rejects wakes the session saying so, and stops the tool', async ($, on) => {
  const { clock, seen, complete } = stub($, on, { rejects: true })
  await $.tool.call({ tool: TOOL })
  await complete()
  await clock.settle()
  expect(seen.prompts).toHaveLength(1)
  expect(seen.prompts[0]).toMatch(/^The compaction scheduled with the compact tool failed: /)
  expect(await $.tool.call({ tool: TOOL })).toEqual(REFUSED)
})

test('where no session.compact hook runs, a context left with no reading has compacted', async ($, on) => {
  const { clock, seen, complete } = stub($, on, { guarded: true })
  await $.tool.call({ tool: TOOL })
  await complete()
  await clock.settle()
  expect(seen.prompts).toEqual(['The compaction scheduled with the compact tool has run.'])
})

test('where no session.compact hook runs and the context keeps its reading, the outcome is unknown and the tool stops', async ($, on) => {
  const { clock, seen, complete } = stub($, on, { guarded: true, stays: true })
  await $.tool.call({ tool: TOOL })
  await complete()
  await clock.settle()
  expect(seen.prompts).toEqual([
    'The compaction scheduled with the compact tool reported no outcome, so whether it ran is unknown.',
  ])
  // Under the built-in guard a veto ends here, so the tool stops as after one it saw.
  expect(await $.tool.call({ tool: TOOL })).toEqual(REFUSED)
})

test("the pointer's name is quoted as Python's quote(branch, safe='') quotes it", () => {
  // Each value printed by `python -c "from urllib.parse import quote; print(quote(branch, safe=''))"`, the
  // quoting `store.pointer` names its file with, for the branch beside it.
  const quoted = {
    'fix/a b': 'fix%2Fa%20b',
    'feature/x': 'feature%2Fx',
    "it's!(x)*": 'it%27s%21%28x%29%2A',
    'café/ü': 'caf%C3%A9%2F%C3%BC',
    'fix/a b!(x)*~é': 'fix%2Fa%20b%21%28x%29%2A~%C3%A9',
  }
  for (const [branch, name] of Object.entries(quoted)) expect(quote(branch)).toBe(name)
})
