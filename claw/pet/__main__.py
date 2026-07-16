"""Run the SJTUClaw desktop pet."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from claw.config import DATA_DIR
from claw.pet.app import run_desktop_pet


DEFAULT_DATA_DIR = DATA_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="SJTUClaw desktop pet")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8000")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()
    return run_desktop_pet(args.gateway_url, args.data_dir)


if __name__ == "__main__":
    sys.exit(main())
