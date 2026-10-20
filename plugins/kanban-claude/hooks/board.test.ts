import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const NOW = 1_800_000_000_000
const S = NOW / 1000
const VERBS = ['list', 'digest', 'show'] as const
type Verb = (typeof VERBS)[number]
type Ran = { exitCode: number; stdout: string; stderr: string }
type Answer = (argv: readonly string[]) => Ran | 'spawn-error' | Promise<Ran | 'spawn-error'>

const ok = (v: unknown): Ran => ({ exitCode: 0, stdout: JSON.stringify(v), stderr: '' })
const fail = (stderr: string, exitCode = 2): Ran => ({ exitCode, stdout: '', stderr: `${stderr}\n` })
const boardOf = (argv: readonly string[]) => (argv.includes('--board') ? argv[argv.indexOf('--board') + 1] : 'main')

const card = (id: string, status: string, title: string, extra: object = {}) => ({ id, title, status, assignee: null, priority: 0, updated_at: S - 60, ...extra })
const CARDS = [
  card('t_run', 'running', 'Fix login', { assignee: 'builder', updated_at: S - 300 }),
  card('t_rev', 'review', 'Add retries', { assignee: 'warden', pr_number: 42, updated_at: S - 7200 }),
  card('t_blk', 'blocked', 'Wire SSO', { block_kind: 'needs_input' }),
  card('t_rdy', 'ready', 'Ready a', { priority: 2, skip: { reason: 'overlaps:t_run', at: S } }),
  card('t_todo', 'todo', 'Todo b', { assignee: 'scout' }),
  card('t_tri', 'triage', 'Raw idea'),
  card('t_done', 'done', 'Shipped'),
]
const NEED = { id: 5, task_id: 't_blk', kind: 'blocked', at: S - 120, title: 'Wire SSO', payload: { kind: 'needs_input' }, reason: 'Which IdP?' }
const QUOTA = { id: 4, task_id: null, kind: 'quota_wall', at: S - 60, title: null, payload: { kind: 'hard', cards: 3, until: S + 3600 } }
const DIGEST = {
  needs_you: [NEED, QUOTA],
  merged: [{ id: 3, task_id: 't_old', kind: 'merged', at: S - 600, title: 'Old fix', payload: {} }],
  cost_usd: 1.234,
  daemon: { alive: true, version: '0.8.0' },
  paused: null,
}
const SHOW = {
  task: { id: 't_rev', title: 'Add retries', status: 'review', assignee: 'warden', branch_name: 'kanban/t_rev', base_branch: 'main', pr_number: 42, pr_url: 'https://example.test/pr/42' },
  runs: [{ role: 'smith', status: 'done', outcome: 'pr_opened', cost_usd: 0.5, started_at: S - 900, ended_at: S - 600 }],
  latest_summary: 'Opened PR #42 with retries on the HTTP client.',
  comments: [{ author: 'owner', body: 'Keep the backoff short.', created_at: S - 3600 }],
}

type World = {
  answers: Partial<Record<Verb, Answer>>
  cwd: string
  panes: string[]
  store: Map<string, unknown>
  runs: string[][]
  opened: string[]
  closed: string[]
  toasts: string[]
  clk: ReturnType<typeof mock.clock>
  count: (verb: Verb) => number
}

