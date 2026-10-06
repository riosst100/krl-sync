"""
Run a whole sync in one process, without the Vercel time budget (local
testing, or a fallback such as a GitHub Actions job):

    python -m krl_sync [--dry] [--types schedules,trains] [--stations THB] [--limit-trains 10]

Reads the same environment variables as the Vercel function (.env is loaded
when present).
"""

import argparse
import json
import os
import sys

from . import config as cfg_module
from .app import log
from .ingest import DryIngest, IngestClient, IngestError
from .kci import KciClient
from .runner import Runner, new_state, summary


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser(prog="krl_sync", description="KCI -> krl-app ingest sync")
    parser.add_argument("--dry", action="store_true", help="fetch from KCI only, send nothing")
    parser.add_argument("--types", help="comma-separated: stations,schedules,trains")
    parser.add_argument("--stations", help="comma-separated station codes")
    parser.add_argument("--limit-trains", type=int, help="only the first N trains")
    args = parser.parse_args()

    load_dotenv()
    config = cfg_module.Config.from_env()

    try:
        ingest = DryIngest(log) if args.dry else IngestClient(config.ingest_url, config.ingest_token, log)
    except IngestError as e:
        log(str(e))
        return 2

    state = new_state(
        config,
        types=cfg_module.types(args.types) if args.types else None,
        stations=cfg_module.codes(args.stations) if args.stations else None,
        dry=args.dry,
        limit_trains=args.limit_trains,
    )
    state = Runner(config, KciClient(config, log), ingest, log).run(state)
    print(json.dumps(summary(state), indent=2))

    return 0 if state["status"] in ("success", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
