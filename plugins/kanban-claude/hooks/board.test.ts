import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const card = (id: string, status: string, title: string, assignee: string | null = null, priority = 0) => ({
  id,
  title,
  status,
  assignee,
  priority,
  updated_at: 0,
})

const kanban = (on: On, exitCode: number, stdout: string, stderr = '') => {
  const runs: string[][] = []
  mock.clock(on, { now: 300_000 })
  on('session.cwd', () => ({ value: '/work' }))
  on('process.run', (_$, e) => {
    runs.push([...e.argv])
    return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return runs
}

const pane = {
  plugin: 'kanban-claude',
  component: 'Pane' as const,
  requestId: 'kanban',
  surface: 'terminal' as const,
  props: { title: 'Kanban', isFocused: false, bodyColumns: 48, placement: 'dock' as const, scroll: { offset: 0, bodyRows: 40 }, view: {} },
}

const cards = [
  card('t_1', 'running', 'Fix login', 'builder'),
  card('t_2', 'review', 'Add retries', 'warden'),
  ...['a', 'b', 'c', 'd', 'e'].map((x, i) => card(`t_r${x}`, 'ready', `Ready ${x}`, null, i === 0 ? 2 : 0)),
  card('t_3', 'triage', 'Raw idea'),
  card('t_4', 'done', 'Shipped'),
]

test('draws the open lanes, caps each at four, and counts triage and done', async ($, on) => {
  kanban(on, 0, JSON.stringify({ schema: 1, board: 'main', cards }))
  const ui = await $.ui.mount(pane)
  expect(await ui.find({ text: /^main$/ })).toBeDefined()
  expect(await ui.find({ text: /^8 open$/ })).toBeDefined()
  expect(await ui.find({ text: /^Running$/ })).toBeDefined()
  expect(await ui.find({ text: /^@builder · 5m$/ })).toBeDefined()
  expect(await ui.find({ text: /^Up next$/ })).toBeDefined()
  expect(await ui.find({ text: /^5$/ })).toBeDefined()
  expect(await ui.find({ text: /^P2$/ })).toBeDefined()
  expect(await ui.find({ text: /^\+1 more$/ })).toBeDefined()
  expect(await ui.find({ text: /^Blocked/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /^done 1$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^triage 1$/ })).toBeDefined()
  await ui.unmount()
})

test('the status bar spans the pane, one segment per status that has cards', async ($, on) => {
  kanban(on, 0, JSON.stringify({ schema: 1, board: 'main', cards }))
  const ui = await $.ui.mount(pane)
  const bar = await ui.findAll({ type: 'Text', text: /^━+$/ })
  expect(bar.length).toBe(5)
  expect(bar.reduce((n, seg) => n + seg.text.length, 0)).toBe(48)
  await ui.unmount()
})

test('r reloads the board', async ($, on) => {
  const runs = kanban(on, 0, JSON.stringify({ schema: 1, board: 'main', cards }))
  const ui = await $.ui.mount(pane)
  const before = runs.length
  await $.ui.press({ plugin: 'kanban-claude', key: 'refresh' })
  expect(runs.length).toBe(before + 1)
  await ui.unmount()
})

test('a folder with no board shows why, not an empty pane', async ($, on) => {
  kanban(on, 2, '', "kanban: no board: pass --board SLUG, set KANBAN_BOARD, or run inside a board's default workdir\n")
  const ui = await $.ui.mount(pane)
  expect(await ui.find({ text: /^kanban: no board/ })).toBeDefined()
  await ui.unmount()
})
