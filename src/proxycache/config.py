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
# Optional: the llama host's --slot-save-path (where .bin KV files live),
# used by --gc and tip-keeping to delete .bin alongside .meta.json.
# Absent/empty = auto-detect from GET /models (the instance's command line).
# save_path = ""


[saves]
# Min seconds between saves of the same key (cost fuse).
min_interval = 10.0
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
ACQUIRE_TIMEOUT = float(_cfg["server"]["acquire_timeout"])

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
# The llama host's --slot-save-path; empty = auto-detect from GET /models.
SAVE_PATH = _cfg["meta"].get("save_path", "")

# --- saves ---------------------------------------------------------------------
SAVE_MIN_INTERVAL = float(_cfg["saves"]["min_interval"])

logging.basicConfig(
    level=LOG_LEVEL.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
