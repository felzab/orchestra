// The plugin's mod: a tool the coordinator calls to compact its own conversation at a seam it chooses, since
// Claude Code lets no model run `/compact` (skills.md, "Restrict Claude's skill access") but lets a mod run it.
// `hooks.json` loads this file through `modules` (plugins/mods/reference.md, "Files"). Each `$` method used is
// cited from the type declarations of anthropics/claude-code `mods/types/claude-code.d.ts`, the reference that
// page names as complete.

// The registered name, which the model calls as `mcp__<plugin>__<name>` (plugins/mods/api.md, "Add a tool").
const NAME = 'compact'
const TOOL = 'mcp__orchestra__compact'
// The share of the compaction window from which the tool compacts. A seam loses least, since the state then holds
// everything in flight, and compacting lower costs less: replayed over one long coordinator day, compacting near
// 300,000 and 400,000 tokens cost 0.85 and 0.91 of compacting near 500,000. Below it, the summary's own cost
// outweighs what it saves; from it, a wave's growth still fits before Claude Code compacts on its own, about 33,000
// tokens short of the window. lib/orchestra/events.py's `COMPACTS_FROM` restates it, so change both together.
const THRESHOLD = 0.6
const DESCRIPTION =
  'Compacts this conversation as /compact does, once your current turn ends, then sends a one-line prompt so the' +
  ' session carries on. For the orchestra coordinator: call it at each natural seam, such as a wave’s end once the' +
  ` state holds everything in flight; it compacts only from ${THRESHOLD * 100}% of the compaction window and` +
  ' otherwise answers that no compaction is needed yet. Refused in an agent and in a session with no orchestra' +
  ' state. End your turn after a call that schedules one.'
// What the tool answers once the compaction is scheduled.
const SCHEDULED = 'scheduled: end your turn, and the conversation compacts once it has ended'
// `store.POINTERS`: the folder in git's common directory holding each branch's pointer to its state.
const POINTERS = 'orchestra'

// A compaction the main loop asked for in the running turn, run when that turn ends.
let pending = false
// While the queued `/compact` runs: the main conversation's compaction result, once one settles.
let running
// Set once a compaction the tool scheduled is not seen to have run, a veto, a rejected `/compact` or an unknown
// outcome, until the owner's next prompt or a main compaction the `session.compact` hook sees run; where the
// built-in guard loads that hook never runs, so only the prompt clears it. The resume prompt starts a turn, so a
// compaction that fails each time would repeat for as long as the model calls the tool again (plugins/mods/api.md,
// "A `claude -p` run with your mod never exits").
let failed = false
// The origins of a prompt that is the owner's own input (PromptOrigin): Enter at the terminal, the Remote Control
// bridge, the SDK host's own turn, and a ping from Slack.
const OWNER = new Set(['composer', 'bridge', 'sdk', 'slack-ping'])

// `store.pointer`'s quoting, Python's `quote(branch, safe="")`: every byte but RFC 3986's unreserved characters
// percent-encoded, which `encodeURIComponent` does once `!'()*` are encoded too (MDN, `encodeURIComponent`,
// "Encoding for RFC3986").
export function quote(branch) {
  return encodeURIComponent(branch).replace(/[!'()*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase())
}

// The state folder the primary checkout's branch names, as `store.state_dir` finds it; null where there is none.
// `config.checkout` takes the common directory from `git rev-parse` and the branch from the first worktree
// `git worktree list --porcelain` prints, which is the primary checkout's.
export async function stateOf($) {
  // `$.session.root`: "the session's project root, absolute", which a shell `cd` does not move.
  const cwd = await $.session.root()
  // `$.process.run`: "Runs a command on the host by its argument vector (no shell)"; "any exit code" resolves.
  const common = await $.process.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], { cwd })
  const listed = await $.process.run(['git', 'worktree', 'list', '--porcelain'], { cwd })
  if (common.exitCode !== 0 || listed.exitCode !== 0) return null
  const first = listed.stdout.split(/\r?\n\r?\n/)[0].split(/\r?\n/)
  const head = first.find((line) => line.startsWith('branch refs/heads/'))
  if (head === undefined) return null
  const pointer = `${common.stdout.trim()}/${POINTERS}/${quote(head.slice('branch refs/heads/'.length))}`
  // `$.fs.exists`: "Returns whether the path exists"; `$.fs.read` then returns the pointer's text.
  if (!(await $.fs.exists(pointer))) return null
  const state = (await $.fs.read(pointer)).trim()
  return state !== '' && (await $.fs.exists(state)) ? state : null
}

// The context in tokens and the compaction window it fills; null where either goes unread.
async function fill($) {
  // `percent` is of `window`, the model's whole context; the breakdown "measures against the compaction window
  // (`rawMaxTokens`), which may be smaller than `window`" (SessionContextUsage), and `summary` "estimates locally
  // and sends none" (SessionUsageArgs). `tokens` is the last response's input, as `context.size` reads it.
  const { context } = await $.session.usage({ breakdown: 'summary' })
  const window = context.breakdown?.rawMaxTokens
  return context.tokens === undefined || window === undefined ? null : { tokens: context.tokens, window }
}

