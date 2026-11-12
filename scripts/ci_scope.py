"""Conservative CI selection and aggregate validation; no third-party dependencies."""

import argparse
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import re
import shutil
import subprocess
import tomllib
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


DOCUMENTATION_ONLY = {
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/help_question.yml",
}

KNOWN_TESTS = {
    "tests/test_activity.py",
    "tests/test_artifact_preview.py",
    "tests/test_assembly.py",
    "tests/test_assets.py",
    "tests/test_authority.py",
    "tests/test_authority_cost_contracts.py",
    "tests/test_ci_scope.py",
    "tests/test_cli.py",
    "tests/test_cli_discovery.py",
    "tests/test_cli_operations.py",
    "tests/test_cli_recipes.py",
    "tests/test_costs.py",
    "tests/test_discovery.py",
    "tests/test_discovery_contracts.py",
    "tests/test_external_install.py",
    "tests/test_job_migration.py",
    "tests/test_job_storage.py",
    "tests/test_local_studio_install.py",
    "tests/test_lower_third.py",
    "tests/test_mcp.py",
    "tests/test_mcp_discovery.py",
    "tests/test_mcp_recipes.py",
    "tests/test_media_probe.py",
    "tests/test_output_review.py",
    "tests/test_owner_auth.py",
    "tests/test_owner_auth_dependency.py",
    "tests/test_owner_cli.py",
    "tests/test_owner_console.py",
    "tests/test_owner_console_ui.py",
    "tests/test_owner_output_review_ui.py",
    "tests/test_preview_contracts.py",
    "tests/test_projects.py",
    "tests/test_recipe_contracts.py",
    "tests/test_recipe_previews.py",
    "tests/test_recipe_render_install.py",
    "tests/test_recipes.py",
    "tests/test_resolve.py",
    "tests/test_silence.py",
    "tests/test_render_contracts.py",
    "tests/test_render_jobs.py",
    "tests/test_render_plan.py",
    "tests/test_studio.py",
    "tests/test_studio_contracts.py",
    "tests/test_transcription.py",
    "tests/test_transcripts.py",
}
TRANSPORT_TESTS = {
    "tests/test_cli.py",
    "tests/test_cli_discovery.py",
    "tests/test_cli_operations.py",
    "tests/test_mcp.py",
    "tests/test_mcp_discovery.py",
    "tests/test_cli_recipes.py",
    "tests/test_mcp_recipes.py",
    "tests/test_owner_cli.py",
    "tests/test_external_install.py",
    "tests/test_local_studio_install.py",
    "tests/test_recipe_render_install.py",
    "tests/test_silence.py",
    "tests/test_transcription.py",
    "tests/test_assembly.py",
}
OWNER_TESTS = {
    "tests/test_owner_auth.py",
    "tests/test_authority.py",
    "tests/test_owner_console.py",
    "tests/test_owner_console_ui.py",
    "tests/test_owner_output_review_ui.py",
}
RENDER_TESTS = (
    {
        "tests/test_lower_third.py",
        "tests/test_render_contracts.py",
        "tests/test_artifact_preview.py",
        "tests/test_recipe_previews.py",
        "tests/test_render_plan.py",
        "tests/test_render_jobs.py",
        "tests/test_output_review.py",
    }
    | OWNER_TESTS
    | TRANSPORT_TESTS
)

