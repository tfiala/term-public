#!/usr/bin/env python3
"""Helpers for hive shell prompt integration."""
from __future__ import annotations

import argparse
import json
import shutil
import sys


def pr_cli_for_origin(origin_url: str | None) -> str | None:
    if not origin_url:
        return None

    normalized = origin_url.lower()
    if "github.com" in normalized:
        return "gh" if shutil.which("gh") else None
    return "fj" if shutil.which("fj") else None


def first_pr_number(pr_json: str) -> str:
    try:
        data = json.loads(pr_json)
    except json.JSONDecodeError:
        return ""

    if isinstance(data, list) and data:
        number = data[0].get("number", "")
        if number:
            return str(number)
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    cli_parser = sub.add_parser("cli")
    cli_parser.add_argument("origin_url")

    sub.add_parser("number")

    args = parser.parse_args(argv)

    if args.command == "cli":
        cli = pr_cli_for_origin(args.origin_url)
        if cli:
            print(cli)
            return 0
        return 1

    if args.command == "number":
        number = first_pr_number(sys.stdin.read())
        if number:
            print(number)
            return 0
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
