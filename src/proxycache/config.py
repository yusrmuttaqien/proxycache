# config.py
# -*- coding: utf-8 -*-

"""
Single configuration point.

The source of truth is `proxycache.toml` next to this package's repo root.
If the file does not exist it is generated on first start with the defaults
below, so configuration is a file you edit — no env vars, no CLI flags.
"""

import os
import tomllib
import logging
from typing import Optional

_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
CONFIG_FILE = os.path.join(_ROOT, "proxycache.toml")

_TEMPLATE = """\
# proxycache.toml — generated on first start; edit and restart.

[server]
host = "0.0.0.0"
port = 8081
log_level = "INFO"
# Max seconds a proxied request may take.
request_timeout = 600.0
# Max seconds to wait for a slot lock before answering 503.
# 0 = no timeout (wait indefinitely, like llama-server's native queue).
acquire_timeout = 300.0

[model]
# Models to manage (router mode). Empty list = auto-discover all from /v1/models.
models = []

# One entry per llama.cpp backend (router or single-model server).
[[backends]]
url = "http://127.0.0.1:8080"   # llama-server default port
# Optional fallback slot count (used only if the server is unreachable at
# startup); the server's /slots is always the source of truth. Defaults to 1.
# n_slots = 2

[hashing]
# Prefixes are hashed in fixed blocks; a block is a hash of a window of
# words (fallback path) or tokens (server-rendered path).
words_per_block = 100
tokens_per_block = 256
# A request is "big" (restore/save eligible) above these thresholds.
big_threshold_words = 500
big_threshold_tokens = 300
# Minimum LCP ratio for a saved key to qualify as a restore candidate.
lcp_threshold = 0.6

[meta]
# Where .meta.json files live (proxy-local bookkeeping).
dir = "./kv_meta"
# In-memory index cap; oldest meta is evicted on write. 0 = no cap
# (manage manually with: python proxycache.py --gc N --by created|unused).
max_entries = 32
# Which meta the cap evicts: "created" (oldest created) or "unused"
# (longest unused, like --gc --by unused). Absent = "created".
# max_entries_policy = "created"
# Max total size of .bin files (GB). 0 = no cap. Uses the same
# policy as max_entries (max_entries_policy).
# max_size_gb = 0.0
# Optional: the llama host's --slot-save-path (where .bin KV files live),
# used by --gc and tip-keeping to delete .bin alongside .meta.json.
# Absent/empty = auto-detect from GET /models (the instance's command line).
# save_path = ""


[saves]
# Min seconds between saves of the same key (cost fuse).
min_interval = 10.0
# False = only response-complete saves; disables event-triggered saves
# (preemptive on model loading, pre-unload, shutdown).
event_saves = true

[watcher]
# Seconds between reconciler polls (KV-loss detection by polling).
reconcile_interval = 20.0
# Initial delay between SSE reconnects (doubles each retry, capped at 60s).
sse_reconnect_backoff = 5.0
# A slot is marked cold if its token count drops below this fraction of
# the last-seen value (in-model KV loss, e.g. RAM eviction).
kv_drop_ratio = 0.1

[receipts]
# A key is stale (pruned) if its last `window` receipts all show a reuse
# ratio below `ratio` (its saved cache is probably dead).
window = 2
ratio = 0.2
"""

log = logging.getLogger(__name__)


def _ensure_file() -> None:
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            f.write(_TEMPLATE)
        log.info("config_generated %s", CONFIG_FILE)


def _load() -> dict:
    _ensure_file()
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


_cfg = _load()

# --- server ---------------------------------------------------------------
SERVER_HOST = _cfg["server"]["host"]
PORT = int(_cfg["server"]["port"])
LOG_LEVEL = _cfg["server"]["log_level"]
REQUEST_TIMEOUT = float(_cfg["server"]["request_timeout"])
# 0 = no timeout (indefinite wait, like llama-server's native queue).
ACQUIRE_TIMEOUT: Optional[float] = float(_cfg["server"]["acquire_timeout"]) or None

# --- model -----------------------------------------------------------------
MODELS: list = list(_cfg["model"].get("models", []))

# --- backends ---------------------------------------------------------------
# Mutable list of dicts; slot discovery updates n_slots at startup.
# n_slots is optional: the server's /slots is the truth, the config value
# is only the fallback when the backend is unreachable at startup.
BACKENDS = [
    {"url": be["url"], "n_slots": int(be.get("n_slots", 1))}
    for be in _cfg.get("backends", [])
]

# --- hashing -----------------------------------------------------------------
WORDS_PER_BLOCK = int(_cfg["hashing"]["words_per_block"])
TOKENS_PER_BLOCK = int(_cfg["hashing"]["tokens_per_block"])
BIG_THRESHOLD_WORDS = int(_cfg["hashing"]["big_threshold_words"])
BIG_THRESHOLD_TOKENS = int(_cfg["hashing"]["big_threshold_tokens"])
LCP_TH = float(_cfg["hashing"]["lcp_threshold"])

# --- meta ---------------------------------------------------------------------
_META_DIR = _cfg["meta"]["dir"]
META_DIR = _META_DIR if os.path.isabs(_META_DIR) else os.path.join(_ROOT, _META_DIR)
os.makedirs(META_DIR, exist_ok=True)
META_MAX_ENTRIES = int(_cfg["meta"]["max_entries"])
META_MAX_ENTRIES_POLICY = _cfg["meta"].get("max_entries_policy", "created")
META_MAX_SIZE_GB = float(_cfg["meta"].get("max_size_gb", 0.0))
# The llama host's --slot-save-path; empty = auto-detect from GET /models.
SAVE_PATH = _cfg["meta"].get("save_path", "")

# --- saves ---------------------------------------------------------------------
SAVE_MIN_INTERVAL = float(_cfg["saves"]["min_interval"])
# False = only response-complete saves; event-triggered saves (preemptive
# on model loading, pre-unload, shutdown) are disabled.
EVENT_SAVES = bool(_cfg["saves"].get("event_saves", True))

# --- watcher ---------------------------------------------------------------
# .get with defaults: toml files generated by older templates lack these
# sections entirely; the template is the only place defaults are written.
_W = _cfg.get("watcher", {})
RECONCILE_INTERVAL = float(_W.get("reconcile_interval", 20.0))
SSE_RECONNECT_BACKOFF = float(_W.get("sse_reconnect_backoff", 5.0))
KV_DROP_RATIO = float(_W.get("kv_drop_ratio", 0.1))

# --- receipts ---------------------------------------------------------------
_R = _cfg.get("receipts", {})
STALE_WINDOW = int(_R.get("window", 2))
STALE_RATIO = float(_R.get("ratio", 0.2))

logging.basicConfig(
    level=LOG_LEVEL.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
