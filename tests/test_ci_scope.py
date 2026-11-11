"""Exercise conservative scope decisions and real Git rename/base handling."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def selector() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/ci_scope.py"
    spec = importlib.util.spec_from_file_location("ci_scope", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for name in selector().KNOWN_TESTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return tmp_path


@pytest.mark.parametrize(
    "path",
    [
        "src/videogenie/contracts/models.py",
        "src/videogenie/core/service.py",
        "uv.lock",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "tests/conftest.py",
        "tests/new_test.py",
        "docs/usage.md",
        "docs/provider-examples.md",
        ".github/ISSUE_TEMPLATE/config.yml",
        "unknown.txt",
        "scripts/new.py",
    ],
)
def test_shared_and_unknown_force_full(root: Path, path: str) -> None:
    module = selector()
    assert module.select([{"status": "M", "old": None, "new": path}], root)["mode"] == "full"


def test_transport_union_and_missing_mapping(root: Path) -> None:
    module = selector()
    changes = [
        {"status": "M", "old": None, "new": "src/videogenie/cli/__init__.py"},
        {"status": "M", "old": None, "new": "tests/test_media_probe.py"},
    ]
    scope = module.select(changes, root)
    assert set(scope["tests"]) == module.TRANSPORT_TESTS | {"tests/test_media_probe.py"}
    assert scope["build"] and scope["runtime"]
    (root / "tests/test_external_install.py").unlink()
    assert module.select(changes, root)["mode"] == "full"


@pytest.mark.parametrize("status", ["A", "D", "R100", "C100", "T", "U"])
def test_added_deleted_renamed_never_skip(root: Path, status: str) -> None:
    module = selector()
    scope = module.select([{"status": status, "old": "src/old.py", "new": "docs/new.md"}], root)
    assert scope["mode"] == "full"


def test_metadata_and_distribution_docs(root: Path) -> None:
    module = selector()
    scope = module.select(
        [{"status": "M", "old": None, "new": ".github/ISSUE_TEMPLATE/bug_report.yml"}], root
    )
    assert scope["mode"] == "metadata" and scope["tests"] == []
    assert not scope["build"] and not scope["runtime"]
    scope = module.select([{"status": "M", "old": None, "new": "README.md"}], root)
    assert scope["tests"] == [] and scope["build"] and scope["runtime"]
    scope = module.select(
        [{"status": "M", "old": None, "new": "docs/external-agent-proof.md"}], root
    )
    assert scope["tests"] == ["tests/test_external_install.py"]


def test_nul_diff_preserves_special_names() -> None:
    module = selector()
    changes = module.parse_changes(
        b"M\0docs/with space\nand newline.md\0R100\0old.py\0new.py\0D\0gone.py\0"
    )
    assert changes[0]["new"] == "docs/with space\nand newline.md"
    assert changes[1] == {"status": "R100", "old": "old.py", "new": "new.py"}
    assert changes[2]["new"] is None


@pytest.mark.parametrize(
    "raw", [b"M\0foo", b"R100\0one\0", b"?\0foo\0", b"M\0../foo\0", b"M\0/abs\0"]
)
def test_malformed_diff_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        selector().parse_changes(raw)


def test_real_git_modified_delete_rename_and_missing_base(tmp_path: Path) -> None:
    module = selector()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "CI test")
    git("config", "user.email", "ci@example.invalid")
    (tmp_path / "README.md").write_text("initial\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    base = git("rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("changed\n")
    git("commit", "-qam", "change")
    head = git("rev-parse", "HEAD")
    assert module.from_git(tmp_path, base, head)["mode"] == "metadata"
    assert module.from_git(tmp_path, "0" * 40, head)["mode"] == "full"
    assert module.from_git(tmp_path, "f" * 40, head)["mode"] == "full"
    assert module.from_git(tmp_path, base, head, force_full=True)["mode"] == "full"
    git("mv", "README.md", "new.md")
    git("commit", "-qm", "rename")
    renamed = git("rev-parse", "HEAD")
    assert module.from_git(tmp_path, head, renamed)["mode"] == "full"
    assert module.from_git(tmp_path, base, head, pull_request=True)["mode"] == "full"
    git("rm", "new.md")
    git("commit", "-qm", "delete")
    assert module.from_git(tmp_path, renamed, git("rev-parse", "HEAD"))["mode"] == "full"


def test_aggregate_requires_exact_required_results(root: Path) -> None:
    module = selector()
    full = module.full("test")
    good = {k: "success" for k in ("select", "qa", "build", "runtime")}
    assert module.check_results(full, good)
    for key in good:
        for state in ("failure", "cancelled", "skipped"):
            assert not module.check_results(full, {**good, key: state})
    assert not module.check_results(full, {"select": "success"})
    docs = module.select([], root)
    assert module.check_results(docs, {**good, "build": "skipped", "runtime": "skipped"})
    assert not module.check_results(docs, good)
    assert module.validate_scope(json.loads(json.dumps(full))) == full
    for field, bad in (
        ("tests", []),
        ("tests", ["--ignore=tests"]),
        ("runtime", "true"),
        ("version", 2),
        ("build", False),
        ("reasons", []),
    ):
        with pytest.raises(ValueError):
            module.validate_scope({**full, field: bad})
