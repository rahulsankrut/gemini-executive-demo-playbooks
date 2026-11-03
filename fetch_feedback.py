#!/usr/bin/env python3
"""Fetch and format live team feedback (comments + inline edits) from a deployed Storybook server."""

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv(dotenv_path: Path):
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(ROOT / ".env")

DEFAULT_URL = os.environ.get("STORYBOOK_CLOUD_RUN_URL", "http://localhost:8847")
PASSWORD = os.environ.get("STORYBOOK_PASSWORD", "")


def main():
    base_url = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL).rstrip("/")
    headers = {"Accept": "application/json"}
    if PASSWORD:
        headers["X-Access-Password"] = PASSWORD
    req = urllib.request.Request(f"{base_url}/api/feedback", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    summary = data.get("summary", {})
    comments = data.get("comments", [])
    edits = [e for e in data.get("edit_log", []) if e.get("currentlyChanged", True)]

    print("# Gemini Executive Storybooks — Live Team Feedback Report")
    print(f"- **Source:** {base_url}/feedback")
    print(
        f"- **Totals:** {summary.get('total_comments', 0)} comments "
        f"({summary.get('open_comments', 0)} open) · "
        f"{summary.get('total_edits', 0)} inline text edits · "
        f"{len(summary.get('active_reviewers', []))} active reviewers\n"
    )

    if not comments and not edits:
        print("_No comments or inline text edits recorded yet._")
        return

    if comments:
        print("## 💬 Page Comments")
        for i, c in enumerate(comments, 1):
            status = "RESOLVED" if c.get("resolved") else "OPEN"
            loc = (
                "3-Book Rack Covers"
                if c.get("bookIdx", -1) == -1
                else f"{c.get('bookTitle')} — {c.get('slideTitle')}"
            )
            print(f"{i}. **[{status}] {loc}** — *{c.get('authorFull')}* ({c.get('createdAt')})")
            print(f"   > {c.get('text')}")
            for r in c.get("replies", []):
                print(f"   - Reply (*{r.get('authorFull')}*): {r.get('text')}")
        print()

    if edits:
        print("## ✏️ Tracked Inline Edits (Before → After)")
        for i, e in enumerate(edits, 1):
            loc = (
                "3-Book Rack Covers"
                if e.get("bookIdx", -1) == -1
                else f"{e.get('bookTitle')} — {e.get('slideTitle')}"
            )
            print(f"{i}. **{loc}** (`{e.get('editKey')}`) — *{e.get('authorFull')}* ({e.get('updatedAt')})")
            print(f"   - **Before:** {e.get('beforeText')}")
            print(f"   - **After:**  {e.get('afterText')}")


if __name__ == "__main__":
    main()