# Explicit shared test fixtures, including indirect consumers. The inventory test
# checks real Python imports so a new helper edge cannot silently narrow selection.
TEST_IMPORTERS = {
    "test_costs": {"test_authority"},
    "test_authority": {
        "test_cli_discovery",
        "test_discovery",
        "test_mcp_discovery",
        "test_render_plan",
        "test_mcp_recipes",
        "test_owner_output_review_ui",
        "test_output_review",
        "test_artifact_preview",
        "test_owner_console_ui",
        "test_cli_recipes",
        "test_render_jobs",
    },
    "test_owner_auth": {
        "test_render_jobs",
        "test_render_plan",
        "test_owner_console",
        "test_owner_cli",
        "test_authority",
    },
    "test_recipes": {
        "test_owner_console_ui",
        "test_recipe_previews",
        "test_render_plan",
        "test_mcp_recipes",
        "test_owner_console",
        "test_owner_cli",
        "test_cli_recipes",
        "test_authority",
    },
    "test_mcp": {"test_mcp_recipes", "test_mcp_discovery", "test_silence"},
    "test_render_plan": {
        "test_cli_discovery",
        "test_discovery",
        "test_mcp_discovery",
        "test_mcp_recipes",
        "test_owner_output_review_ui",
        "test_output_review",
        "test_artifact_preview",
        "test_owner_console_ui",
        "test_cli_recipes",
        "test_render_jobs",
    },
    "test_job_storage": {"test_owner_console", "test_owner_console_ui"},
    "test_owner_console": {"test_owner_console_ui", "test_owner_output_review_ui"},
    "test_render_contracts": {
        "test_discovery_contracts",
        "test_artifact_preview",
        "test_job_storage",
        "test_preview_contracts",
    },
    "test_render_jobs": {"test_owner_console_ui"},
    "test_cli_operations": {"test_cli_recipes", "test_cli_discovery"},
    "test_owner_cli": {"test_cli_recipes", "test_cli_discovery"},
    "test_cli_recipes": {"test_cli_discovery"},
    "test_mcp_recipes": {"test_mcp_discovery"},
    "test_output_review": {"test_discovery", "test_cli_discovery", "test_mcp_discovery"},
    "test_lower_third": {"test_recipe_previews"},
}
PATH_TESTS = {
    "src/videogenie/cli/__init__.py": TRANSPORT_TESTS,
    "src/videogenie/cli/owner.py": OWNER_TESTS | TRANSPORT_TESTS,
    "src/videogenie/adapters/mcp_server.py": TRANSPORT_TESTS,
    "src/videogenie/adapters/owner_auth.py": RENDER_TESTS,
    "src/videogenie/adapters/owner_console.py": OWNER_TESTS | TRANSPORT_TESTS,
    "src/videogenie/adapters/owner_console.html": OWNER_TESTS | TRANSPORT_TESTS,
    "src/videogenie/workers/probe.py": RENDER_TESTS
    | {
        "tests/test_media_probe.py",
        "tests/test_assets.py",
        "tests/test_projects.py",
        "tests/test_activity.py",
        "tests/test_job_storage.py",
        "tests/test_job_migration.py",
    },
    "src/videogenie/workers/lower_third.py": RENDER_TESTS,
    "src/videogenie/workers/artifact_preview.py": RENDER_TESTS,
    "src/videogenie/workers/silence.py": {"tests/test_silence.py"},
    "src/videogenie/adapters/resolve.py": {
        "tests/test_resolve.py",
        "tests/test_transcription.py",
        "tests/test_assembly.py",
    },
    "src/videogenie/core/transcription.py": {
        "tests/test_transcription.py",
        "tests/test_assembly.py",
    },
    "src/videogenie/core/assembly.py": {"tests/test_assembly.py"},
    "src/videogenie/core/transcripts.py": {
        "tests/test_transcripts.py",
        "tests/test_transcription.py",
        "tests/test_assembly.py",
    },
    "scripts/dogfood_external_agent.py": {
        "tests/test_ci_scope.py",
        "tests/test_external_install.py",
        "tests/test_local_studio_install.py",
        "tests/test_recipe_render_install.py",
    },
    "scripts/dogfood_local_studio.py": {"tests/test_local_studio_install.py"},
    "scripts/dogfood_recipe_render.py": {"tests/test_recipe_render_install.py"},
    "docs/external-agent-proof.md": {"tests/test_external_install.py"},
    "docs/local-studio-proof.md": {"tests/test_local_studio_install.py"},
    "docs/local-recipe-proof.md": {"tests/test_recipe_render_install.py"},
    "tests/fixtures/README.md": RENDER_TESTS,
    "tests/fixtures/fonts/Inter-Bold.ttf": RENDER_TESTS,
    "tests/fixtures/fonts/OFL.txt": RENDER_TESTS,
    "tests/fixtures/fonts/fonts.lock.json": RENDER_TESTS,
    "tests/fixtures/lower_third_goldens.json": {"tests/test_lower_third.py"},
}


def include_test_importers(selected: set[str]) -> set[str]:
    """Include transitive users of changed/selected test fixtures without importing them."""
    result = set(selected)
    pending = list(selected)
    while pending:
        for name in TEST_IMPORTERS.get(Path(pending.pop()).stem, set()):
            path = f"tests/{name}.py"
            if path not in result:
                result.add(path)
                pending.append(path)
    return result


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
        elif path in DOCUMENTATION_ONLY:
            continue
        elif path in KNOWN_TESTS:
            # Every test edit must admit its new helper edges through the inventory audit.
            selected.update({path, "tests/test_ci_scope.py"})
        elif path in PATH_TESTS:
            selected.update(PATH_TESTS[path])
            if path.startswith("scripts/") and path.endswith(".py"):
                selected.add("tests/test_ci_scope.py")
        else:
            return full("shared, dependency, CI, fixture or unknown path", changes)
    selected = include_test_importers(selected)
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


PACKAGE_INPUTS = (
    "src",
    "tests",
    "scripts",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    ".python-version",
    ".github/workflows/ci.yml",
)


def distribution_input_hash(root: Path) -> str:
    """Hash exact staged input paths, modes and blobs, independent of commit IDs."""
    if git(root, "diff", "--name-only", "-z", "--", *PACKAGE_INPUTS):
        raise ValueError("package inputs differ from the index")
    if git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", *PACKAGE_INPUTS):
        raise ValueError("untracked package inputs")
    entries = git(root, "ls-files", "--stage", "-z", "--", *PACKAGE_INPUTS)
    if not entries:
        raise ValueError("no package inputs")
    for entry in entries.rstrip(b"\0").split(b"\0"):
        header, separator, path = entry.partition(b"\t")
        if not separator or not path or not re.fullmatch(rb"[0-7]{6} [0-9a-f]{40,64} 0", header):
            raise ValueError("invalid or unmerged index entry")
    return hashlib.sha256(entries).hexdigest()


