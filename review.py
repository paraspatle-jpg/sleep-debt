#!/usr/bin/env python3
"""A PR reviewer whose strictness depends on how much you slept.

Run inside the repo, on the branch you want reviewed:

  python /path/to/review.py               # review diff vs default branch, print comments
  python /path/to/review.py --pr 42       # ...and post them as a review on PR #42 (uses `gh`)
  python /path/to/review.py --sleep 4     # pretend you slept 4h (for testing / content)
  python /path/to/review.py --hour 3      # pretend the commit was made at 3am
  python /path/to/review.py --dry-run     # show mood + diff size, no API call
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date

import anthropic

from sleep import hours_for

MODEL = "claude-opus-5-5"
MAX_DIFF_CHARS = 400_000  # refuse instead of silently truncating

# Harshest first. (max hours, persona name, persona). First match wins.
MOODS = [
    (5.0, "your dad looking at your marks",
     "You are the developer's dad looking at their report card. You are not angry, you are "
     "disappointed. Compare them to Sharma ji's son, who writes tests. Assume every change is guilty "
     "until proven innocent, nitpick naming, question every decision, and request changes unless "
     "the diff is genuinely flawless."),
    (6.0, "senior dev who got paged at 3am",
     "You are a senior engineer who got paged at 3am because of code like this. Flag anything "
     "questionable, including style and naming. Request changes for anything above a nitpick. "
     "Dry, clipped, tired tone."),
    (7.5, "tech lead the day before a release",
     "You are a tech lead the day before a release. Strict but fair: flag real bugs, risky "
     "patterns and unclear code. Approve if there are only minor issues. Brisk, professional tone."),
    (24.0, "supportive mentor",
     "You are a supportive, well-rested mentor. Flag real bugs and risks, mention what was done "
     "well, and approve unless something is actually broken. Warm tone."),
]
HR = ("HR investigation",
      "You are HR opening a formal investigation, because the developer did not log their sleep today. "
      "Write in corporate HR language ('we noticed', 'per policy', 'this has been documented'). "
      "Treat the missing sleep log as a policy violation and every code issue as further evidence. "
      "Request changes for anything above a nitpick.")
NO_LOG_HOURS = 4.0  # no sleep logged today -> assume the worst
LATE_NIGHT = range(0, 4)  # commits made 00:00-03:59 have more bugs (Eyolfson et al.)

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"]},
        "summary": {"type": "string"},
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["nit", "minor", "major", "critical"]},
                    "comment": {"type": "string"},
                },
                "required": ["file", "line", "severity", "comment"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "summary", "comments"],
    "additionalProperties": False,
}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()


def default_branch() -> str:
    try:
        return git("symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[1]
    except subprocess.CalledProcessError:
        for b in ("main", "master", "origin/main", "origin/master"):
            if subprocess.run(["git", "rev-parse", "--verify", "-q", b], capture_output=True).returncode == 0:
                return b
        sys.exit("couldn't find a default branch (main/master)")


def get_diff() -> str:
    base = git("merge-base", default_branch(), "HEAD")
    return git("diff", base, "HEAD")


def commit_hour() -> int:
    """Hour of the HEAD commit in the author's own timezone (works the same on a UTC CI runner)."""
    return int(git("log", "-1", "--format=%ad", "--date=format:%H"))


def sleep_hours() -> float | None:
    """Last night's sleep: the local log, or SLEEP_HOURS/SLEEP_DATE env vars (set by the bot's workflow)."""
    env_hours, env_date = os.environ.get("SLEEP_HOURS"), os.environ.get("SLEEP_DATE")
    if env_hours and env_date:
        return float(env_hours) if env_date == date.today().isoformat() else None  # stale → unlogged
    return hours_for(date.today())


def pick_mood(hours: float, late_night: bool) -> tuple[str, str]:
    level = next((i for i, (limit, _, _) in enumerate(MOODS) if hours < limit), len(MOODS) - 1)
    if late_night:
        level = max(0, level - 1)  # it's not personal, it's research
    _, name, persona = MOODS[level]
    return name, persona


def footer(hours: float, logged: bool, mood: str, late_night: bool, hour: int) -> str:
    sleep = f"{hours:g}h of sleep" if logged else "an unlogged night of sleep"
    late = f" · committed at {hour:02d}:xx, one level harsher" if late_night else ""
    return f"reviewed under {sleep} · mood: {mood}{late}"


