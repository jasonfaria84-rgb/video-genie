"""Conservative CI selection and aggregate validation; no third-party dependencies."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict


class Change(TypedDict):
    status: str
    old: str | None
    new: str | None


class Scope(TypedDict):
    version: int
    mode: Literal["full", "targeted", "metadata"]
    tests: list[str]
    build: bool
    runtime: bool
    reasons: list[str]
    changed: list[Change]


KNOWN_TESTS = {
    "tests/test_cli.py",
    "tests/test_cli_operations.py",
    "tests/test_mcp.py",
    "tests/test_media_probe.py",
    "tests/test_projects.py",
    "tests/test_external_install.py",
    "tests/test_ci_scope.py",
}
TRANSPORT_TESTS = {
    "tests/test_cli.py",
    "tests/test_cli_operations.py",
    "tests/test_mcp.py",
    "tests/test_external_install.py",
}


def parse_changes(raw: bytes) -> list[Change]:
    if not raw:
        return []
    parts = raw.decode("utf-8", errors="strict").split("\0")
    if parts.pop() != "":
        raise ValueError("unterminated diff")
    changes: list[Change] = []
    while parts:
        status = parts.pop(0)
        count = 2 if status.startswith(("R", "C")) else 1
        if len(parts) < count or not re.fullmatch(r"[AMDTUXB]|[RC][0-9]+", status):
            raise ValueError("invalid diff status")
        paths = [parts.pop(0) for _ in range(count)]
        if any(not p or PurePosixPath(p).is_absolute() or ".." in p.split("/") for p in paths):
            raise ValueError("invalid diff path")
        changes.append(
            {
                "status": status,
                "old": paths[0] if count == 2 or status == "D" else None,
                "new": paths[-1] if status != "D" else None,
            }
        )
    return changes


def full(reason: str, changes: list[Change] | None = None) -> Scope:
    return {
        "version": 1,
        "mode": "full",
        "tests": ["tests"],
        "build": True,
        "runtime": True,
        "reasons": [reason],
        "changed": changes or [],
    }


def select(changes: list[Change], root: Path) -> Scope:
    selected: set[str] = set()
    build = False
    for change in changes:
        path = change["new"]
        if change["status"] != "M" or path is None:
            return full("added/deleted/renamed or unsupported change", changes)
        if path in {"README.md", "LICENSE"}:
            build = True
        elif path == "docs/external-agent-proof.md":
            selected.add("tests/test_external_install.py")
        elif path.startswith("docs/") and path.endswith(".md"):
            continue
        elif path in KNOWN_TESTS:
            selected.add(path)
        elif (
            path.startswith("src/videogenie/cli/")
            or path == "src/videogenie/adapters/mcp_server.py"
        ):
            selected.update(TRANSPORT_TESTS)
        elif path.startswith("src/videogenie/workers/"):
            selected.update(
                TRANSPORT_TESTS | {"tests/test_media_probe.py", "tests/test_projects.py"}
            )
        elif path == "scripts/dogfood_external_agent.py":
            selected.add("tests/test_external_install.py")
        else:
            return full("shared, dependency, CI, fixture or unknown path", changes)
    if any(not (root / test).is_file() for test in selected):
        return full("required mapped suite missing", changes)
    build = build or bool(selected)
    return {
        "version": 1,
        "mode": "targeted" if selected else "metadata",
        "tests": sorted(selected),
        "build": build,
        "runtime": build,
        "reasons": ["verified changed-path selection"],
        "changed": changes,
    }


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)


def from_git(
    root: Path,
    base: str,
    head: str,
    *,
    pull_request: bool = False,
    tested: str = "HEAD",
    force_full: bool = False,
) -> Scope:
    if force_full:
        return full("explicit full validation")
    if not re.fullmatch(r"[0-9a-f]{40,64}", base) or set(base) == {"0"}:
        return full("missing or invalid base")
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        return full("missing or invalid head")
    try:
        git(root, "cat-file", "-e", base + "^{commit}")
        git(root, "cat-file", "-e", head + "^{commit}")
        if pull_request:
            base = git(root, "merge-base", base, head).decode().strip()
            if git(root, "diff", "--name-only", "-z", head, tested, "--"):
                return full("merge tree differs from selected head")
        raw = git(root, "diff", "--name-status", "-z", "--find-renames", base, head, "--")
        return select(parse_changes(raw), root)
    except (subprocess.CalledProcessError, OSError, UnicodeError, ValueError):
        return full("unavailable or invalid complete diff")


def validate_scope(value: object) -> Scope:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "mode",
        "tests",
        "build",
        "runtime",
        "reasons",
        "changed",
    }:
        raise ValueError("invalid scope fields")
    if value["version"] != 1 or value["mode"] not in {"full", "targeted", "metadata"}:
        raise ValueError("invalid scope version/mode")
    tests = value["tests"]
    if not isinstance(tests, list) or any(not isinstance(t, str) for t in tests):
        raise ValueError("invalid tests")
    if any(t != "tests" and t not in KNOWN_TESTS for t in tests):
        raise ValueError("unmapped test argument")
    if type(value["build"]) is not bool or type(value["runtime"]) is not bool:
        raise ValueError("invalid job flags")
    if value["mode"] == "full" and tests != ["tests"]:
        raise ValueError("full selection must cover all tests")
    if value["mode"] == "targeted" and (not tests or "tests" in tests):
        raise ValueError("targeted selection is empty or broad")
    if value["mode"] == "metadata" and tests:
        raise ValueError("metadata selection contains tests")
    if value["runtime"] != value["build"] or (tests and not value["build"]):
        raise ValueError("inconsistent required jobs")
    reasons = value["reasons"]
    if not isinstance(reasons, list) or not reasons or any(not isinstance(r, str) for r in reasons):
        raise ValueError("invalid reasons")
    changes = value["changed"]
    if not isinstance(changes, list):
        raise ValueError("invalid changes")
    # Reconstruct a typed record after validation rather than casting untrusted JSON.
    return {
        "version": 1,
        "mode": value["mode"],
        "tests": tests,
        "build": value["build"],
        "runtime": value["runtime"],
        "reasons": reasons,
        "changed": changes,
    }


def check_results(scope: Scope, results: dict[str, str]) -> bool:
    expected = {"select": True, "qa": True, "build": scope["build"], "runtime": scope["runtime"]}
    return set(results) == set(expected) and all(
        results[job] == ("success" if required else "skipped") for job, required in expected.items()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--pull-request", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--check-results", action="store_true")
    args = parser.parse_args()
    if args.check_results:
        try:
            scope = validate_scope(json.loads(os.environ["SCOPE_JSON"]))
            needs = json.loads(os.environ["NEEDS_JSON"])
            return 0 if check_results(scope, {k: v["result"] for k, v in needs.items()}) else 1
        except (KeyError, TypeError, ValueError):
            return 1
    scope = from_git(
        Path.cwd(), args.base, args.head, pull_request=args.pull_request, force_full=args.full
    )
    encoded = json.dumps(scope, separators=(",", ":"))
    print(encoded)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"scope={encoded}\n")
            stream.write(f"build={str(scope['build']).lower()}\n")
            stream.write(f"runtime={str(scope['runtime']).lower()}\n")
            stream.write(f"mode={scope['mode']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
