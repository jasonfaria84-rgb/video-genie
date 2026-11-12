"""Local DaVinci Resolve Studio scripting boundary.

VideoGenie loads Blackmagic's fusionscript library directly and talks to an already
running Resolve. It acts only on Resolve projects named after a VideoGenie project and
never switches away from a project the person has open unless the caller allows it.
"""

import importlib.machinery
import importlib.util
import os
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.resolve import ResolveStatus, UnreachableReason

DEFAULT_LIBRARIES = {
    "darwin": "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/"
    "fusionscript.so",
    "win32": "C:\\Program Files\\Blackmagic Design\\DaVinci Resolve\\fusionscript.dll",
    "linux": "/opt/resolve/libs/Fusion/fusionscript.so",
}
PROJECT_PREFIX = "VideoGenie "
FIXES: dict[UnreachableReason, str] = {
    "library_missing": "Install DaVinci Resolve Studio, or set RESOLVE_SCRIPT_LIB to its "
    "fusionscript library",
    "library_unloadable": "Reinstall DaVinci Resolve Studio; its scripting library could not load",
    "not_running_or_scripting_disabled": "Open DaVinci Resolve Studio and set Preferences > "
    "System > General > External scripting using to Local",
    "timeout": "Resolve did not answer in time; close any open dialog in Resolve and retry",
    "free_edition": "External scripting needs DaVinci Resolve Studio, not the free edition",
}
_loaded: dict[str, ModuleType] = {}
_lock = threading.Lock()

# The scripting objects are untyped native proxies.
ResolveApp = Any


def library_path() -> Path | None:
    """The fusionscript library from RESOLVE_SCRIPT_LIB or the platform default, if present."""
    configured = os.environ.get("RESOLVE_SCRIPT_LIB")
    platform = next((key for key in DEFAULT_LIBRARIES if sys.platform.startswith(key)), None)
    candidate = configured or (DEFAULT_LIBRARIES[platform] if platform else None)
    if not candidate:
        return None
    path = Path(candidate)
    return path if path.is_absolute() and path.is_file() else None


def _load(path: Path) -> ModuleType:
    with _lock:
        if str(path) in _loaded:
            return _loaded[str(path)]
        loader = importlib.machinery.ExtensionFileLoader("fusionscript", str(path))
        spec = importlib.util.spec_from_loader("fusionscript", loader)
        if spec is None:
            raise ImportError("no module spec")
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _loaded[str(path)] = module
        return module


def call_with_timeout(function: Any, timeout_seconds: float) -> tuple[bool, Any]:
    """Run a scripting call on a daemon thread; Resolve can block while a dialog is open."""
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = function()
        except Exception as exc:  # native proxies raise arbitrary errors
            outcome["error"] = exc

    worker = threading.Thread(target=target, name="videogenie-resolve", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        return False, None
    if "error" in outcome:
        return True, None
    return True, outcome.get("value")


def _unreachable(reason: UnreachableReason, path: Path | None, **known: Any) -> ResolveStatus:
    return ResolveStatus(
        reachable=False,
        product=known.get("product"),
        version=known.get("version"),
        studio=False,
        library_path=None if path is None else str(path),
        reason=reason,
        fix=FIXES[reason],
    )


def _connect(timeout_seconds: float) -> tuple[ResolveStatus, ResolveApp | None]:
    path = library_path()
    if path is None:
        return _unreachable("library_missing", None), None
    try:
        module = _load(path)
    except (ImportError, OSError):
        return _unreachable("library_unloadable", path), None
    finished, app = call_with_timeout(lambda: module.scriptapp("Resolve"), timeout_seconds)
    if not finished:
        return _unreachable("timeout", path), None
    if app is None:
        return _unreachable("not_running_or_scripting_disabled", path), None
    finished, facts = call_with_timeout(
        lambda: (str(app.GetProductName()), str(app.GetVersionString())), timeout_seconds
    )
    if not finished:
        return _unreachable("timeout", path), None
    if not isinstance(facts, tuple):
        return _unreachable("not_running_or_scripting_disabled", path), None
    product, version = facts
    if "Studio" not in product:
        return _unreachable("free_edition", path, product=product, version=version), None
    return (
        ResolveStatus(
            reachable=True,
            product=product,
            version=version,
            studio=True,
            library_path=str(path),
            reason=None,
            fix=None,
        ),
        app,
    )


def resolve_status(timeout_seconds: float) -> ResolveStatus:
    """Report reachability without changing anything in Resolve."""
    return _connect(timeout_seconds)[0]


def connect(timeout_seconds: float) -> ResolveApp:
    """Return the running Resolve Studio, or a typed error explaining how to fix it."""
    status, app = _connect(timeout_seconds)
    if app is None:
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE",
            "DaVinci Resolve Studio is not available for scripting",
            details={"reason": status.reason, "fix": status.fix},
        )
    return app


def project_name(project_id: str) -> str:
    """The only Resolve project name VideoGenie edits for a VideoGenie project."""
    return PROJECT_PREFIX + project_id


def open_project(app: ResolveApp, project_id: str, *, allow_switch: bool) -> Any:
    """Open or create the VideoGenie project's own Resolve project.

    If the person has a different project open, refuse unless allow_switch is true, so an
    agent never closes someone's work in progress by surprise.
    """
    name = project_name(project_id)
    manager = app.GetProjectManager()
    if manager is None:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve project manager is unavailable")
    current = manager.GetCurrentProject()
    if current is not None and current.GetName() == name:
        return current
    if current is not None and not allow_switch:
        raise VideoGenieError(
            "LEASE_HELD",
            "Resolve has another project open; save it and retry with allow_switch true",
            details={"open_project": str(current.GetName()), "needed_project": name},
        )
    project = manager.LoadProject(name)
    if project is None:
        project = manager.CreateProject(name)
    if project is None or project.GetName() != name:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not open the project")
    return project
