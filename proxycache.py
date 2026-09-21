"""proxycache launcher.

Server:    python proxycache.py
One-shot:  python proxycache.py --gc N --by created|unused [--dry-run]
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import uvicorn

from proxycache import config


def _gc_args() -> dict | None:
    import argparse

    p = argparse.ArgumentParser(description="proxycache")
    p.add_argument("--gc", type=int, metavar="N",
                   help="evict N metas (and their .bin) and exit")
    p.add_argument("--by", choices=["created", "unused"], default="created",
                   help="gc policy: oldest created (default) or longest unused")
    p.add_argument("--dry-run", action="store_true",
                   help="gc: print what would be deleted, delete nothing")
    a = p.parse_args()
    if a.gc is None:
        return None
    return {"n": a.gc, "by": a.by, "dry": a.dry_run}


if __name__ == "__main__":
    gc = _gc_args()
    if gc:
        from proxycache import gc as gcmod
        gcmod.main(gc["n"], gc["by"], gc["dry"])
    else:
        from proxycache.app import app
        uvicorn.run(
            app,
            host=config.SERVER_HOST,
            port=config.PORT,
            log_level=config.LOG_LEVEL.lower(),
        )
