# proxycache

A transparent proxy in front of [llama.cpp](https://github.com/ggml-org/llama.cpp) that persists each
conversation's KV cache to disk and restores it on demand — so long-context chats (30–100k tokens,
IDE sessions) skip full re-prefill after slot eviction, model swaps, or proxy restarts.

Clients keep pointing at the proxy: everything is passed through byte-for-byte except the two things
the proxy exists to manage.

```
client ──▶ proxycache :8081 ──▶ llama.cpp (router) :30000 ──▶ model instance :36899
              │  watches /models/sse
              │  saves/restores {key}.bin via /slots/{id}
```

## How it works

**Keying.** For each chat request the proxy renders the exact prompt the model will see
(`/apply-template`), tokenizes it (`/tokenize`), and cuts the token stream into fixed blocks
(256 tokens each), hashing every block. The conversation key is a hash of `(model, rendered prefix)` —
the request's own model, so multi-model routers never cross-key.
If the server endpoints are unavailable it falls back to word blocks.

**Matching (LCP).** A restore candidate is the saved key sharing the longest common block prefix,
with ratio ≥ `lcp_threshold`. Same chat → exact match. Chat grown → the last save is a prefix.
Edited mid-history → shared blocks up to the edit point. Below threshold → cold start.

**Slot management.** The proxy tracks which key lives in which slot (updated only on *confirmed*
save/restore). On a request it:

1. pre-saves the slot's current key if it differs from the request's and is not a pure prefix
   (pre-eviction save — this is what makes "switch chat, come back" work),
2. restores the best candidate into the slot,
3. pins the chat to that slot, forwards, then saves the new state (with a cost fuse: never
   re-saves the same key more often than `min_interval`).

**Event-driven saves.** A watcher per backend consumes `GET /models/sse`: a foreign model loading
→ preemptive save of the current key; a model unloading → slot marked cold; loaded → table reset.
A 20s reconciler polls `/props` + `/slots` for in-model KV loss (e.g. RAM eviction).
Shutdown (SIGTERM) saves every occupied slot.

**Receipts & GC.** Every response records its reuse ratio (`cache_n / (cache_n + prompt_n)`).
A key whose last two receipts show ~0 reuse is pruned from the index (its `.bin` stays on disk
along with its `.bin` when the save path is reachable). The meta index can also be capped
(`max_entries`, `0` = no cap) — manual eviction: `python proxycache.py --gc N --by created|unused`.

**Pass-through.** Everything else — `/v1/models`, `/props`, `/health`, `/metrics`, the Web UI,
tools, embeddings, anything — is forwarded raw. The proxy injects the `model` field into
`POST /slots/{id}?action=save|restore` (required in router mode).

## Quick start

```bash
git clone <repo> && cd proxycache
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python proxycache.py        # generates proxycache.toml on first run, then serves
```

Your llama.cpp (single server, or the router) is configured in `proxycache.toml`:

```toml
[server]
host = "0.0.0.0"
port = 8081

[model]
# Managed models; empty list = auto-discover all from /v1/models
models = ["27B-Q3.8"]

[[backends]]
url = "http://127.0.0.1:30000"   # n_slots is optional (fallback only; /slots is the truth)

[meta]
dir = "/path/to/llama-kv-cache"   # usually the server's --slot-save-path
```

Point clients (Open WebUI, Cline, …) at the proxy's port and use `/v1/chat/completions` as usual —
streaming and non-streaming both work.

## Configuration

Everything lives in **`proxycache.toml`** — generated on first start if missing, edit and restart.
No env vars, no CLI flags.

| key | default | meaning |
|---|---|---|
| `server.host` / `server.port` | `0.0.0.0` / `8081` | listen address |
| `server.log_level` | `INFO` | log level |
| `server.request_timeout` | `600` | max seconds per proxied request |
| `server.acquire_timeout` | `300` | max wait for a slot lock, then 503 |
| `model.models` | `[]` | managed models (empty = auto-discover all) |
| `backends[].url` / `n_slots` | — / optional | backend URL; slot count fallback (server's `/slots` always wins) |
| `hashing.words_per_block` / `tokens_per_block` | `100` / `256` | block window size |
| `hashing.big_threshold_words` / `big_threshold_tokens` | `500` / `300` | "big request" threshold |
| `hashing.lcp_threshold` | `0.6` | min LCP ratio to restore |
| `meta.dir` | `./kv_meta` | meta directory (proxy-local) |
| `meta.max_entries` | `32` | index cap, oldest evicted (`0` = no cap) |
| `meta.save_path` | `""` | llama host's `--slot-save-path`; empty = auto-detect from `GET /models` |
| `saves.min_interval` | `10` | seconds between saves of the same key |
| `watcher.reconcile_interval` | `20` | seconds between KV-loss polls |
| `watcher.sse_reconnect_backoff` | `5` | initial SSE reconnect delay (doubles, cap 60s) |
| `watcher.kv_drop_ratio` | `0.1` | slot token-count drop fraction that marks cold |
| `receipts.window` / `receipts.ratio` | `2` / `0.2` | stale-key pruning thresholds |

## Manual GC

One-shot eviction, no server needed:

```sh
python proxycache.py --gc 8 --by created --dry-run   # preview
python proxycache.py --gc 8 --by unused              # evict 8 longest-unused
```

For each evicted key it deletes `{meta.dir}/{key}.meta.json` and
`{save_path}/{key}.bin` (the save path is auto-detected from `GET /models`
unless `meta.save_path` overrides it; `.bin` deletion is skipped when the
path is unreachable — e.g. proxy and llama on different machines).

## Endpoints

| path | behaviour |
|---|---|
| `POST /v1/chat/completions` | the watch: key → match → restore → pin → save |
| `POST /slots/{id}?action=save\|restore` | pass-through + `model` injection |
| `GET /metrics` | proxy counters (`proxycache_*`) |
| everything else | byte-for-byte pass-through |

## Tests

```bash
pytest tests/
```

Unit tests cover hashing/LCP, the meta index (cap, persistence, filtering), receipts/stale
pruning, and the slot manager (acquire/release, table semantics, cost fuse).

## Layout

```
src/proxycache/
  app.py           # FastAPI app: passthrough, the watch, lifespan
  config.py        # toml config, generated on first start
  hashing.py       # prefix rendering, block hashing, LCP
  llama_client.py  # httpx client: chat, slots, models, sse
  slot_manager.py  # slot table, locks, save/restore, shutdown saves
  meta_index.py    # in-memory meta index + cap GC
  receipts.py      # reuse receipts, stale-key pruning
  watcher.py       # /models/sse lifecycle watcher + reconciler
```

Design records: [`assessment.md`](assessment.md) (why), [`upgrade.md`](upgrade.md) (what was built, in order), [`flows.md`](flows.md) (request flow).
