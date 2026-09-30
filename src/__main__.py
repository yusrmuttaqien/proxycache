"""proxycache entry point.

Default (no flags): serve the proxy. Flags: --generate-config, --config, --version.
"""

from __future__ import annotations

import argparse

from importlib.metadata import PackageNotFoundError, version

from src.config import generate_config_text, load_config

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


def main() -> int:
    args = build_parser().parse_args()

    if args.generate_config:
        print(generate_config_text())
        return 0

    # Default: serve the proxy.
    config, path = load_config(args.config)
    # Phase 0 item 5 (transparent forward) starts the serve loop here.
    print(f"proxycache: config loaded from {path}")
    print(f"proxycache: listen={config['listen']} upstream={config['upstream']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
