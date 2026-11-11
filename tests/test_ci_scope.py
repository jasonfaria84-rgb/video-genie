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


@pytest.mark.parametrize("change", ["content", "rename", "delete", "mode"])
def test_distribution_key_tracks_index_inputs(tmp_path: Path, change: str) -> None:
    module = selector()

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=tmp_path)

    git("init", "-q")
    source = tmp_path / "src" / "sample.py"
    source.parent.mkdir()
    source.write_text("original\n")
    (tmp_path / "README.md").write_text("readme\n")
    git("add", ".")
    before = module.distribution_input_hash(tmp_path)
    if change == "content":
        source.write_text("changed\n")
        with pytest.raises(ValueError, match="differ from the index"):
            module.distribution_input_hash(tmp_path)
        git("add", ".")
    elif change == "rename":
        git("mv", "src/sample.py", "src/renamed.py")
    elif change == "delete":
        source.unlink()
        git("add", "-u")
    else:
        source.chmod(0o755)
        git("add", ".")
    assert module.distribution_input_hash(tmp_path) != before


def test_distribution_key_reuses_only_unchanged_package_inputs(tmp_path: Path) -> None:
    module = selector()

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=tmp_path)

    git("init", "-q")
    git("config", "user.name", "CI test")
    git("config", "user.email", "ci@example.invalid")
    (tmp_path / "README.md").write_text("packaged metadata\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    first = module.distribution_input_hash(tmp_path)
    (tmp_path / "AGENTS.md").write_text("development guidance\n")
    git("add", ".")
    git("commit", "-qm", "non-package docs")
    assert module.distribution_input_hash(tmp_path) == first
    for name in module.PACKAGE_INPUTS:
        if name in {"src", "tests", "scripts"}:
            path = tmp_path / name / "added.py"
        else:
            path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("new package input\n")
        git("add", ".")
        current = module.distribution_input_hash(tmp_path)
        assert current != first, name
        first = current
    untracked = tmp_path / "src" / "untracked.py"
    untracked.write_text("untracked\n")
    with pytest.raises(ValueError, match="untracked"):
        module.distribution_input_hash(tmp_path)


@pytest.fixture
def distributions(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "video-genie"\nversion = "0.1.0"\n')
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / ".gitignore").write_bytes(b"*")
    for name in selector().distribution_names(tmp_path):
        (dist / name).write_bytes(b"test artifact bytes")
    selector().record_distribution(tmp_path, dist)
    return tmp_path, dist


def test_distribution_validation_roundtrip(distributions: tuple[Path, Path]) -> None:
    root, dist = distributions
    wheel = selector().verify_distribution(root, dist)
    assert wheel == (dist / "video_genie-0.1.0-py3-none-any.whl").resolve()


@pytest.mark.parametrize(
    "damage", ["missing", "extra", "wrong-name", "bytes", "manifest", "digest", "symlink"]
)
def test_distribution_validation_fails_closed(
    distributions: tuple[Path, Path], damage: str
) -> None:
    root, dist = distributions
    wheel = dist / "video_genie-0.1.0-py3-none-any.whl"
    manifest = dist / "digests.json"
    if damage == "missing":
        wheel.unlink()
    elif damage == "extra":
        (dist / "unexpected.whl").write_bytes(b"extra")
    elif damage == "wrong-name":
        wheel.rename(dist / "video_genie-0.2.0-py3-none-any.whl")
    elif damage == "bytes":
        wheel.write_bytes(b"tampered")
    elif damage == "manifest":
        manifest.write_text('{"../escape": "wrong"}')
    elif damage == "digest":
        data = json.loads(manifest.read_text())
        data[wheel.name] = "wrong"
        manifest.write_text(json.dumps(data))
    else:
        target = root / "outside.whl"
        wheel.rename(target)
        wheel.symlink_to(target)
    with pytest.raises(ValueError):
        selector().verify_distribution(root, dist)


@pytest.mark.parametrize(
    "path", ["src/videogenie/cli/__init__.py", "src/videogenie/adapters/mcp_server.py"]
)
def test_transport_edits_cover_both_installed_consumers(root: Path, path: str) -> None:
    module = selector()
    changes = [{"status": "M", "old": None, "new": path}]
    scope = module.select(changes, root)
    assert scope["mode"] == "targeted"
    assert {"tests/test_external_install.py", "tests/test_local_studio_install.py"} <= set(
        scope["tests"]
    )
    (root / "tests/test_local_studio_install.py").unlink()
    assert module.select(changes, root)["mode"] == "full"


@pytest.mark.parametrize(
    "name",
    [
        "test_studio_contracts.py",
        "test_studio.py",
        "test_activity.py",
        "test_local_studio_install.py",
    ],
)
def test_studio_tests_are_known_and_selectable(root: Path, name: str) -> None:
    path = "tests/" + name
    module = selector()
    scope = module.select([{"status": "M", "old": None, "new": path}], root)
    assert scope["mode"] == "targeted" and scope["tests"] == [path]
    assert module.validate_scope(scope) == scope
    (root / path).unlink()
    assert module.select([{"status": "M", "old": None, "new": path}], root)["mode"] == "full"


@pytest.mark.parametrize(
    "path",
    [
        "src/videogenie/contracts/studio.py",
        "src/videogenie/core/studio.py",
        "src/videogenie/core/activity.py",
    ],
)
def test_studio_shared_contracts_and_core_preserve_full_fallback(root: Path, path: str) -> None:
    scope = selector().select([{"status": "M", "old": None, "new": path}], root)
    assert scope["mode"] == "full" and scope["tests"] == ["tests"]


@pytest.mark.parametrize("path", ["scripts/dogfood_local_studio.py", "docs/local-studio-proof.md"])
def test_studio_harness_and_proof_select_installed_consumer(root: Path, path: str) -> None:
    module = selector()
    changes = [{"status": "M", "old": None, "new": path}]
    scope = module.select(changes, root)
    assert scope["mode"] == "targeted"
    assert scope["tests"] == ["tests/test_local_studio_install.py"]
    (root / "tests/test_local_studio_install.py").unlink()
    assert module.select(changes, root)["mode"] == "full"


def test_legacy_discovery_requires_original_tools_and_allows_additions() -> None:
    path = Path(__file__).resolve().parents[1] / "scripts/dogfood_external_agent.py"
    spec = importlib.util.spec_from_file_location("legacy_consumer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    required = {
        "videogenie_capabilities",
        "videogenie_project_create",
        "videogenie_project_list",
        "videogenie_project_get",
        "videogenie_project_rename",
        "videogenie_project_archive",
        "videogenie_project_restore",
        "videogenie_media_probe",
        "videogenie_job_get",
        "videogenie_job_resume",
    }
    assert module.LEGACY_TOOLS == required
    additions = {"videogenie_client_create", "videogenie_project_duration"}
    module.require_legacy_tools(required)
    module.require_legacy_tools(required | additions)
    for missing in required:
        with pytest.raises(AssertionError, match="Missing legacy tools"):
            module.require_legacy_tools((required - {missing}) | additions)
