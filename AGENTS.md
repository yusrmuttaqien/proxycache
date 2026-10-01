# AGENTS.md

## Project

`proxycache` — a transparent reverse proxy in front of a multi-model `llama-server`
router that auto-saves conversation KV caches to disk and restores them on demand,
so evicted conversations are re-served from a warm KV file (a cache hit) instead of a
full prefill. **Zero client cooperation** — clients install nothing. Full spec:
`llamacpp-autosave-proxy-design.md` (behavior source of truth).

## DOX Workflow

- **Before editing**: read this file + `CHANGELOG.md` (last entries) + the spec
  sections relevant to the task
- **After editing**: append to `CHANGELOG.md` (Task/Changes/Lessons) + update the
  phase status table in it
- **Do not rely on memory**: re-read this chain in the current session before editing
- Spec changes (new decisions) go into `llamacpp-autosave-proxy-design.md`, not here

## Root Ownership

- `CHANGELOG.md` — build log + phase tracker + work queue; human + model editable
- `AGENTS.md` — this file
- `README.md` — user-facing overview (run, config, ground rules)
- `llamacpp-autosave-proxy-design.md` — spec (edit only when a behavior decision changes)

## Build Conventions

- **No project/packaging setup** — no pyproject install; the repo root is the app.
  Run: `venv/bin/python proxycache.py [flags]` (root entry → `src/__main__.main`)
- **No TUI** — default (no flags) = serve the proxy
- Venv: `venv/` (Python 3.12); use `venv/bin/python` / `venv/bin/pip` directly
- `requirements.txt` is the dependency record: **regenerate it in the same commit
  that changes dependencies** (`venv/bin/pip freeze | grep -v "^pip" > requirements.txt`)
- Config: single `config.json`; `--generate-config` self-doc; file > default
  precedence. See spec Configuration.

## Commit Conventions

- **Commit at worthy checkpoints and at every phase boundary** — each commit = a
  restorable state, paired with its `CHANGELOG.md` entry
- **Conventional format**: `feat:` · `fix:` · `docs:` · `refactor:` · `test:` ·
  `chore:` · `perf:` — imperative, one line; the `CHANGELOG.md` entry carries the
  detail (1:1 mapping, `git log` + changelog tell the same story)
- Optional elaboration after an em-dash:
  `feat: drain before switch — restore lands on an empty slot`
- No commit for trivial in-progress state; a checkpoint is worthy when the last
  logged step is complete and verified

## Modules (src/)

One module per concern; entry `src/__main__.py`, config `src/config.py`.

- `src/__main__.py` — entry point (argparse, serve loop)
- `src/config.py` — config system (load/generate, save_path detection)
- `src/proxy.py` — transparent forward (catch-all route)
- `src/routes.py` — route table (classify → Action)
- `src/desk.py` — desk state (ram_since_restore, mark_dirty)
- `src/convkey.py` — conversation keying (ConvTracker)
- `src/fingerprint.py` — token fingerprint (get_tokens)
- `src/save.py` — save trigger (should_save)
- `src/restore.py` — restore decision (decide, RestoreAction)
- `src/allocator.py` — slot allocator (SlotAllocator, inject_id_slot)
- `src/shifted.py` — shifted-suffix detection (detect_shifted_suffix)
- `src/robustness.py` — robustness (timeouts, connection drop, health poll)
- `src/observability.py` — miss debugger (classify_miss, echo_conversation_id)
- `src/storage.py` — filename layout (conv_path)
- `src/client.py` — HTTP client (UpstreamClient)
- `src/handler.py` — request handler (ProxyState, handle_chat, _execute_save, _execute_restore)
- `src/ledger.py` — file ledger (FileLedger)
- `src/lru.py` — LRU eviction (LRUEvictor)
