"""Exercise conservative scope decisions and real Git rename/base handling."""

import ast
import importlib.util
import json
import shutil
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
    assert set(scope["tests"]) == module.TRANSPORT_TESTS | {
        "tests/test_media_probe.py",
        "tests/test_ci_scope.py",
    }
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
    assert scope["mode"] == "targeted" and set(scope["tests"]) == {path, "tests/test_ci_scope.py"}
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
    assert set(scope["tests"]) == (
        {"tests/test_local_studio_install.py", "tests/test_ci_scope.py"}
        if path.startswith("scripts/")
        else {"tests/test_local_studio_install.py"}
    )
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


def test_explicit_inventory_covers_current_tests_and_helper_importers() -> None:
    """A helper import is a real test dependency, even without application changes."""
    module = selector()
    repository = Path(__file__).resolve().parents[1]
    actual: dict[str, set[str]] = {}
    paths = list((repository / "tests").rglob("*.py"))
    assert {p.relative_to(repository).as_posix() for p in paths} == module.KNOWN_TESTS
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                if name.startswith("test_"):
                    actual.setdefault(name, set()).add(path.stem)
    assert actual == module.TEST_IMPORTERS


@pytest.mark.parametrize(
    "path",
    [
        "src/videogenie/workers/probe.py",
        "src/videogenie/workers/lower_third.py",
        "src/videogenie/workers/artifact_preview.py",
        "src/videogenie/adapters/owner_auth.py",
    ],
)
def test_worker_authority_changes_reach_transitive_public_consumers(root: Path, path: str) -> None:
    scope = selector().select([{"status": "M", "old": None, "new": path}], root)
    assert scope["mode"] == "targeted"
    assert {
        "tests/test_lower_third.py",
        "tests/test_artifact_preview.py",
        "tests/test_render_jobs.py",
        "tests/test_output_review.py",
        "tests/test_owner_console_ui.py",
        "tests/test_owner_output_review_ui.py",
        "tests/test_cli_recipes.py",
        "tests/test_mcp_recipes.py",
        "tests/test_recipe_render_install.py",
        "tests/test_external_install.py",
        "tests/test_local_studio_install.py",
    } <= set(scope["tests"])
    assert scope["build"] and scope["runtime"]
    assert selector().validate_scope(scope) == scope


@pytest.mark.parametrize(
    "path",
    [
        "src/videogenie/core/discovery.py",
        "src/videogenie/contracts/discovery.py",
        "src/videogenie/core/assets.py",
        "src/videogenie/core/rendering.py",
        "src/videogenie/core/authority.py",
        "src/videogenie/contracts/rendering.py",
        "src/videogenie/contracts/output_previews.py",
        "src/videogenie/contracts/authority.py",
        "src/videogenie/workers/future_worker.py",
        "src/videogenie/cli/future_command.py",
    ],
)
def test_shared_or_unassessed_source_retains_full_coverage(root: Path, path: str) -> None:
    assert selector().select([{"status": "M", "old": None, "new": path}], root)["tests"] == [
        "tests"
    ]


@pytest.mark.parametrize(
    "path",
    [
        "scripts/dogfood_recipe_render.py",
        "docs/local-recipe-proof.md",
        "tests/test_recipe_render_install.py",
    ],
)
def test_recipe_proof_selects_same_wheel_consumer(root: Path, path: str) -> None:
    scope = selector().select([{"status": "M", "old": None, "new": path}], root)
    assert scope["mode"] == "targeted"
    assert set(scope["tests"]) == (
        {"tests/test_recipe_render_install.py", "tests/test_ci_scope.py"}
        if path.startswith(("tests/", "scripts/"))
        else {"tests/test_recipe_render_install.py"}
    )
    assert scope["build"] and scope["runtime"]
    (root / "tests/test_recipe_render_install.py").unlink()
    assert selector().select([{"status": "M", "old": None, "new": path}], root)["mode"] == "full"


def test_test_helper_changes_include_transitive_importers(root: Path) -> None:
    scope = selector().select(
        [{"status": "M", "old": None, "new": "tests/test_render_contracts.py"}], root
    )
    assert {
        "tests/test_render_contracts.py",
        "tests/test_job_storage.py",
        "tests/test_owner_console.py",
        "tests/test_owner_console_ui.py",
        "tests/test_owner_output_review_ui.py",
        "tests/test_artifact_preview.py",
        "tests/test_preview_contracts.py",
    } <= set(scope["tests"])
    assert "tests/test_costs.py" not in scope["tests"]


def test_shared_harness_selects_every_importing_installed_proof(root: Path) -> None:
    scope = selector().select(
        [{"status": "M", "old": None, "new": "scripts/dogfood_external_agent.py"}], root
    )
    assert set(scope["tests"]) == {
        "tests/test_ci_scope.py",
        "tests/test_external_install.py",
        "tests/test_local_studio_install.py",
        "tests/test_recipe_render_install.py",
    }


