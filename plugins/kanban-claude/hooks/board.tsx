import type { EngineInterface, Register } from 'claude-code'

// A short board beside the transcript: what runs, what waits for review, what is next.
// The web board stays the full view; this pane only reads `kanban --json list`.

const PANE = 'kanban'
const POLL_MS = 15_000
const SHOWN = 4

const LANES: [string, string[]][] = [
  ['Running', ['running']],
  ['Review', ['review']],
  ['Blocked', ['blocked']],
  ['Up next', ['ready', 'todo']],
]

type Card = { id: string; title: string; status: string; assignee: string | null; updated_at: number }
type Snapshot = { board: string; cards: Card[] } | { error: string }
// Kept in $.store across sessions while the person leaves the pane open; `board` is a pinned slug.
type Saved = { board: string | null }

async function load($: EngineInterface, board: string | null): Promise<Snapshot> {
  const argv = ['kanban', ...(board ? ['--board', board] : []), '--json', 'list', '--sort', 'priority']
  try {
    const r = await $.process.run(argv, { cwd: await $.session.cwd(), timeoutMs: 5_000 })
    if (r.exitCode !== 0) return { error: r.stderr.trim() || `kanban exited ${r.exitCode}` }
    const out = JSON.parse(r.stdout)
    return { board: out.board, cards: out.cards }
  } catch (err) {
    return { error: `kanban did not run: ${err instanceof Error ? err.message : String(err)}` }
  }
}

function ago(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

// ponytail: one pane per module instance; per-session atoms if a host ever shares a module across sessions.
let board: string | null = null
let snap: Snapshot | null = null
let timer: { cancel: () => void } | null = null

async function refresh($: EngineInterface) {
  snap = await load($, board)
  $.ui.invalidate('ui.render')
}

async function show($: EngineInterface) {
  await refresh($)
  timer ??= $.clock.every(POLL_MS, () => void refresh($))
  return $.ui.open({ id: PANE, title: 'Kanban', columns: 48, rows: 8 })
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // A card's own headless worker has no one to show a board to.
    if (await $.env.get('KANBAN_RUN_ID')) return next(e)
    const saved = (await $.store.get('pane')) as Saved | undefined
    if (saved) {
      board = saved.board
      void show($).catch(() => undefined)
    }
    await $.command.register({
      name: 'kanban-board',
      description: 'Show the kanban board beside the transcript, or close it',
      argumentHint: '[BOARD-SLUG|close]',
    })
    return next(e)
  })

  on('command.run', { command: 'kanban-board' }, async ($, e) => {
    const arg = e.args.trim()
    if (arg === 'close') {
      await $.ui.close({ id: PANE })
      return { text: 'Kanban pane closed.' }
    }
    board = arg || null
    await $.store.set('pane', { board } satisfies Saved)
    const opened = await show($)
    return { text: opened.isPlaced ? 'Kanban pane open.' : `Kanban pane not shown: ${opened.reason}` }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) {
      timer?.cancel()
      timer = null
      if (e.origin.kind !== 'unload') await $.store.delete('pane').catch(() => undefined)
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const s = (snap ??= await load($, board))
    if ('error' in s) {
      return (
        <Box flexDirection="column" width={e.props.bodyColumns}>
          <Text color="yellow">{s.error}</Text>
          <Text dimColor>/kanban-board SLUG pins a board.</Text>
        </Box>
      )
    }
    const now = (await $.clock.now()) / 1000
    const count = (status: string) => s.cards.filter(c => c.status === status).length
    const lanes = LANES.map(([name, statuses]) => ({
      name,
      cards: statuses.flatMap(st => s.cards.filter(c => c.status === st)),
    })).filter(l => l.cards.length > 0)
    return (
      <Box flexDirection="column" width={e.props.bodyColumns}>
        <Text bold wrap="truncate-end">{s.board}</Text>
        {lanes.length === 0 && <Text dimColor>Nothing running or planned.</Text>}
        {lanes.map(l => (
          <Box key={l.name} flexDirection="column" marginTop={1}>
            <Text bold>{`${l.name} ${l.cards.length}`}</Text>
            {l.cards.slice(0, SHOWN).map(c => (
              <Box key={c.id} flexDirection="row" gap={1}>
                <Box flexGrow={1} flexShrink={1}>
                  <Text wrap="truncate-end">{c.title}</Text>
                </Box>
                <Text dimColor>{`${c.assignee ? `@${c.assignee} ` : ''}${ago(now - c.updated_at)}`}</Text>
              </Box>
            ))}
            {l.cards.length > SHOWN && <Text dimColor>{`+${l.cards.length - SHOWN} more`}</Text>}
          </Box>
        ))}
        <Box marginTop={1}>
          <Text dimColor>{`triage ${count('triage')} · done ${count('done')}`}</Text>
        </Box>
      </Box>
    )
  })
}
