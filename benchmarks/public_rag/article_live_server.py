"""Serve an already-provisioned loopback benchmark API with a snapshot-scoped article arm."""

import argparse
from pathlib import Path

from benchmarks.public_rag.article_live_adapter import install


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--scope", choices=("full", "qasper", "title"), required=True)
    parser.add_argument(
        "--arm",
        choices=("baseline", "article_local_passage", "full_parent4", "title_standalone"),
        required=True,
    )
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if args.arm != "baseline":
        install(args.directory, args.scope, args.arm)
    import uvicorn

    from apps.api.main import app

    uvicorn.run(app, host="127.0.0.1", port=args.port, loop="none", access_log=False)


if __name__ == "__main__":
    main()
