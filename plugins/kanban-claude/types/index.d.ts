// The pane's state contract: what `hooks/board.tsx` keeps in `$.state`, read back while drawing.
// Fields marked optional come from newer `kanban` CLIs; an older CLI leaves them out.

export type KanbanSkip = { reason: string; at: number }

export type KanbanCard = {
  id: string
  title: string
  status: string
  assignee: string | null
  priority: number
  updated_at: number
  completed_at?: number | null
  pr_number?: number | null
  skip?: KanbanSkip | null
  block_kind?: string | null
}

// One `needs_you` or `merged` item of `kanban --json digest`.
export type KanbanNeed = {
  id?: number
  task_id: string | null
  kind: string
  at: number
  title: string | null
  payload: { [key: string]: unknown }
  reason?: string | null
}

export type KanbanDigest = {
  needs_you: KanbanNeed[]
  merged: KanbanNeed[]
  cost_usd: number | null
  daemon: { alive: boolean; version: string | null } | null
  paused: { reason: string; detail: string | null } | null
}

// The last good read of the board; `at` is the engine clock in ms when the read started.
export type KanbanSnapshot = {
  board: string
  cards: KanbanCard[]
  digest: KanbanDigest | null
  at: number
  pin: string | null
}

// Why the latest refresh failed, and since when refreshes have been failing.
export type KanbanFault = { reason: string; since: number }

export type KanbanComment = { author: string; body: string; created_at: number }

export type KanbanRun = {
  role: string | null
  status: string | null
  outcome: string | null
  cost_usd: number | null
  started_at: number | null
  ended_at: number | null
}

// The facts the detail view shows, picked from `kanban --json show ID`.
export type KanbanFacts = {
  id: string
  title: string
  status: string
  assignee: string | null
  branch: string | null
  base: string | null
  pr_number: number | null
  pr_url: string | null
  block_kind: string | null
  skip: KanbanSkip | null
  summary: string | null
  run: KanbanRun | null
  comments: KanbanComment[]
}

export type KanbanDetail = { id: string; facts: KanbanFacts | null; error: string | null }

declare module 'claude-code' {
  interface PluginState {
    'kanban-claude': {
      snapshot: KanbanSnapshot | null
      fault: KanbanFault | null
      pin: string | null
      open: string | null
      detail: KanbanDetail | null
      seen: number | null
    }
  }
}
