# proxycache

Transparent proxy in front of `llama-server` (beellama.cpp fork) that auto-persists
slot KV state to disk so conversations survive server restarts. **Zero client-side
config** — clients never install plugins or set headers.

## Run

```
venv/bin/python proxycache.py [flags]
```

| Flag | Meaning |
|---|---|
| (none) | serve the proxy (default) |
| `--config <path>` | config file location (default `./config.json`) |
| `--generate-config` | print every key with default + accepted values + description |
| `--version` | print version |

There is no TUI — no flags means it serves.

## Config

Single `config.json`, self-documenting. `--generate-config` prints every key with
its default, accepted values, and a one-line description. Run `--generate-config`
once to see them all; the full knob table (defaults, accepted domains) is in the
spec's **Configuration** section.

## Ground rules (carried from the design)

- **Zero client cooperation** — the proxy fingerprints conversations itself from the
  token list (via `/apply-template` + `/tokenize`); the optional `X-Conversation-Id`
  header is the only client surface.
- **Strictly additive** — the proxy only restores when a conversation provably can't be
  served from the live tree; it never touches a conv already warm in RAM.
- **Everything degrades to cold** — every failure mode falls back to a full prefill,
  never to a wrong answer.
- **Pinned dependency** — server behavior is verified against a specific commit; the
  re-verification checklist in the design doc runs before trusting any version bump.
  **Current**: live API tests run against the mainline (non-fork) + cherry-picked
  commit (`25747b08` + #25592 + #26004), not the bee fork.
- **Server reachable only via the proxy** — if any client mutates a slot directly,
  the proxy's action log goes stale silently.
- **Shared filesystem** — the server *writes* `.bin` files (`--slot-save-path`); the
  proxy *deletes* them; both must see the same path (same host or shared volume).

## Files

- **`llamacpp-autosave-proxy-design.md`** — the spec: mechanism, save/restore events,
  desk state, failure modes, config, provenance, test strategy. **The source of truth.**
- **`AGENTS.md`** — how to work in this repo (workflow, conventions).
- **`CHANGELOG.md`** — build log + phase tracker + work queue. **Track progress here.**
