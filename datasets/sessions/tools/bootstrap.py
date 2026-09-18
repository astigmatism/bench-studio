"""Create a repeatable base checkout without any reference solution or private tests."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def bootstrap(root, app):
    root = Path(root)
    shutil.copytree("/opt/base", root, dirs_exist_ok=True)
    config = (
        {
            "kind": "issues",
            "title": "Issue tracker",
            "description": "Plan, assign and track your team’s work.",
        }
        if app == "issue-tracker"
        else {
            "kind": "inventory",
            "title": "Inventory desk",
            "description": "A clear view of supplies and stock.",
        }
    )
    (root / "app.json").write_text(json.dumps(config, sort_keys=True) + "\n")
    link = root / "frontend/node_modules"
    if not link.exists():
        link.symlink_to("/opt/frontend/node_modules", target_is_directory=True)
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Bench Studio",
        GIT_AUTHOR_EMAIL="fixtures@localhost",
        GIT_COMMITTER_NAME="Bench Studio",
        GIT_COMMITTER_EMAIL="fixtures@localhost",
        GIT_AUTHOR_DATE="2026-01-01T00:00:00Z",
        GIT_COMMITTER_DATE="2026-01-01T00:00:00Z",
    )
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "commit.gpgsign=false", "commit", "-qm", "Pinned fixture base"],
    ):
        subprocess.run(["git", *args], cwd=root, env=env, check=True)
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()


if __name__ == "__main__":
    print(bootstrap(sys.argv[1], sys.argv[2]))