@pytest.mark.parametrize(
    "path",
    [
        "src/videogenie/adapters/owner_console.html",
        "src/videogenie/adapters/owner_console.py",
        "src/videogenie/cli/owner.py",
        "tests/fixtures/fonts/Inter-Bold.ttf",
        "tests/fixtures/fonts/OFL.txt",
        "tests/fixtures/fonts/fonts.lock.json",
    ],
)
def test_owner_resources_and_licensed_assets_reach_installed_proof(root: Path, path: str) -> None:
    scope = selector().select([{"status": "M", "old": None, "new": path}], root)
    assert scope["mode"] == "targeted"
    assert {
        "tests/test_owner_console_ui.py",
        "tests/test_owner_output_review_ui.py",
        "tests/test_recipe_render_install.py",
    } <= set(scope["tests"])


def test_standalone_test_modification_does_not_expand_to_unrelated_suites(root: Path) -> None:
    scope = selector().select([{"status": "M", "old": None, "new": "tests/test_assets.py"}], root)
    assert set(scope["tests"]) == {"tests/test_assets.py", "tests/test_ci_scope.py"}


def test_runtime_preflight_actual_pinned_dependencies_and_fixture() -> None:
    """Exercise the CI check with real installed packages and exact supplied font bytes."""
    selector().verify_runtime(Path(__file__).resolve().parents[1])


@pytest.mark.parametrize("damage", ["ffmpeg", "ffprobe", "version", "font", "license", "missing"])
def test_runtime_preflight_fails_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    module = selector()
    repository = Path(__file__).resolve().parents[1]
    shutil.copyfile(repository / "pyproject.toml", tmp_path / "pyproject.toml")
    fixture = tmp_path / "tests/fixtures/fonts"
    shutil.copytree(repository / "tests/fixtures/fonts", fixture)
    if damage in {"ffmpeg", "ffprobe"}:
        original = module.shutil.which
        monkeypatch.setattr(
            module.shutil, "which", lambda name: None if name == damage else original(name)
        )
        expected = "Required media tool unavailable"
    elif damage == "version":
        monkeypatch.setattr(module.importlib.metadata, "version", lambda _: "0.0.0")
        expected = "pinned dependency"
    elif damage == "missing":

        def missing(name: str) -> str:
            raise module.importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(module.importlib.metadata, "version", missing)
        with pytest.raises(module.importlib.metadata.PackageNotFoundError):
            module.verify_runtime(tmp_path)
        return
    else:
        (fixture / ("Inter-Bold.ttf" if damage == "font" else "OFL.txt")).write_bytes(b"tampered")
        expected = "Licensed fixture bytes"
    with pytest.raises(RuntimeError, match=expected):
        module.verify_runtime(tmp_path)


