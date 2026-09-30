# proxycache

Transparent proxy in front of `llama-server` (beellama.cpp fork) that auto-persists slot
KV state to disk so conversations survive server restarts. **Zero client-side config** —
clients never install plugins or set headers.

## Status

**Design complete · implementation not started.**

| Artifact | State |
|---|---|
| Design doc | ✅ complete — `llamacpp-autosave-proxy-design.md` |
| Build plan | ✅ scaffolded — `PLAN.md` |
| Implementation | ⬜ not started (track in `PLAN.md`) |

## Files

- **`llamacpp-autosave-proxy-design.md`** — the design: mechanism, save/restore events,
  desk state, failure modes, config, provenance (verified against source at a pinned
  commit), test strategy. **Read this first** — it is the source of truth.
- **`PLAN.md`** — the build: phased task breakdown with checkboxes, each task mapped to
  the design section it implements. **Track progress here.**

## Workflow

1. **Design is the source of truth.** If implementation reveals a design gap, update the
   design doc first (it carries provenance), then the plan. Never let the two drift.
2. **Work `PLAN.md` top-to-bottom.** Check off a task only when it lands *and* is tested.
3. **Keep the task→design link honest.** Every task cites the design section it
   implements; if that section changes, re-read the task.

## Ground rules (carried from the design)

- **Zero client cooperation** — the proxy fingerprints conversations itself from
  `input_tokens`; the optional `X-Conversation-Id` header is the only client surface.
- **Strictly additive** — the proxy only restores when a conversation provably can't be
  served from the live tree; it never touches a conv already warm in RAM.
- **Everything degrades to cold** — every failure mode falls back to a full prefill,
  never to a wrong answer.
- **Pinned dependency** — server behavior is verified against a specific commit; the
  re-verification checklist in the design doc runs before trusting any version bump.
- **Server reachable only via the proxy** — if any client mutates a slot directly,
  the proxy's action log goes stale silently.
- **Shared filesystem** — the server *writes* `.bin` files (`--slot-save-path`); the
  proxy *deletes* them; both must see the same path (same host or shared volume).
