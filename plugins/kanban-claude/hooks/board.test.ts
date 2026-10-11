import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const card = (id: string, status: string, title: string, assignee: string | null = null) => ({
  id,
  title,
  status,
  assignee,
  updated_at: 0,
})

const kanban = (on: On, exitCode: number, stdout: string, stderr = '') => {
  mock.clock(on, { now: 300_000 })
  on('session.cwd', () => ({ value: '/work' }))
  on('process.run', () => ({ value: { exitCode, stdout, stderr } }))
}

const pane = {
  plugin: 'kanban-claude',
  component: 'Pane' as const,
  requestId: 'kanban',
  surface: 'terminal' as const,
  props: { title: 'Kanban', isFocused: false, bodyColumns: 48, placement: 'dock' as const, scroll: { offset: 0, bodyRows: 40 }, view: {} },
}

test('draws the open lanes, caps each at four, and counts triage and done', async ($, on) => {
  const cards = [
    card('t_1', 'running', 'Fix login', 'builder'),
    card('t_2', 'review', 'Add retries', 'warden'),
    ...['a', 'b', 'c', 'd', 'e'].map(x => card(`t_r${x}`, 'ready', `Ready ${x}`)),
    card('t_3', 'triage', 'Raw idea'),
    card('t_4', 'done', 'Shipped'),
  ]
  kanban(on, 0, JSON.stringify({ schema: 1, board: 'main', cards }))
  const ui = await $.ui.mount(pane)
  expect(await ui.find({ text: /^main$/ })).toBeDefined()
  expect(await ui.find({ text: /^Running 1$/ })).toBeDefined()
  expect(await ui.find({ text: /^@builder 5m$/ })).toBeDefined()
  expect(await ui.find({ text: /^Up next 5$/ })).toBeDefined()
  expect(await ui.find({ text: /^\+1 more$/ })).toBeDefined()
  expect(await ui.find({ text: /^Blocked/ })).toBeUndefined()
  expect(await ui.find({ text: /^triage 1 · done 1$/ })).toBeDefined()
  await ui.unmount()
})

test('a folder with no board shows why, not an empty pane', async ($, on) => {
  kanban(on, 2, '', "kanban: no board: pass --board SLUG, set KANBAN_BOARD, or run inside a board's default workdir\n")
  const ui = await $.ui.mount(pane)
  expect(await ui.find({ text: /^kanban: no board/ })).toBeDefined()
  await ui.unmount()
})
