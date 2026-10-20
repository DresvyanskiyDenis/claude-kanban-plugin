import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { KanbanCard, KanbanDetail, KanbanDigest, KanbanFacts, KanbanNeed, KanbanSnapshot } from '../types'

// The board beside the transcript: what needs the owner, what runs, waits for review or comes next.
// Hooks read `kanban --json list|digest|show` into $.state; the render hook only reads that state.

const PANE = 'kanban'
const POLL_MS = 15_000
const RUN_MS = 10_000
// process.run rejects both when the binary cannot start and when it times out: only a quick rejection means "not found".
const SPAWN_MS = 1_000
const SHOWN = 5
const LABEL = 10

const SNAP = atom({ plugin: 'kanban-claude', key: 'snapshot' } as const, null)
const FAULT = atom({ plugin: 'kanban-claude', key: 'fault' } as const, null)
const PIN = atom({ plugin: 'kanban-claude', key: 'pin' } as const, null)
const OPEN = atom({ plugin: 'kanban-claude', key: 'open' } as const, null)
const DETAIL = atom({ plugin: 'kanban-claude', key: 'detail' } as const, null)
const SEEN = atom({ plugin: 'kanban-claude', key: 'seen' } as const, null)

// Colors are theme keys, not hex, so the pane follows the person's theme. One accent per lane.
type Lane = { key: string; name: string; statuses: string[]; glyph: string; color: string; meta: (c: KanbanCard, now: number) => string }
const LANES: Lane[] = [
  { key: 'running', name: 'Running', statuses: ['running'], glyph: '●', color: 'claude', meta: (c, now) => join(' · ', c.assignee, ago(now - c.updated_at)) },
  { key: 'review', name: 'Review', statuses: ['review'], glyph: '◐', color: 'merged', meta: (c, now) => join(' · ', c.pr_number ? `#${c.pr_number}` : null, ago(now - c.updated_at)) },
  { key: 'blocked', name: 'Blocked', statuses: ['blocked'], glyph: '✕', color: 'error', meta: c => words(c.block_kind) },
  { key: 'next', name: 'Up next', statuses: ['ready', 'todo'], glyph: '○', color: 'suggestion', meta: c => (c.skip ? words(c.skip.reason) : (c.assignee ?? '')) },
]
const DONE = { name: 'done', statuses: ['done'], color: 'success' }
const TRIAGE = { name: 'triage', statuses: ['triage'], color: 'inactive' }
const LEGEND = [DONE, TRIAGE]
// The status bar: shipped on the left, the lanes, raw ideas on the right; archived cards stay out.
const BAR = [DONE, ...LANES, TRIAGE]

type Raw = { [key: string]: any }
type Ran = { exitCode: number; stdout: string; stderr: string }
type Saved = { pin: string | null }

// Module variables reset on a hot reload, and the old timer is cancelled; the board itself lives in the atoms above.
let timer: { cancel: () => void } | null = null
let bin: string | null = null
// A running dispatch keeps reading $.state as it stood when it began, so a read out for an old pin
// cannot see a new one there; `epoch` counts pin changes where every dispatch sees them at once.
let epoch = 0
let flight: { epoch: number; done: Promise<boolean> } | null = null

const message = (err: unknown) => (err instanceof Error ? err.message : String(err))
const str = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : String(v))
// `needs_input` → `needs input`, `overlaps:t_1` → `overlaps t_1`.
const words = (v: unknown) => str(v).replace(/^[a-z_]+/, k => k.replace(/_/g, ' ')).replace(':', ' ')
const join = (sep: string, ...parts: unknown[]) => parts.filter(p => p !== null && p !== undefined && p !== '').join(sep)
const list = (v: unknown): Raw[] => (Array.isArray(v) ? v : [])