// The engine beneath the plugin: a clock, a store we can read back, the pane calls, and `kanban` routed on its verb.
function world(on: On, answers: Partial<Record<Verb, Answer>> = {}, store: Record<string, unknown> = {}, env: Record<string, string> = {}): World {
  const w: World = {
    answers: { list: argv => ok({ schema: 1, board: boardOf(argv), cards: CARDS }), digest: () => ok(DIGEST), show: () => ok(SHOW), ...answers },
    cwd: '/work',
    panes: [],
    store: new Map(Object.entries(store)),
    runs: [],
    opened: [],
    closed: [],
    toasts: [],
    clk: mock.clock(on, { now: NOW }),
    count: verb => w.runs.filter(a => a.includes(verb)).length,
  }
  mock.env(on, { HOME: '/home/me', ...env })
  on('session.cwd', () => ({ value: w.cwd }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: { command: 'kanban-board' } }))
  on('store.get', (_$, e) => ({ value: w.store.get(e.key) }))
  on('store.set', (_$, e) => {
    w.store.set(e.key, e.value)
    return { value: undefined }
  })
  on('store.delete', (_$, e) => {
    w.store.delete(e.key)
    return { value: undefined }
  })
  on('ui.open', (_$, e) => {
    w.opened.push(e.id)
    return { value: { isPlaced: true } }
  })
  on('ui.close', (_$, e) => {
    w.closed.push(e.id)
    return { value: undefined }
  })
  on('ui.panes', () => ({ value: w.panes.map(id => ({ id, title: 'Kanban', isShown: true, isFocused: false, isPlaced: true })) }))
  on('ui.toast', (_$, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  on('process.run', async (_$, e, next) => {
    w.runs.push([...e.argv])
    const verb = VERBS.find(v => e.argv.includes(v))
    const r = verb && w.answers[verb] ? await w.answers[verb](e.argv) : fail('unexpected command')
    // Nothing answers beneath the test's hook, so passing the call on is a command that cannot start.
    if (r === 'spawn-error') return next(e)
    return { value: { ...r, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return w
}

const pane = (props: { bodyColumns?: number; placement?: 'dock' | 'inline'; bodyRows?: number } = {}) => ({
  plugin: 'kanban-claude',
  component: 'Pane' as const,
  requestId: 'kanban',
  surface: 'terminal' as const,
  props: { title: 'Kanban', isFocused: false, bodyColumns: props.bodyColumns ?? 48, placement: props.placement ?? 'dock', scroll: { offset: 0, bodyRows: props.bodyRows ?? 40 }, view: {} },
})

const command = (args = '') => ({ command: 'kanban-board', args, origin: { kind: 'composer' as const }, presentation: { isFullscreen: true, columns: 170 } })
const start = (cwd = '/work') => ({ cwd, surface: 'terminal' as const, isInteractive: true })

test('the dock draws the lanes with their meta, the bar and the legend', async ($, on) => {
  world(on)
  await $.command.run(command())
  const ui = await $.ui.mount(pane())
  expect(await ui.find({ type: 'Text', text: /^main$/ })).toBeDefined()
  for (const name of ['Running', 'Review', 'Blocked', 'Up next']) expect(await ui.find({ type: 'Text', text: name })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /builder · 5m$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /#42 · 2h$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /needs input$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /overlaps t_run$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^P2 $/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^done 1$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^triage 1$/ })).toBeDefined()
  const bar = await ui.findAll({ type: 'Text', text: /^━+$/ })
  expect(bar.reduce((n, seg) => n + String(seg.props.children ?? seg.text).length, 0)).toBe(48)
  // Every row line is exactly the pane's width, so the columns line up.
  const row = await ui.find({ type: 'Button', key: 'card:t_run' })
  expect(row).toBeDefined()
  expect(row?.text.length).toBe(48)
  await ui.unmount()
})

test('the needs-you strip leads with what waits on the owner and how to answer it', async ($, on) => {
  world(on)
  await $.command.run(command())
  const ui = await $.ui.mount(pane())
  expect(await ui.find({ type: 'Text', text: /^Needs you$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^question/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Which IdP\? → kanban unblock/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /hard quota wall · 3 cards wait/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /lifts by itself in 1h/ })).toBeDefined()
  await ui.unmount()
})

test('the health header says live, paused or daemon off, and the spend', async ($, on) => {
  let digest: object = DIGEST
  const w = world(on, { digest: () => ok(digest) })
  await $.command.run(command())
  const ui = await $.ui.mount(pane())
  expect(await ui.find({ type: 'Text', text: '● live' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '$1.23 24h' })).toBeDefined()
  digest = { ...DIGEST, paused: { reason: 'quota', detail: 'until 12:40' } }
  await ui.press({ key: 'refresh' })
  expect(await ui.find({ type: 'Text', text: '‖ paused' })).toBeDefined()
  digest = { ...DIGEST, daemon: { alive: false, version: null } }
  await ui.press({ key: 'refresh' })
  expect(await ui.find({ type: 'Text', text: '○ daemon off' })).toBeDefined()
  expect(w.count('digest')).toBe(3)
  await ui.unmount()
})

test('drawing only reads state: a pane with nothing read yet runs nothing', async ($, on) => {
  const w = world(on)
  const ui = await $.ui.mount(pane())
  expect(await ui.find({ type: 'Text', text: 'Reading the board…' })).toBeDefined()
  await ui.redraw()
  await w.clk.settle()
  expect(w.runs).toHaveLength(0)
  await ui.unmount()
})

test('refreshes run single flight: a second r joins the read already out', async ($, on) => {
  const w = world(on)
  w.answers.list = async argv => {
    await w.clk.sleep(1_000)
    return ok({ schema: 1, board: boardOf(argv), cards: CARDS })
  }
  const ui = await $.ui.mount(pane())
  const first = ui.press({ key: 'refresh' })
  await w.clk.settle()
  const second = ui.press({ key: 'refresh' })
  await w.clk.settle()
  await w.clk.advance(1_000)
  await Promise.all([first, second])
  expect(w.count('list')).toBe(1)
  expect(await ui.find({ type: 'Text', text: /^main$/ })).toBeDefined()
  await ui.unmount()
})

test('a board pinned while a read is out is the one that lands', async ($, on) => {
  const w = world(on)
  w.answers.list = async argv => {
    if (boardOf(argv) === 'alpha') await w.clk.sleep(1_000)
    return ok({ schema: 1, board: boardOf(argv), cards: CARDS })
  }
  const a = $.command.run(command('alpha'))
  await w.clk.settle()
  const b = $.command.run(command('beta'))
  await w.clk.settle()
  await w.clk.advance(1_000)
  await Promise.all([a, b])
  const ui = await $.ui.mount(pane())
  expect(await ui.find({ type: 'Text', text: /^beta$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^alpha$/ })).toBeUndefined()
  expect(w.runs.filter(r => r.includes('list')).map(boardOf)).toEqual(['alpha', 'beta'])
  await ui.unmount()
})

test('a failed refresh keeps the last good board and says how stale it is', async ($, on) => {
  const w = world(on)
  await $.command.run(command())
  w.answers.list = () => fail('kanban: database is locked', 1)
  await w.clk.advance(30_000)
  const ui = await $.ui.mount(pane())
  expect(await ui.find({ type: 'Text', text: /^main$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '△ stale 30s' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'kanban: database is locked' })).toBeDefined()
  w.answers.list = argv => ok({ schema: 1, board: boardOf(argv), cards: CARDS })
  await ui.press({ key: 'refresh' })
  expect(await ui.find({ type: 'Text', text: /^△ stale/ })).toBeUndefined()
  await ui.unmount()
})

test('a folder with no board shows why and how to pin one', async ($, on) => {
  world(on, { list: () => fail("kanban: no board: pass --board SLUG, set KANBAN_BOARD, or run inside a board's default workdir") })
  await $.command.run(command())
  const ui = await $.ui.mount(pane())
  expect(await ui.find({ type: 'Text', text: /^kanban: no board/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '/kanban-board SLUG pins a board.' })).toBeDefined()
  await ui.unmount()
})

test('a reload with the pane up polls once per period, not once per start', async ($, on) => {
  const w = world(on)
  w.panes = ['kanban']
  await $.session.start(start())
  await $.session.start(start())
  await w.clk.settle()
  w.runs.length = 0
  await w.clk.advance(15_000)
  expect(w.count('list')).toBe(1)
  expect(w.count('digest')).toBe(1)
})

test('the reopen key is per folder, and a folder whose board does not read stays closed', async ($, on) => {
  const w = world(on, {}, { pane: { pin: null } })
  await $.command.run(command())
  expect(w.store.get('pane:/work')).toEqual({ pin: null })
  expect(w.opened).toEqual(['kanban'])

  w.opened.length = 0
  w.runs.length = 0
  w.cwd = '/elsewhere'
  await $.session.start(start('/elsewhere'))
  await w.clk.settle()
  expect(w.store.has('pane')).toBe(false)
  expect(w.opened).toEqual([])
  expect(w.runs).toHaveLength(0)

  w.cwd = '/work'
  w.answers.list = () => fail('kanban: no board: pass --board SLUG')
  await $.session.start(start())
  await w.clk.settle()
  expect(w.opened).toEqual([])

  await $.command.run(command('close'))
  expect(w.closed).toEqual(['kanban'])
  expect(w.store.has('pane:/work')).toBe(false)
})

test('a saved pane reopens where its board reads', async ($, on) => {
  const w = world(on, {}, { 'pane:/work': { pin: 'main' } })
  await $.session.start(start())
  await w.clk.settle()
  expect(w.opened).toEqual(['kanban'])
  expect(w.runs[0]).toEqual(['kanban', '--board', 'main', '--json', 'list', '--sort', 'priority'])
})

test('a kanban that cannot start from PATH is found in ~/.local/bin', async ($, on) => {
  const w = world(on)
  const list = w.answers.list as Answer
  w.answers.list = argv => (argv[0] === 'kanban' ? 'spawn-error' : list(argv))
  w.answers.digest = argv => (argv[0] === 'kanban' ? 'spawn-error' : ok(DIGEST))
  await $.command.run(command())
  const ui = await $.ui.mount(pane())
  expect(await ui.find({ type: 'Text', text: /^main$/ })).toBeDefined()
  expect(w.runs.some(r => r[0] === '/home/me/.local/bin/kanban')).toBe(true)
  await ui.unmount()
})

test('a kanban that times out is not retried from ~/.local/bin', async ($, on) => {
  const w = world(on)
  w.answers.list = async () => {
    await w.clk.sleep(10_000)
    return 'spawn-error' as const
  }
  const run = $.command.run(command())
  await w.clk.settle()
  await w.clk.advance(10_000)
  await run
  expect(w.runs.some(r => r[0] === '/home/me/.local/bin/kanban' && r.includes('list'))).toBe(false)
})

test('an older CLI without digest still draws the board and hides the rest', async ($, on) => {
  const old = CARDS.map(({ id, title, status, assignee, priority, updated_at }) => ({ id, title, status, assignee, priority, updated_at }))
  world(on, {
    list: () => ok({ schema: 1, board: 'main', cards: old }),
    digest: () => fail("kanban: error: argument command: invalid choice: 'digest'"),
  })
  await $.command.run(command())
  const ui = await $.ui.mount(pane())
  expect(await ui.find({ type: 'Text', text: /^main$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Fix login/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '6 open' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^Needs you$/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /^Merged$/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /invalid choice/ })).toBeUndefined()
  await ui.unmount()
})

test('a row opens the card, and b goes back to the board', async ($, on) => {
  const w = world(on)
  await $.command.run(command())
  const ui = await $.ui.mount(pane())
  await ui.press({ key: 'card:t_rev' })
  expect(w.runs.some(r => r.join(' ') === 'kanban --json show t_rev')).toBe(true)
  expect(await ui.find({ type: 'Text', text: 'Add retries' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'kanban/t_rev → main' })).toBeDefined()
  expect(await ui.find({ type: 'Link', text: '#42' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'smith · pr_opened · $0.50 · 10m ago' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Opened PR #42/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'Keep the backoff short.' })).toBeDefined()
  await ui.press({ key: 'back' })
  expect(await ui.find({ type: 'Text', text: /^Running$/ })).toBeDefined()
  await ui.unmount()
})

test('recently merged cards close the dock', async ($, on) => {
  world(on)
  await $.command.run(command())
  const ui = await $.ui.mount(pane())
  expect(await ui.find({ type: 'Text', text: /^Merged$/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'merged:t_old:' + String(S - 600) })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /10m$/ })).toBeDefined()
  await ui.unmount()
})

test('inline and wide, the lanes sit side by side; narrow, the meta column goes', async ($, on) => {
  world(on)
  await $.command.run(command())
  const wide = await $.ui.mount(pane({ placement: 'inline', bodyColumns: 120, bodyRows: 12 }))
  const run = await wide.find({ type: 'Button', key: 'card:t_run' })
  expect(run?.text.length).toBe(Math.floor((120 - 2 * 3) / 4))
  expect(await wide.find({ type: 'Text', text: /Which IdP\?/ })).toBeDefined()
  await wide.unmount()
  const narrow = await $.ui.mount(pane({ bodyColumns: 26 }))
  expect(await narrow.find({ type: 'Text', text: /builder/ })).toBeUndefined()
  expect((await narrow.find({ type: 'Button', key: 'card:t_run' }))?.text.length).toBe(26)
  await narrow.unmount()
})

test('a new need toasts once; what was there at the first read never replays', async ($, on) => {
  let digest: object = DIGEST
  const w = world(on, { digest: () => ok(digest) })
  await $.command.run(command())
  expect(w.toasts).toEqual([])
  digest = { ...DIGEST, needs_you: [...DIGEST.needs_you, { ...NEED, id: 9, task_id: 't_rev', title: 'Add retries', payload: { kind: 'capability' } }] }
  await w.clk.advance(15_000)
  await w.clk.advance(15_000)
  expect(w.toasts).toEqual(['Kanban · grant: Add retries'])
})

test("a card's own headless worker gets no board", async ($, on) => {
  const w = world(on, {}, {}, { KANBAN_RUN_ID: 'r_1' })
  w.panes = ['kanban']
  await $.session.start(start())
  await w.clk.settle()
  expect(w.runs).toHaveLength(0)
})
