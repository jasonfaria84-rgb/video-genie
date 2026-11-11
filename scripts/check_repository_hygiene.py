"""Reject runtime state and production payloads in Git's index."""

import subprocess
from pathlib import PurePosixPath


def main() -> int:
    result = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True, text=True)
    forbidden = []
    cache_parts = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
    for name in filter(None, result.stdout.split("\0")):
        path = PurePosixPath(name)
        if (
            name.startswith(
                (
                    "productions/",
                    "media/",
                    ".videogenie/",
                    ".cleo/blobs/",
                    ".cleo/backups/",
                    ".cleo/checkpoint.git/",
                    ".cleo/session-journals/",
                )
            )
            or any(part in cache_parts or part.startswith(".venv") for part in path.parts)
            or path.name.startswith(".env")
            and path.name != ".env.example"
            or path.suffix.lower()
            in {
                ".db",
                ".sqlite",
                ".sqlite3",
                ".pyc",
                ".pem",
                ".key",
                ".mp4",
                ".mov",
                ".mxf",
                ".braw",
                ".wav",
            }
            or ".db-" in path.name
            or ".db.bak" in path.name
            or name.endswith(".cleobundle.tar.gz")
            or name
            in {
                ".cleo/config.json",
                ".cleo/project-info.json",
                ".cleo/memory-bridge.md",
                ".claude/scheduled_tasks.lock",
                ".npmrc",
                ".pnpmrc",
            }
        ):
            forbidden.append(name)
    if forbidden:
        print("Untrack these local runtime files before committing:")
        print("\n".join(forbidden))
        return 1
    print("Repository hygiene passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