function ago(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

// Terminal cells: CJK and emoji take two. shortcut: covers the common wide ranges, not every grapheme cluster.
const WIDE = /[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦\u{1f300}-\u{1faff}\u{20000}-\u{3fffd}]/u
const cells = (s: string) => [...s].reduce((n, ch) => n + (WIDE.test(ch) ? 2 : 1), 0)

// Exactly `n` cells: cut with an ellipsis, or padded on the right (`left` pads on the left).
function fit(s: string, n: number, left = false): string {
  if (n <= 0) return ''
  const flat = s.replace(/\s+/g, ' ')
  if (cells(flat) > n) {
    let out = ''
    for (const ch of flat) {
      if (cells(out + ch) > n - 1) break
      out += ch
    }
    return out + '…' + ' '.repeat(n - 1 - cells(out))
  }
  const pad = ' '.repeat(n - cells(flat))
  return left ? pad + flat : flat + pad
}

// A hint ends in the command to run, after its last arrow: cut the words before it, never the command.
function fitHint(s: string, n: number): string {
  const at = s.lastIndexOf(' → ')
  const room = n - cells(s.slice(at + 3)) - 3
  return cells(s) <= n || at < 0 || room < 12 ? fit(s, n) : `${fit(s.slice(0, at), room)} → ${s.slice(at + 3)}`
}

// Under a need, the reason and the command share a line when they fit, else the command gets its own.
function hintLines(s: string, n: number): string[] {
  const at = s.lastIndexOf(' → ')
  return cells(s) <= n || at < 0 ? [fit(s, n)] : [fit(s.slice(0, at), n), fit(s.slice(at + 1), n)]
}

// Cells per segment, rounded on the running total so the segments always fill `width` exactly.
// shortcut: a status holding a sliver of a big board can round to no cell; its lane still lists it.
function segments(parts: { color: string; n: number }[], width: number) {
  const total = parts.reduce((a, p) => a + p.n, 0)
  let seen = 0
  let drawn = 0
  return parts.map(p => {
    seen += p.n
    const end = Math.round((seen / total) * width)
    const n = end - drawn
    drawn = end
    return { color: p.color, n }
  })
}

// What each actionable event asks of the owner, in a few words.
function ask(n: KanbanNeed, now: number): { label: string; title: string; hint: string } {
  const p = n.payload ?? {}
  const id = n.task_id ?? ''
  const title = n.title ?? ''
  const why = str(n.reason)
  switch (n.kind) {
    case 'blocked':
      if (p.kind === 'needs_input') return { label: 'question', title, hint: join(' → ', why, `kanban unblock ${id} --reason "…"`) }
      if (p.kind === 'capability') return { label: 'grant', title, hint: join(' → ', why, `grant, then kanban unblock ${id}`) }
      return { label: 'blocked', title, hint: join(' → ', why || words(p.kind), `kanban unblock ${id}`) }
    case 'block_loop_detected':
      return { label: 'loop', title, hint: join(' → ', `blocked ${str(p.recurrences)}× on the same thing`, 'rewrite the card') }
    case 'gave_up':
      return { label: 'gave up', title, hint: join(' → ', p.failures ? `${str(p.failures)} failed runs` : why, `kanban log ${id} --tail 80`) }
    case 'merge_failed':
      return { label: 'merge', title, hint: join(' → ', str(p.reason), `kanban merge-retry ${id}`) }
    case 'awaiting_owner_merge':
      return { label: 'merge PR', title, hint: `${p.pr ? `PR #${str(p.pr)} ` : ''}approved, merge it yourself` }
    case 'account_paused':
      return { label: 'login', title: `${str(p.config_dir) || 'account'} signed out`, hint: join(' → ', p.login ? `run ${str(p.login)}` : null, 'kanban resume') }
    case 'quota_wall':
      return { label: 'quota', title: `${str(p.kind) || 'usage'} quota wall · ${Number(p.cards) === 1 ? '1 card waits' : `${str(p.cards || 0)} cards wait`}`, hint: `lifts by itself in ${ago(Number(p.until) - now)}` }
    case 'dispatch_paused':
      return { label: 'paused', title: 'Dispatch paused', hint: join(' → ', str(p.detail), 'kanban resume') }
    default:
      return { label: words(n.kind), title, hint: why }
  }
}

async function kanban($: EngineInterface, args: string[], cwd: string): Promise<Ran> {
  const opts = { cwd, timeoutMs: RUN_MS }
  if (bin) return $.process.run([bin, ...args], opts)
  const started = await $.clock.now()
  try {
    const r = await $.process.run(['kanban', ...args], opts)
    bin = 'kanban'
    return r
  } catch (err) {
    // A session started from a GUI or an IDE may not have ~/.local/bin on PATH.
    const home = await $.env.get('HOME')
    if (!home || (await $.clock.now()) - started > SPAWN_MS) throw err
    const fallback = `${home}/.local/bin/kanban`
    const r = await $.process.run([fallback, ...args], opts).catch(() => {
      throw new Error(`kanban is not on PATH or at ${fallback}`)
    })
    bin = fallback
    return r
  }
}

function parsed(r: Ran): Raw {
  if (r.exitCode !== 0) {
    const last = r.stderr.trim().split('\n').pop()
    throw new Error(last || `kanban exited ${r.exitCode}`)
  }
  return JSON.parse(r.stdout)
}

function digestOf(v: Raw): KanbanDigest {
  return {
    needs_you: list(v.needs_you) as KanbanNeed[],
    merged: list(v.merged) as KanbanNeed[],
    cost_usd: typeof v.cost_usd === 'number' ? v.cost_usd : null,
    daemon: v.daemon && typeof v.daemon.alive === 'boolean' ? { alive: v.daemon.alive, version: v.daemon.version ?? null } : null,
    paused: v.paused ? { reason: str(v.paused.reason), detail: v.paused.detail ?? null } : null,
  }
}

function factsOf(v: Raw): KanbanFacts {
  const t: Raw = v.task ?? {}
  const runs = list(v.runs)
  const r = runs[runs.length - 1]
  return {
    id: str(t.id),
    title: str(t.title),
    status: str(t.status),
    assignee: t.assignee ?? null,
    branch: t.branch_name ?? null,
    base: t.base_branch ?? null,
    pr_number: t.pr_number ?? null,
    pr_url: t.pr_url ?? null,
    block_kind: t.block_kind ?? null,
    skip: t.skip ?? null,
    summary: v.latest_summary ?? null,
    run: r ? { role: r.role ?? null, status: r.status ?? null, outcome: r.outcome ?? null, cost_usd: r.cost_usd ?? null, started_at: r.started_at ?? null, ended_at: r.ended_at ?? null } : null,
    comments: list(v.comments)
      .slice(-3)
      .map(c => ({ author: str(c.author), body: str(c.body), created_at: Number(c.created_at) || 0 })),
  }
}

async function loadDetail($: EngineInterface, id: string) {
  let next: KanbanDetail
  try {
    next = { id, facts: factsOf(parsed(await kanban($, ['--json', 'show', id], await $.session.cwd()))), error: null }
  } catch (err) {
    next = { id, facts: null, error: message(err) }
  }
  if ((await read($, OPEN)) !== id) return
  // A failed re-read keeps the facts already on screen.
  await update($, DETAIL, cur => (next.error && cur?.id === id && cur.facts ? { ...cur, error: next.error } : next))
}

// A toast per actionable event newer than the cursor. The first read only sets the cursor, so nothing replays.
async function toast($: EngineInterface, snap: KanbanSnapshot) {
  const needs = (snap.digest?.needs_you ?? []).filter(n => typeof n.id === 'number')
  const seen = await read($, SEEN)
  if (seen !== null) {
    for (const n of needs) {
      if ((n.id as number) <= seen) continue
      const a = ask(n, snap.at / 1000)
      $.ui.toast(`Kanban · ${a.label}: ${a.title}`)
    }
  }
  const top = Math.max(seen ?? 0, ...needs.map(n => n.id as number))
  if (top !== seen) await update($, SEEN, cur => Math.max(cur ?? 0, top))
}

async function fault($: EngineInterface, reason: string) {
  const at = await $.clock.now()
  await update($, FAULT, f => ({ reason, since: f?.since ?? at }))
}

// Resolves true when this read put a board on screen.
async function cycle($: EngineInterface, mine: number): Promise<boolean> {
  const pin = await read($, PIN)
  const cwd = await $.session.cwd()
  const scope = pin ? ['--board', pin] : []
  const at = await $.clock.now()
  const [cards, digest] = await Promise.allSettled([
    kanban($, [...scope, '--json', 'list', '--sort', 'priority'], cwd).then(parsed),
    // An older CLI without `digest` only hides the sections built on it.
    kanban($, [...scope, '--json', 'digest', '--since', '24h'], cwd).then(parsed),
  ])
  if (mine !== epoch) return false
  if (cards.status === 'rejected') {
    await fault($, message(cards.reason))
    return false
  }
  const snap: KanbanSnapshot = {
    board: str(cards.value.board) || pin || 'board',
    cards: list(cards.value.cards) as KanbanCard[],
    digest: digest.status === 'fulfilled' ? digestOf(digest.value) : null,
    at,
    pin,
  }
  // A read the module started before a hot reload can still finish after a newer one landed.
  await update($, SNAP, cur => (cur && cur.at > snap.at ? cur : snap))
  await update($, FAULT, () => null)
  await toast($, snap)
  const open = await read($, OPEN)
  if (open) await loadDetail($, open)
  return true
}

// One refresh in flight per pin: a poll, `r` and the command join the read already out,
// and a read for a pin since replaced is dropped when it returns.
function refresh($: EngineInterface): Promise<boolean> {
  if (flight?.epoch === epoch) return flight.done
  const mine = epoch
  const done: Promise<boolean> = cycle($, mine)
    .catch(async err => {
      if (mine === epoch) await fault($, message(err))
      return false
    })
    .catch(() => false)
    .finally(() => {
      if (flight?.done === done) flight = null
    })
  flight = { epoch: mine, done }
  return done
}

function poll($: EngineInterface) {
  timer ??= $.clock.every(POLL_MS, () => void refresh($))
}

function stop() {
  timer?.cancel()
  timer = null
}

const savedKey = async ($: EngineInterface) => `pane:${await $.session.cwd()}`

function openPane($: EngineInterface) {
  return $.ui.open({ id: PANE, title: 'Kanban', columns: 48, rows: 16 })
}

async function show($: EngineInterface, id: string) {
  await update($, OPEN, () => id)
  await update($, DETAIL, cur => (cur?.id === id ? cur : { id, facts: null, error: null }))
  await loadDetail($, id)
}

async function back($: EngineInterface) {
  await update($, OPEN, () => null)
  await update($, DETAIL, () => null)
}

// Pinning another board drops what was read for the old one, and any read still out for it.
async function pinTo($: EngineInterface, pin: string | null) {
  if ((await read($, PIN)) === pin) return
  epoch++
  await update($, PIN, () => pin)
  await update($, SNAP, () => null)
  await update($, FAULT, () => null)
  await back($)
}

// `r` also restarts polling, in case a reload cancelled the timer and no session.start followed.
async function press($: EngineInterface) {
  poll($)
  await refresh($)
}

// Reopen only where the saved board still reads; a failed read leaves the pane closed for this session.
async function reopen($: EngineInterface, saved: Saved) {
  await pinTo($, saved.pin ?? null)
  if (!(await refresh($))) return
  poll($)
  await openPane($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // A card's own headless worker has no one to show a board to.
    if (await $.env.get('KANBAN_RUN_ID')) return next(e)
    await $.command.register({
      name: 'kanban-board',
      description: 'Show the kanban board beside the transcript, or close it',
      argumentHint: '[BOARD-SLUG|close]',
    })
    // The pre-dev8 reopen key followed the person into every folder.
    await $.store.delete('pane').catch(() => undefined)
    if ((await $.ui.panes()).some(p => p.id === PANE)) {
      // Still open from before a reload or a resume: pick the polling back up.
      poll($)
      void refresh($)
    } else {
      const saved = (await $.store.get(await savedKey($))) as Saved | undefined
      if (saved) void reopen($, saved).catch(() => undefined)
    }
    return next(e)
  })

  on('command.run', { command: 'kanban-board' }, async ($, e) => {
    const arg = e.args.trim()
    const key = await savedKey($)
    if (arg === 'close') {
      await $.ui.close({ id: PANE })
      await $.store.delete(key)
      return { text: 'Kanban pane closed.' }
    }
    const pin = arg || null
    await pinTo($, pin)
    await $.store.set(key, { pin } satisfies Saved)
    poll($)
    await refresh($)
    const opened = await openPane($)
    return { text: opened.isPlaced ? 'Kanban pane open.' : `Kanban pane not shown: ${opened.reason}` }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) {
      stop()
      if (e.origin.kind !== 'unload') {
        await back($)
        await $.store.delete(await savedKey($)).catch(() => undefined)
      }
    }
    return next(e)
  }).catch(($, e, next) => next(e)) // a failed cleanup never keeps the pane open

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Link } = $.ui.resolve(e)
    const [snap, fault, open, detail] = await Promise.all([read($, SNAP), read($, FAULT), read($, OPEN), read($, DETAIL)])
    const now = (await $.clock.now()) / 1000
    const width = e.props.bodyColumns
    const narrow = width < 30
    const refreshButton = <Button key="refresh" label="refresh" hotkey="r" plain dimColor onPress={() => press($)} />

    if (!snap) {
      return (
        <Box flexDirection="column" width={width}>
          <Box flexDirection="row" gap={1}>
            <Text color="claude">◆</Text>
            <Text bold>Kanban</Text>
          </Box>
          {fault ? (
            <Box flexDirection="column">
              <Box flexDirection="row" gap={1}>
                <Text color="warning">▲</Text>
                <Box flexShrink={1}>
                  <Text color="warning">{fault.reason}</Text>
                </Box>
              </Box>
              <Text dimColor>/kanban-board SLUG pins a board.</Text>
            </Box>
          ) : (
            <Text dimColor>Reading the board…</Text>
          )}
          <Box marginTop={1}>{refreshButton}</Box>
        </Box>
      )
    }

    // A failed refresh keeps the last good board on screen and says how old it is.
    const stale = fault && (
      <Box flexDirection="row" gap={1}>
        <Box flexShrink={0}>
          <Text color="warning">{`△ stale ${ago(now - snap.at / 1000)}`}</Text>
        </Box>
        <Box flexShrink={1}>
          <Text dimColor wrap="truncate-end">
            {fault.reason}
          </Text>
        </Box>
      </Box>
    )

    if (open) {
      const f = detail?.id === open ? detail.facts : null
      const lane = LANES.find(l => f && l.statuses.includes(f.status))
      const field = (label: string, value: string) => (
        <Box key={label} flexDirection="row" gap={1}>
          <Box width={8} flexShrink={0}>
            <Text dimColor>{label}</Text>
          </Box>
          <Box flexShrink={1}>
            <Text wrap="truncate-end">{value}</Text>
          </Box>
        </Box>
      )
      const run = f?.run
      return (
        <Box flexDirection="column" width={width}>
          <Box flexDirection="row" justifyContent="space-between">
            <Button key="back" label="back" hotkey="b" plain role="dismiss" onPress={() => back($)} />
            {refreshButton}
          </Box>
          {stale}
          {!f && <Text dimColor>{detail?.error ?? `Reading ${open}…`}</Text>}
          {f && (
            <Box flexDirection="column" marginTop={1}>
              <Box flexDirection="row" gap={1}>
                <Text color={lane?.color ?? 'inactive'}>{lane?.glyph ?? '■'}</Text>
                <Text dimColor>{join(' · ', f.id, f.status, f.assignee)}</Text>
              </Box>
              <Text bold>{f.title}</Text>
              <Box flexDirection="column" marginTop={1}>
                {f.branch && field('branch', join(' → ', f.branch, f.base))}
                {f.pr_number && (
                  <Box flexDirection="row" gap={1}>
                    <Box width={8} flexShrink={0}>
                      <Text dimColor>pr</Text>
                    </Box>
                    {f.pr_url ? <Link href={f.pr_url} label={`#${f.pr_number}`} /> : <Text>{`#${f.pr_number}`}</Text>}
                  </Box>
                )}
                {f.block_kind && f.status === 'blocked' && field('blocked', words(f.block_kind))}
                {f.skip && field('waits', words(f.skip.reason))}
                {run &&
                  field(
                    'last run',
                    join(
                      ' · ',
                      run.role,
                      run.outcome ?? run.status,
                      run.cost_usd != null ? `$${run.cost_usd.toFixed(2)}` : null,
                      run.started_at ? `${ago(now - (run.ended_at ?? run.started_at))} ago` : null,
                    ),
                  )}
              </Box>
              {f.summary && (
                <Box marginTop={1}>
                  <Text dimColor>{f.summary}</Text>
                </Box>
              )}
              {f.comments.length > 0 && (
                <Box flexDirection="column" marginTop={1}>
                  <Text bold dimColor>
                    Comments
                  </Text>
                  {f.comments.map(c => (
                    <Box key={`${c.created_at}:${c.author}`} flexDirection="column">
                      <Text dimColor>{`${c.author} · ${ago(now - c.created_at)}`}</Text>
                      <Text>{c.body}</Text>
                    </Box>
                  ))}
                </Box>
              )}
              {detail?.error && (
                <Text color="warning" wrap="truncate-end">
                  {`△ ${detail.error}`}
                </Text>
              )}
            </Box>
          )}
        </Box>
      )
    }

    const of = (statuses: string[]) => snap.cards.filter(c => statuses.includes(c.status))
    const lanes = LANES.map(l => ({ ...l, cards: of(l.statuses) })).filter(l => l.cards.length > 0)
    const parts = BAR.map(g => ({ color: g.color, n: of(g.statuses).length }))
    const total = parts.reduce((a, p) => a + p.n, 0)
    const d = snap.digest
    const needs = d?.needs_you ?? []
    const merged = (d?.merged ?? []).slice(-2).reverse()
    const wide = e.props.placement === 'inline' && width >= 72

    const health = (
      <Box flexDirection="row" gap={2} flexShrink={0}>
        {d?.daemon && !d.daemon.alive ? (
          <Text color="error">○ daemon off</Text>
        ) : d?.paused ? (
          <Text color="warning">‖ paused</Text>
        ) : d?.daemon?.alive ? (
          <Text color="success">● live</Text>
        ) : null}
        <Text dimColor>{d?.cost_usd != null ? `$${d.cost_usd.toFixed(2)} 24h` : `${total - of(['done']).length} open`}</Text>
      </Box>
    )
    const header = (
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Box flexDirection="row" gap={1} flexShrink={1}>
          <Text color="claude">◆</Text>
          <Text bold wrap="truncate-end">
            {snap.board}
          </Text>
        </Box>
        {health}
      </Box>
    )
    const bar = total > 0 && (
      <Box flexDirection="row">
        {segments(parts, width).map(
          (seg, i) =>
            seg.n > 0 && (
              <Text key={String(i)} color={seg.color}>
                {'━'.repeat(seg.n)}
              </Text>
            ),
        )}
      </Box>
    )
    const footer = (
      <Box flexDirection="row" justifyContent="space-between">
        <Box flexDirection="row" gap={2}>
          {LEGEND.map(g => (
            <Box key={g.name} flexDirection="row" gap={1}>
              <Text color={g.color}>■</Text>
              <Text dimColor>{`${g.name} ${of(g.statuses).length}`}</Text>
            </Box>
          ))}
        </Box>
        {refreshButton}
      </Box>
    )

    // A section header: glyph and name in the section's accent, the count dim on the right.
    const head = (glyph: string, name: string, color: string, count: number | null, w: number) => (
      <Box flexDirection="row" justifyContent="space-between" width={w}>
        <Box flexDirection="row" gap={1}>
          <Text color={color}>{glyph}</Text>
          <Text bold color={color}>
            {name}
          </Text>
        </Box>
        {count !== null && <Text dimColor>{String(count)}</Text>}
      </Box>
    )

    // A row is one Button, so focus inverts it and Enter opens the card. Its cells are padded by hand,
    // so badges, titles and the meta column line up across lanes.
    const row = (key: string, id: string | null, w: number, badge: number, metaW: number, c: { priority?: number; title: string; meta: string }) => {
      const titleW = w - 2 - badge - (metaW ? metaW + 1 : 0)
      const cellsOf = [
        <Text key="pad">{'  '}</Text>,
        ...(badge
          ? [
              <Text key="badge" bold color="warning">
                {fit(c.priority ? `P${c.priority}` : '', badge)}
              </Text>,
            ]
          : []),
        <Text key="title">{fit(c.title, titleW)}</Text>,
        ...(metaW
          ? [
              <Text key="meta" dimColor>
                {' ' + fit(c.meta, metaW, true)}
              </Text>,
            ]
          : []),
      ]
      return id ? (
        <Button key={key} plain onPress={() => show($, id)}>
          {cellsOf}
        </Button>
      ) : (
        <Box key={key} flexDirection="row">
          {cellsOf}
        </Box>
      )
    }

    const metaWidth = (w: number, metas: string[]) => (narrow ? 0 : Math.min(Math.floor(w / 3), Math.max(0, ...metas.map(cells))))
    const badgeWidth = (cs: KanbanCard[]) => (cs.some(c => c.priority > 0) ? Math.max(...cs.map(c => (c.priority > 0 ? `P${c.priority}`.length : 0))) + 1 : 0)

    const strip = (max: number, oneLine: boolean) =>
      needs.length > 0 && (
        <Box flexDirection="column">
          {head('▲', 'Needs you', 'warning', needs.length, width)}
          {needs.slice(0, max).map((n, i) => {
            const a = ask(n, now)
            const lead = [
              <Text key="pad">{'  '}</Text>,
              <Text key="label" color="warning">
                {fit(a.label, LABEL)}
              </Text>,
              <Text key="title">{' ' + (oneLine ? fitHint(join(' · ', a.title, a.hint), width - 3 - LABEL) : fit(a.title, width - 3 - LABEL))}</Text>,
            ]
            return (
              <Box key={String(i)} flexDirection="column">
                {n.task_id ? (
                  <Button key={`need:${i}`} plain onPress={() => show($, n.task_id as string)}>
                    {lead}
                  </Button>
                ) : (
                  <Box flexDirection="row">{lead}</Box>
                )}
                {!oneLine &&
                  a.hint &&
                  hintLines(a.hint, width - 3 - LABEL).map((s, j) => (
                    <Text key={`hint:${j}`} dimColor>
                      {' '.repeat(LABEL + 3) + s}
                    </Text>
                  ))}
              </Box>
            )
          })}
          {needs.length > max && <Text dimColor italic>{`  +${needs.length - max} more`}</Text>}
        </Box>
      )

    const recent = (w: number) =>
      merged.length > 0 && (
        <Box flexDirection="column">
          {head('✓', 'Merged', 'success', null, w)}
          {merged.map(m => row(`merged:${m.task_id}:${m.at}`, m.task_id, w, 0, narrow ? 0 : 4, { title: m.title ?? '', meta: ago(now - m.at) }))}
        </Box>
      )

    if (wide) {
      // Inline: the lanes side by side, as many rows as the frame holds.
      const gap = 2
      const colW = Math.floor((width - gap * Math.max(0, lanes.length - 1)) / Math.max(1, lanes.length))
      const used = 3 + (needs.length > 0 ? 1 + Math.min(needs.length, 2) + (needs.length > 2 ? 1 : 0) : 0) + (fault ? 1 : 0)
      const room = Math.max(1, Math.min(SHOWN, e.props.scroll.bodyRows - used - 2))
      return (
        <Box flexDirection="column" width={width}>
          {header}
          {bar}
          {stale}
          {strip(2, true)}
          {lanes.length === 0 && <Text dimColor>Nothing running or planned.</Text>}
          <Box flexDirection="row" gap={gap}>
            {lanes.map(l => {
              const cs = l.cards.slice(0, room)
              const metaW = colW < 30 ? 0 : metaWidth(colW, cs.map(c => l.meta(c, now)))
              const badge = badgeWidth(cs)
              return (
                <Box key={l.key} flexDirection="column" width={colW} flexShrink={0}>
                  {head(l.glyph, l.name, l.color, l.cards.length, colW)}
                  {cs.map(c => row(`card:${c.id}`, c.id, colW, badge, metaW, { priority: c.priority, title: c.title, meta: l.meta(c, now) }))}
                  {l.cards.length > cs.length && <Text dimColor italic>{`  +${l.cards.length - cs.length} more`}</Text>}
                </Box>
              )
            })}
          </Box>
          {footer}
        </Box>
      )
    }

    // Dock: one column, lanes as headed lists, one meta column shared by every lane.
    const visible = lanes.flatMap(l => l.cards.slice(0, SHOWN).map(c => ({ c, meta: l.meta(c, now) })))
    const metaW = metaWidth(width, visible.map(v => v.meta))
    const badge = badgeWidth(visible.map(v => v.c))
    return (
      <Box flexDirection="column" width={width}>
        {header}
        {bar}
        {stale}
        {strip(3, false)}
        {lanes.length === 0 && <Text dimColor>Nothing running or planned.</Text>}
        {lanes.map(l => (
          <Box key={l.key} flexDirection="column">
            {head(l.glyph, l.name, l.color, l.cards.length, width)}
            {l.cards.slice(0, SHOWN).map(c => row(`card:${c.id}`, c.id, width, badge, metaW, { priority: c.priority, title: c.title, meta: l.meta(c, now) }))}
            {l.cards.length > SHOWN && <Text dimColor italic>{`  +${l.cards.length - SHOWN} more`}</Text>}
          </Box>
        ))}
        {recent(width)}
        {footer}
      </Box>
    )
  })
}