def review(diff: str, persona: str, hours: float, logged: bool, late_night: bool, hour: int) -> dict:
    sleep_line = (
        f"The author slept {hours:g} hours last night."
        if logged else "The author did not log their sleep today."
    )
    late_line = (
        f" They made this commit at {hour:02d}:xx, and research shows commits made between midnight "
        "and 4am contain more bugs." if late_night else ""
    )
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=(
            f"{persona}\n\n"
            f"Context: {sleep_line}{late_line}\n\n"
            "This review is meant to be funny. Sleep is the running joke: most comments should tie the "
            "problem to how the author slept, in your persona's voice. Vary the angle so it doesn't "
            "repeat: the bug as a symptom of tiredness (\"this off-by-one has <their hours> of sleep "
            "written all over it\", using only hours given in Context), the code as a dream or sleepwalking, naps, alarms, snoozing, coffee, "
            "yawning, bedtime, counting sheep, the code reviewer itself being tired of it. Each comment "
            "should have one joke and then the real fix. Don't explain the jokes.\n\n"
            "Be thorough: find the subtle bugs, and also nitpick everything a pedantic senior would — "
            "naming, magic numbers, leftover debug prints, unused imports, missing type hints, "
            "inconsistent style, vague docstrings, swallowed exceptions. Mark these as `nit`.\n\n"
            "Rules: every comment must point at a real, specific issue in the diff. Never invent "
            "problems, and never invent facts about the author: the only things you know about them "
            "are stated in Context above (for example, don't claim a commit was late at night unless "
            "Context says so).\n"
            "Review the unified diff. Only comment on lines that were added or changed; `line` is the "
            "line number in the new version of the file. Keep each comment to 1-3 sentences."
        ),
        messages=[{"role": "user", "content": f"<diff>\n{diff}\n</diff>"}],
    )
    if response.stop_reason == "refusal":
        sys.exit("the reviewer declined to review this diff")
    if response.stop_reason == "max_tokens":
        sys.exit("review got cut off (max_tokens); try a smaller diff")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def post_to_pr(pr: int, result: dict, foot: str) -> None:
    body = f"{result['summary']}\n\n---\n<sub>{foot}</sub>"
    payload = {
        "event": result["verdict"],
        "body": body,
        "comments": [
            {"path": c["file"], "line": c["line"], "side": "RIGHT",
             "body": f"**[{c['severity']}]** {c['comment']}"}
            for c in result["comments"]
        ],
    }
    # GitHub won't let you approve/request changes on your own PR; fall back to a plain comment review.
    for event in (payload["event"], "COMMENT"):
        payload["event"] = event
        r = subprocess.run(
            ["gh", "api", "--method", "POST", f"repos/{{owner}}/{{repo}}/pulls/{pr}/reviews", "--input", "-"],
            input=json.dumps(payload), text=True, capture_output=True,
        )
        if r.returncode == 0:
            print(f"posted review ({event}) on PR #{pr}")
            return
    sys.exit(f"failed to post review: {r.stderr.strip()}")


def main() -> None:
    p = argparse.ArgumentParser(description="sleep-aware PR reviewer")
    p.add_argument("--pr", type=int, help="PR number to post the review on")
    p.add_argument("--sleep", type=float, help="override hours slept")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--hour", type=int, help="override the commit hour (0-23)")
    args = p.parse_args()

    hours = args.sleep if args.sleep is not None else sleep_hours()
    logged = hours is not None
    hour = args.hour if args.hour is not None else commit_hour()
    late_night = hour in LATE_NIGHT
    if logged:
        mood, persona = pick_mood(hours, late_night)
    else:
        hours = NO_LOG_HOURS
        mood, persona = HR
        print("no sleep logged today. HR has been notified.")
    foot = footer(hours, logged, mood, late_night, hour)
    print(foot)

    diff = get_diff()
    if not diff:
        sys.exit("no changes vs the default branch")
    if len(diff) > MAX_DIFF_CHARS:
        sys.exit(f"diff is {len(diff):,} chars; too big to review in one go")
    if args.dry_run:
        print(f"diff: {len(diff):,} chars. (dry run, not calling the API)")
        return

    result = review(diff, persona, hours, logged, late_night, hour)
    print(f"\n{result['verdict']}: {result['summary']}\n")
    for c in result["comments"]:
        print(f"  {c['file']}:{c['line']}  [{c['severity']}]  {c['comment']}")
    print(f"\n— {foot}")

    if args.pr:
        post_to_pr(args.pr, result, foot)


if __name__ == "__main__":
    main()