def distribution_names(root: Path) -> set[str]:
    """Resolve the two expected distribution names from the package metadata."""
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    name, version = project["name"], project["version"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError("invalid package name")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", version):
        raise ValueError("invalid package version")
    name = re.sub(r"[-_.]+", "_", name).lower()
    return {f"{name}-{version}-py3-none-any.whl", f"{name}-{version}.tar.gz"}


def verify_distribution(root: Path, dist: Path) -> Path:
    """Fail closed on missing, extra, symlinked or digest-mismatched cache artifacts."""
    expected = distribution_names(root)
    if dist.is_symlink() or not dist.is_dir():
        raise ValueError("distribution directory missing or symlinked")
    paths = list(dist.iterdir())
    if {p.name for p in paths} != expected | {"digests.json"}:
        raise ValueError("unexpected distribution files")
    if any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError("distribution contains non-regular files")
    manifest = json.loads((dist / "digests.json").read_text())
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise ValueError("invalid distribution manifest")
    for name in expected:
        digest = manifest[name]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid artifact digest")
        if hashlib.sha256((dist / name).read_bytes()).hexdigest() != digest:
            raise ValueError("artifact digest mismatch")
    return next(dist.glob("*.whl")).resolve()


def record_distribution(root: Path, dist: Path) -> None:
    """Record freshly built outputs, then apply the same validation as a cache hit."""
    expected = distribution_names(root)
    if dist.is_symlink() or not dist.is_dir():
        raise ValueError("build directory missing or symlinked")
    # uv creates this output-directory marker; it is not a distribution artifact.
    marker = dist / ".gitignore"
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != b"*":
            raise ValueError("unexpected build directory marker")
        marker.unlink()
    if dist.is_symlink() or {p.name for p in dist.iterdir()} != expected:
        raise ValueError("unexpected build outputs")
    if any(p.is_symlink() or not p.is_file() for p in dist.iterdir()):
        raise ValueError("build outputs must be regular files")
    manifest = {
        name: hashlib.sha256((dist / name).read_bytes()).hexdigest() for name in sorted(expected)
    }
    (dist / "digests.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    verify_distribution(root, dist)


def verify_runtime(root: Path) -> None:
    """Fail explicitly before tests if a real renderer/verifier prerequisite is unavailable."""
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"Required media tool unavailable: {tool}")
    with (root / "pyproject.toml").open("rb") as stream:
        requirements = tomllib.load(stream)["project"]["dependencies"]
    for requirement in requirements:
        name, pin = requirement.split("==")
        if importlib.metadata.version(name) != pin:
            raise RuntimeError(f"Installed runtime does not match pinned dependency: {name}")
    for module in ("PIL.ImageFont", "fontTools.ttLib", "webauthn", "cbor2", "cryptography"):
        importlib.import_module(module)
    fixture = root / "tests/fixtures/fonts"
    lock = json.loads((fixture / "fonts.lock.json").read_text())
    for part, name, key in (
        ("font", "Inter-Bold.ttf", "sha256"),
        ("license", "OFL.txt", "text_sha256"),
    ):
        raw = (fixture / name).read_bytes()
        if (
            hashlib.sha256(raw).hexdigest() != lock[part][key]
            or len(raw) != lock[part]["byte_size"]
        ):
            raise RuntimeError(f"Licensed fixture bytes do not match lock: {name}")
    # Explicit bytes and BASIC: no system font search or assumed Unicode shaping.
    from PIL import ImageFont

    font = ImageFont.truetype(
        io.BytesIO((fixture / "Inter-Bold.ttf").read_bytes()),
        42,
        layout_engine=ImageFont.Layout.BASIC,
    )
    if font.getbbox("VideoGenie")[2] <= 0:
        raise RuntimeError("Pillow FreeType/BASIC fixture font is unusable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--pull-request", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--check-results", action="store_true")
    parser.add_argument("--verify-runtime", action="store_true")
    parser.add_argument("--distribution-key", action="store_true")
    parser.add_argument("--verify-dist", type=Path)
    parser.add_argument("--record-dist", type=Path)
    args = parser.parse_args()
    if args.verify_runtime:
        verify_runtime(Path.cwd())
        return 0
    if args.distribution_key:
        digest = distribution_input_hash(Path.cwd())
        print(digest)
        if output := os.environ.get("GITHUB_OUTPUT"):
            with Path(output).open("a") as stream:
                stream.write(f"input_hash={digest}\n")
        return 0
    if args.record_dist is not None:
        record_distribution(Path.cwd(), args.record_dist)
        return 0
    if args.verify_dist is not None:
        wheel = verify_distribution(Path.cwd(), args.verify_dist)
        print(wheel)
        if output := os.environ.get("GITHUB_ENV"):
            with Path(output).open("a") as stream:
                stream.write(f"VIDEOGENIE_TEST_WHEEL={wheel}\n")
        return 0
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
