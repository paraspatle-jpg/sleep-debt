#!/usr/bin/env python3
"""Manual sleep log. One entry per morning.

  python sleep.py log 5.5          # last night's sleep, in hours
  python sleep.py log 7 --date 2026-11-10
  python sleep.py                  # show the last 7 nights
"""
import argparse
import json
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path

LOG = Path(__file__).resolve().parent / "sleep.json"


def load() -> dict[str, float]:
    if not LOG.exists():
        return {}
    return json.loads(LOG.read_text())


def save(data: dict[str, float]) -> None:
    LOG.write_text(json.dumps(dict(sorted(data.items())), indent=2) + "\n")


def hours_for(day: date) -> float | None:
    """Hours slept the night before `day` (logged on the morning of `day`)."""
    return load().get(day.isoformat())


def push_to_bot(hours: float, day: str) -> None:
    """Tell the GitHub bot how you slept: sets SLEEP_HOURS/SLEEP_DATE repo variables via `gh`.

    Repos come from SLEEPY_REPOS, e.g. export SLEEPY_REPOS="you/repo-a,you/repo-b".
    """
    for repo in filter(None, os.environ.get("SLEEPY_REPOS", "").split(",")):
        ok = all(
            subprocess.run(["gh", "variable", "set", name, "--body", value, "--repo", repo.strip()],
                           capture_output=True).returncode == 0
            for name, value in (("SLEEP_HOURS", f"{hours:g}"), ("SLEEP_DATE", day))
        )
        print(f"  {'told' if ok else 'FAILED to tell'} the bot on {repo.strip()}")

    # Optional: mirror the whole log to a gist, so a website can read it (SLEEPY_GIST=<gist id>).
    gist = os.environ.get("SLEEPY_GIST")
    if gist:
        ok = subprocess.run(["gh", "gist", "edit", gist, "-f", LOG.name, str(LOG)], capture_output=True).returncode == 0
        print(f"  {'updated' if ok else 'FAILED to update'} the gist")


def main() -> None:
    p = argparse.ArgumentParser(description="manual sleep log")
    sub = p.add_subparsers(dest="cmd")
    lg = sub.add_parser("log", help="log last night's sleep")
    lg.add_argument("hours", type=float)
    lg.add_argument("--date", default=date.today().isoformat(), help="morning date (YYYY-MM-DD)")
    args = p.parse_args()

    if args.cmd == "log":
        if not 0 <= args.hours <= 24:
            p.error("hours must be between 0 and 24")
        data = load()
        data[args.date] = args.hours
        save(data)
        print(f"logged {args.hours}h for {args.date}")
        push_to_bot(args.hours, args.date)
        return

    data = load()
    today = date.today()
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        h = data.get(d)
        bar = "█" * round(h) if h is not None else "·"
        print(f"{d}  {'' if h is None else f'{h:>4}h'}{'  --  ' if h is None else '  '}{bar}")


if __name__ == "__main__":
    main()
