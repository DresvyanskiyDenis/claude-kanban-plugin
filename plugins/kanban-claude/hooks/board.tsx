import type { EngineInterface, Register } from 'claude-code'

// A short board beside the transcript: what runs, what waits for review, what is next.
// This pane only reads `kanban --json list`.

const PANE = 'kanban'
const POLL_MS = 15_000
const SHOWN = 4

// Colors are theme keys, not hex, so the pane follows the person's light or dark theme.
type Group = { name: string; statuses: string[]; glyph: string; color: string }
const LANES: Group[] = [
  { name: 'Running', statuses: ['running'], glyph: '●', color: 'claude' },
  { name: 'Review', statuses: ['review'], glyph: '◐', color: 'merged' },
  { name: 'Blocked', statuses: ['blocked'], glyph: '✕', color: 'error' },
  { name: 'Up next', statuses: ['ready', 'todo'], glyph: '○', color: 'suggestion' },
]
const DONE: Group = { name: 'done', statuses: ['done'], glyph: '■', color: 'success' }
const TRIAGE: Group = { name: 'triage', statuses: ['triage'], glyph: '■', color: 'inactive' }
// The status bar: shipped on the left, the lanes, raw ideas on the right; archived cards stay out.
const BAR = [DONE, ...LANES, TRIAGE]

type Card = { id: string; title: string; status: string; assignee: string | null; priority: number; updated_at: number }
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

// Cells per segment, rounded on the running total so the segments always fill `width` exactly.
// ponytail: a status holding a sliver of a big board can round to no cell; its lane still lists it.
function segments(parts: { color: string; n: number }[], width: number) {
  const total = parts.reduce((a, p) => a + p.n, 0)
  let seen = 0
  let drawn = 0
  return parts.map(p => {
    seen += p.n
    const end = Math.round((seen / total) * width)
    const cells = end - drawn
    drawn = end
    return { color: p.color, cells }
  })
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
    const { Box, Text, Button } = $.ui.resolve(e)
    const width = e.props.bodyColumns
    const s = (snap ??= await load($, board))
    const controls = (
      <Box flexDirection="row" gap={2}>
        <Button key="refresh" label="refresh" hotkey="r" plain dimColor onPress={() => refresh($)} />
      </Box>
    )
    if ('error' in s) {
      return (
        <Box flexDirection="column" width={width}>
          <Box flexDirection="row" gap={1}>
            <Text color="warning">▲</Text>
            <Box flexShrink={1}>
              <Text color="warning">{s.error}</Text>
            </Box>
          </Box>
          <Text dimColor>/kanban-board SLUG pins a board.</Text>
          <Box marginTop={1}>{controls}</Box>
        </Box>
      )
    }
    const now = (await $.clock.now()) / 1000
    const of = (g: Group) => g.statuses.flatMap(st => s.cards.filter(c => c.status === st))
    const lanes = LANES.map(l => ({ ...l, cards: of(l) })).filter(l => l.cards.length > 0)
    const parts = BAR.map(g => ({ color: g.color, n: of(g).length }))
    const total = parts.reduce((a, p) => a + p.n, 0)
    // One badge column wide enough for every priority shown, so the titles line up.
    const shown = lanes.flatMap(l => l.cards.slice(0, SHOWN))
    const badge = Math.max(0, ...shown.map(c => (c.priority > 0 ? `P${c.priority}`.length : 0)))
    return (
      <Box flexDirection="column" width={width}>
        <Box flexDirection="row" justifyContent="space-between" gap={1}>
          <Box flexDirection="row" gap={1} flexShrink={1}>
            <Text color="claude">◆</Text>
            <Text bold wrap="truncate-end">{s.board}</Text>
          </Box>
          <Text dimColor>{`${total - of(DONE).length} open`}</Text>
        </Box>
        {total > 0 && (
          <Box flexDirection="row">
            {segments(parts, width).map(seg => seg.cells > 0 && <Text color={seg.color}>{'━'.repeat(seg.cells)}</Text>)}
          </Box>
        )}
        {lanes.length === 0 && <Text dimColor>Nothing running or planned.</Text>}
        {lanes.map(l => (
          <Box key={l.name} flexDirection="column" borderStyle="round" borderColor={l.color} paddingX={1}>
            <Box flexDirection="row" justifyContent="space-between">
              <Box flexDirection="row" gap={1}>
                <Text color={l.color}>{l.glyph}</Text>
                <Text bold color={l.color}>{l.name}</Text>
              </Box>
              <Text dimColor>{String(l.cards.length)}</Text>
            </Box>
            {l.cards.slice(0, SHOWN).map(c => (
              <Box key={c.id} flexDirection="row" gap={1}>
                {badge > 0 && (
                  <Box width={badge} flexShrink={0}>
                    {c.priority > 0 && <Text bold color="warning">{`P${c.priority}`}</Text>}
                  </Box>
                )}
                <Box flexGrow={1} flexShrink={1}>
                  <Text wrap="truncate-end">{c.title}</Text>
                </Box>
                <Box flexShrink={0}>
                  <Text dimColor>{[c.assignee && `@${c.assignee}`, ago(now - c.updated_at)].filter(Boolean).join(' · ')}</Text>
                </Box>
              </Box>
            ))}
            {l.cards.length > SHOWN && <Text dimColor italic>{`+${l.cards.length - SHOWN} more`}</Text>}
          </Box>
        ))}
        <Box flexDirection="row" justifyContent="space-between">
          <Box flexDirection="row" gap={2}>
            {[DONE, TRIAGE].map(g => (
              <Box key={g.name} flexDirection="row" gap={1}>
                <Text color={g.color}>{g.glyph}</Text>
                <Text dimColor>{`${g.name} ${of(g).length}`}</Text>
              </Box>
            ))}
          </Box>
          {controls}
        </Box>
      </Box>
    )
  })
}
