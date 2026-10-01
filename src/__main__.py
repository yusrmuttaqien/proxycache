"""proxycache entry point.

Default (no flags): serve the proxy. Flags: --generate-config, --config, --version.
"""

from __future__ import annotations

import argparse

from importlib.metadata import PackageNotFoundError, version

from aiohttp import web

from src.config import (
    CONFIG_FILENAME,
    DEFAULTS,
    PROJECT_ROOT,
    SCHEMA,
    generate_config_text,
    load_config,
    write_config,
)
from src.proxy import make_app

try:
    __version__ = version("proxycache")
except PackageNotFoundError:  # running from a checkout without install
    __version__ = "0.0.0+local"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proxycache",
        description=(
            "Transparent reverse proxy that auto-saves/restores llama-server "
            "conversation KV caches to disk"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"proxycache {__version__}"
    )
    parser.add_argument(
        "--config",
        help="Config file path (default: <project root>/config.json)",
    )
    parser.add_argument(
        "--generate-config",
        action="store_true",
        help="Emit all keys with defaults + descriptions",
    )
    return parser


def _split_host_port(host_port: str) -> tuple[str, int]:
    host, port = host_port.rsplit(":", 1)
    return host, int(port)


def main() -> int:
    args = build_parser().parse_args()

    if args.generate_config:
        config_path = PROJECT_ROOT / CONFIG_FILENAME
        config = {**DEFAULTS, "_schema": SCHEMA}
        write_config(config, config_path)
        print(f"Generated {config_path}")
        return 0

    # Default: serve the proxy.
    config, path = load_config(args.config)
    host, port = _split_host_port(config["listen"])
    upstream = config["upstream"]
    print(f"proxycache: serving on {host}:{port} -> {upstream} (config: {path})")
    app = make_app(upstream, config)
    web.run_app(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