def test_package_cache_tracks_new_resources_and_proof_without_commit_identity(
    tmp_path: Path,
) -> None:
    module = selector()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "README.md").write_text("metadata")
    git("add", ".")
    previous = module.distribution_input_hash(tmp_path)
    for name in (
        "src/videogenie/adapters/owner_console.html",
        "tests/fixtures/fonts/Inter-Bold.ttf",
        "tests/fixtures/fonts/OFL.txt",
        "tests/fixtures/fonts/fonts.lock.json",
        "tests/fixtures/lower_third_goldens.json",
        "scripts/dogfood_recipe_render.py",
        "tests/test_recipe_render_install.py",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("exact packaged resource")
        git("add", ".")
        current = module.distribution_input_hash(tmp_path)
        assert current != previous, name
        previous = current
    doc = tmp_path / "docs/local-recipe-proof.md"
    doc.parent.mkdir()
    doc.write_text("nonpackaged evidence")
    git("add", ".")
    assert module.distribution_input_hash(tmp_path) == previous


def test_targeted_source_maps_cover_every_direct_application_import() -> None:
    """Independent source inventory catches type/helper callers omitted from grouped maps."""
    module = selector()
    repository = Path(__file__).resolve().parents[1]
    for path in (repository / "tests").glob("test_*.py"):
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for source in module.PATH_TESTS:
            if not source.startswith("src/") or not source.endswith(".py"):
                continue
            name = source.removeprefix("src/").removesuffix(".py").replace("/", ".")
            name = name.removesuffix(".__init__")
            if name in imported:
                scope = module.select([{"status": "M", "old": None, "new": source}], repository)
                assert path.relative_to(repository).as_posix() in scope["tests"], (
                    source,
                    path.name,
                )


def test_every_modified_known_test_runs_inventory_admission(root: Path) -> None:
    module = selector()
    for path in module.KNOWN_TESTS:
        scope = module.select([{"status": "M", "old": None, "new": path}], root)
        assert {path, "tests/test_ci_scope.py"} <= set(scope["tests"]), path


def test_worker_type_callers_are_not_omitted(root: Path) -> None:
    module = selector()
    for source, affected in (
        ("src/videogenie/workers/lower_third.py", "tests/test_render_contracts.py"),
        ("src/videogenie/workers/probe.py", "tests/test_assets.py"),
    ):
        assert (
            affected in module.select([{"status": "M", "old": None, "new": source}], root)["tests"]
        )


def test_harness_inventory_and_import_edges_cannot_drift() -> None:
    """New unassessed Python helpers fail admission instead of bypassing known mappings."""
    module = selector()
    repository = Path(__file__).resolve().parents[1]
    consumers = {
        "dogfood_external_agent": "tests/test_external_install.py",
        "dogfood_local_studio": "tests/test_local_studio_install.py",
        "dogfood_recipe_render": "tests/test_recipe_render_install.py",
    }
    assert {
        p.relative_to(repository).as_posix() for p in (repository / "scripts").rglob("*.py")
    } == {
        "scripts/ci_scope.py",
        "scripts/check_repository_hygiene.py",
        *(f"scripts/{name}.py" for name in consumers),
    }
    edges: dict[str, set[str]] = {}
    for name in consumers:
        path = repository / "scripts" / f"{name}.py"
        for node in ast.walk(ast.parse(path.read_text())):
            imports = []
            if isinstance(node, ast.ImportFrom) and node.module:
                imports = [node.module]
            elif isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            for imported in imports:
                if imported in consumers:
                    edges.setdefault(imported, set()).add(name)
        scope = module.select(
            [{"status": "M", "old": None, "new": f"scripts/{name}.py"}], repository
        )
        assert {consumers[name], "tests/test_ci_scope.py"} <= set(scope["tests"])
    assert edges == {"dogfood_external_agent": {"dogfood_local_studio", "dogfood_recipe_render"}}
    for helper, importers in edges.items():
        scope = module.select(
            [{"status": "M", "old": None, "new": f"scripts/{helper}.py"}], repository
        )
        assert {consumers[name] for name in importers} <= set(scope["tests"])


@pytest.mark.parametrize(
    "path",
    [
        "src/videogenie/cli/__init__.py",
        "src/videogenie/adapters/mcp_server.py",
    ],
)
def test_discovery_transports_reach_paired_and_installed_consumers(root: Path, path: str) -> None:
    module = selector()
    scope = module.select([{"status": "M", "old": None, "new": path}], root)
    assert scope["mode"] == "targeted"
    assert {
        "tests/test_cli_discovery.py",
        "tests/test_mcp_discovery.py",
        "tests/test_cli_recipes.py",
        "tests/test_mcp_recipes.py",
        "tests/test_external_install.py",
        "tests/test_local_studio_install.py",
        "tests/test_recipe_render_install.py",
    } <= set(scope["tests"])
    assert scope["build"] and scope["runtime"]
    assert module.validate_scope(scope) == scope
    (root / "tests/test_mcp_discovery.py").unlink()
    assert module.select([{"status": "M", "old": None, "new": path}], root)["mode"] == "full"


@pytest.mark.parametrize(
    "helper,expected",
    [
        ("test_authority", {"test_discovery", "test_cli_discovery", "test_mcp_discovery"}),
        ("test_costs", {"test_discovery", "test_cli_discovery", "test_mcp_discovery"}),
        ("test_render_plan", {"test_discovery", "test_cli_discovery", "test_mcp_discovery"}),
        ("test_output_review", {"test_discovery", "test_cli_discovery", "test_mcp_discovery"}),
        ("test_render_contracts", {"test_discovery_contracts"}),
        ("test_cli_operations", {"test_cli_discovery"}),
        ("test_cli_recipes", {"test_cli_discovery"}),
        ("test_owner_cli", {"test_cli_discovery"}),
        ("test_mcp", {"test_mcp_discovery"}),
        ("test_mcp_recipes", {"test_mcp_discovery"}),
    ],
)
def test_discovery_fixture_changes_follow_actual_importers(
    root: Path, helper: str, expected: set[str]
) -> None:
    scope = selector().select([{"status": "M", "old": None, "new": f"tests/{helper}.py"}], root)
    assert scope["mode"] == "targeted"
    assert {f"tests/{name}.py" for name in expected} <= set(scope["tests"])
    assert "tests/test_ci_scope.py" in scope["tests"]
    assert "tests/test_projects.py" not in scope["tests"]


@pytest.mark.parametrize(
    "suite",
    ["test_discovery", "test_discovery_contracts", "test_cli_discovery", "test_mcp_discovery"],
)
def test_discovery_suite_edits_admit_inventory_without_unrelated_product_tests(
    root: Path, suite: str
) -> None:
    path = f"tests/{suite}.py"
    scope = selector().select([{"status": "M", "old": None, "new": path}], root)
    assert scope["mode"] == "targeted"
    assert set(scope["tests"]) == {path, "tests/test_ci_scope.py"}