// Runs the queued `/compact`, then wakes the session whatever came of it: the coordinator ended its turn to let
// the compaction run, and with no agent left to report, nothing else would start its next one.
async function compactThenResume($) {
  running = {}
  let said
  try {
    // `$.command.run`: "Runs a slash command as if the person typed `/command args`", "queued and run once the
    // session is idle".
    await $.command.run({ command: 'compact', args: '' })
    const outcome = running.outcome
    if (outcome?.skip !== undefined) {
      said = `The compaction scheduled with the compact tool did not run: ${outcome.skip}.`
      failed = true
    } else if (outcome !== undefined || (await $.session.usage()).context.tokens === undefined) {
      // Where Claude Code's built-in guard loads, no `session.compact` hook of this mod runs (plugins/mods/admin.md,
      // "Know which hooks of a user's mod don't run"). The context's `tokens` then tells: "absent until the first
      // response of the live window: a fresh session, or one just compacted" (SessionContextUsage).
      said = 'The compaction scheduled with the compact tool has run.'
    } else {
      // Where the guard loads, a veto ends here: no hook of this mod reports it, and `$.command.run` "Rejects an
      // unknown name, inside a hook the turn or the session's start waits on, past a Stop or a host's limit". A
      // reading from a turn started since would stop the tool after a compaction that ran, which costs only the seams
      // until the owner's next prompt, as Claude Code still compacts at the window.
      said = 'The compaction scheduled with the compact tool reported no outcome, so whether it ran is unknown.'
      failed = true
    }
  } catch (error) {
    said = `The compaction scheduled with the compact tool failed: ${error instanceof Error ? error.message : error}.`
    failed = true
  } finally {
    running = undefined
  }
  // `$.prompt.submit`: "a turn of its own, once the session is idle", read after a sentence naming this mod.
  const sent = await $.prompt.submit({ text: said })
  // A refused prompt "resolves with `{ drop: '<reason>' }` and queues no prompt" (plugins/mods/api.md, "What
  // refused calls return"), so the session stays idle; the engine's own reason is logged nowhere else.
  if (sent.drop !== undefined) $.ui.log(`the compaction's resume prompt was refused: ${sent.drop}`)
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    // `$.tool.register`: "Declares a tool the model can call from the next prompt on"; left out, `isDeferred`
    // puts it behind ToolSearch as an MCP server's tool is placed.
    await $.tool.register({
      name: NAME,
      description: DESCRIPTION,
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    })
    return next(e)
  })

  // Answered here, ahead of the permission check (`$.tool.register`: "at `tool.call`, ahead of the permission
  // check"), so an away session's call waits on no approval.
  on('tool.call', { tool: TOOL }, async ($, e) => {
    // `agentId`: "for a subagent or a teammate ...; absent on the main loop" (AgentLoop).
    if (e.agentId !== undefined) {
      return { deny: `${TOOL} compacts the main conversation alone; agent ${e.agentId} called it.` }
    }
    if (failed) {
      return {
        deny: `${TOOL} schedules nothing until the owner's next prompt or a compaction it sees run, since the one it last scheduled was not seen to run.`,
      }
    }
    if (pending) return { result: SCHEDULED }
    if ((await stateOf($)) === null) {
      return {
        deny: `${TOOL} compacts an orchestra coordinator, and this session's checkout holds no orchestra session on its branch.`,
      }
    }
    const read = await fill($)
    // A size that goes unread schedules, as the tool did before it read one: the seam is where to compact.
    if (read !== null && read.tokens < THRESHOLD * read.window) {
      const share = Math.floor((100 * read.tokens) / read.window)
      return {
        result:
          `not needed yet: the context is ${share}% of the ${read.window.toLocaleString('en-US')}-token compaction` +
          ` window, below the ${THRESHOLD * 100}% from which this tool compacts. Nothing was scheduled; call it` +
          ' again at a later seam.',
      }
    }
    pending = true
    return { result: SCHEDULED }
  }).catch(($, e, next) => ({
    // Without it a failed check, such as no `git` on the path, would leave the call to no hook, and it would fail
    // without saying why (`next.error`: "`kind` is `throw` or `timeout`, and `message` is the error's text").
    deny: `${TOOL} could not check this session, so nothing was scheduled: ${next.error.message}`,
  }))

  on('turn.complete', ($, e, next) => {
    if (e.agentId !== undefined || !pending) return next(e)
    pending = false
    // An interrupted turn has the owner at the prompt; an error or a refusal is no seam, and a prompt sent then
    // could start the same failure again.
    if (e.reason !== 'answer') {
      // `$.ui.log`: "A dim line in the transcript that Claude doesn't read" (plugins/mods/api.md).
      $.ui.log(`the turn ended (${e.reason}), so the compaction it scheduled was dropped`)
      return next(e)
    }
    // `$.command.run` rejects "inside a hook the turn or the session's start waits on", so it is queued from a
    // timer, which runs outside any event (`$.clock.after`: "Calls `fn` once after `ms` milliseconds").
    $.clock.after(0, () => {
      compactThenResume($).catch((error) => $.ui.log(`the compaction's resume failed: ${error}`))
    })
    return next(e)
  })

  // The owner's input lets the tool schedule again, as Claude Code refuses a mod's calls after an interruption
  // "until the user's next input" (plugins/mods/api.md, "Retry a refused or dropped call"). `prompt.submit` "Fires
  // when a prompt is submitted, before the turn starts", the mod's own resume prompt among them, whose `origin` is
  // a plugin's.
  on('prompt.submit', ($, e, next) => {
    if (OWNER.has(e.origin.kind)) failed = false
    return next(e)
  })

  // Records whether the queued `/compact` compacted the main conversation: `next(e)` resolves to the compaction or
  // to `{ skip }` (SessionCompactResult), and `precompute` installs nothing (SessionCompactTrigger).
  on('session.compact', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined && e.trigger !== 'precompute') {
      if (running !== undefined) running.outcome = result
      // Any main compaction that ran, the owner's `/compact` or Claude Code's own at the window, shows the next may.
      if (result.skip === undefined) failed = false
    }
    return result
  })
}
