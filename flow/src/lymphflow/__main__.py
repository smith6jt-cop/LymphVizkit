"""`python -m lymphflow` / `lymphflow`: run the service, or generate synthetic example data."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lymphflow", description="LymphVizkit flow cytometry workbench")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the web service (default)")
    syn = sub.add_parser("synthetic", help="write synthetic FCS files with known ground truth")
    syn.add_argument("out_dir")
    syn.add_argument("--files", type=int, default=6)
    syn.add_argument("--events", type=int, default=20000)
    syn.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    if args.cmd == "synthetic":
        from .synthetic import make_dataset

        manifest = make_dataset(args.out_dir, args.files, args.events, args.seed)
        print(f"wrote {len(manifest['files'])} FCS files and truth.json to {args.out_dir}")
        return 0

    import uvicorn

    from .app import create_app
    from .config import load_settings

    settings = load_settings()
    if settings.auth_mode == "dev" and settings.host not in ("127.0.0.1", "localhost", "::1"):
        print("refusing to start: LYMPH_FLOW_AUTH_MODE=dev (no authentication) may only bind to loopback. "
              "Set LYMPH_FLOW_AUTH_MODE=proxy with LYMPH_FLOW_USER_HEADER, or bind to 127.0.0.1.",
              file=sys.stderr)
        return 2
    print(f"lymphflow: data dir {settings.data_dir}; auth mode {settings.auth_mode}; "
          f"http://{settings.host}:{settings.port}{settings.url_prefix}/")
    # One process: the dispatcher, SQLite writer lock and array cache are per-process.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
