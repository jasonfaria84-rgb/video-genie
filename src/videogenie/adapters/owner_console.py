"""Bounded loopback HTTP transport for the explicitly launched trusted owner console.

No HTTP route mints bootstrap/recovery admission. Same-account arbitrary code is
outside this boundary; a browser session never replaces per-action WebAuthn proof.
"""

import base64
import difflib
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from pydantic import ValidationError
from webauthn.helpers import bytes_to_base64url

from videogenie.adapters.owner_auth import EnrollmentTicket
from videogenie.contracts.authority import (
    IssueGrantAction,
    OwnerActionRequest,
    OwnerChallengeInput,
    PublishBrandAction,
    PublishRecipeAction,
    RegisterOwnerInput,
    VerifyOwnerActionInput,
)
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.recipes import (
    BrandDraft,
    BrandDraftInput,
    BrandVersionInput,
    PublishedBrand,
    PublishedRecipe,
    RecipeDraft,
    RecipeDraftInput,
    RecipeVersionInput,
    content_sha256,
)
from videogenie.core.authority import OwnerAuthorityService
from videogenie.core.job_storage import JobStorage
from videogenie.core.service import validate_identifier

_COOKIE = "videogenie_owner"
_SESSION_SECONDS = 600
_LAUNCH_SECONDS = 120
_MAX_BODY = 1048576
_MAX_SESSIONS = 16


def _clock() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class ConsoleLaunch:
    """Deliver only to the intentionally launched owner browser; never log/serialize."""

    url: str = field(repr=False)
    token: str = field(repr=False)


@dataclass
class _Session:
    csrf: str = field(repr=False)
    expires_at: float
    enrollment: EnrollmentTicket | None = field(default=None, repr=False)
    selected_challenge: str | None = None
    selected_digest: str | None = None
    enrollment_completed: bool = False


@dataclass
class _Launch:
    expires_at: float
    enrollment: EnrollmentTicket | None = field(repr=False)


class _HttpError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        self.status, self.reason = status, reason


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


class OwnerConsole:
    """Bind one stable persisted HTTP localhost origin; start/stop explicitly.

    open_session is a trusted local-launch primitive, not an agent-facing service.
    T290 must intentionally initiate the owner flow and privately deliver the URL.
    Supervised enrollment/recovery tickets must be obtained separately through
    OwnerAuth; neither an actor flag nor a public HTTP request can create them.
    """

    def __init__(self, authority: OwnerAuthorityService) -> None:
        self.authority = authority
        self.origin = authority.auth.origin
        parsed = urlsplit(self.origin)
        if parsed.scheme != "http" or parsed.hostname != "localhost" or parsed.port is None:
            raise VideoGenieError(
                "INVALID_ARGUMENT", "Console requires persisted HTTP localhost origin"
            )
        self.host = parsed.netloc
        self._lock = threading.RLock()
        self._launches: dict[str, _Launch] = {}
        self._sessions: dict[str, _Session] = {}
        console = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                self.request.settimeout(5)
                super().setup()

            def log_message(self, format: str, *args: object) -> None:
                # Base logging includes URLs and untrusted request text.
                return

            def do_GET(self) -> None:
                console._handle(self)

            def do_POST(self) -> None:
                console._handle(self)

            def send_error(
                self, code: int, message: str | None = None, explain: str | None = None
            ) -> None:
                console._json(self, code, {"ok": False, "error": {"reason": "invalid_http"}})

        try:
            self.server = HTTPServer(("127.0.0.1", parsed.port), Handler)
        except OSError as exc:
            raise VideoGenieError(
                "ACCESS_DENIED", "Cannot bind owner console loopback port"
            ) from exc

    def open_session(self, *, enrollment: EnrollmentTicket | None = None) -> ConsoleLaunch:
        """One-use admission for explicit trusted owner launch, optionally supervised enrollment."""
        if enrollment is not None and (
            enrollment.challenge.application_instance_id
            != self.authority.auth.application_instance_id
            or enrollment.challenge.origin != self.origin
        ):
            raise VideoGenieError(
                "INVALID_ARGUMENT", "Enrollment ticket belongs to another console"
            )
        with self._lock:
            self._purge()
            if len(self._launches) + len(self._sessions) >= _MAX_SESSIONS:
                raise VideoGenieError("ACCESS_DENIED", "Owner console session capacity reached")
            token = secrets.token_urlsafe(32)
            self._launches[token] = _Launch(_clock() + _LAUNCH_SECONDS, enrollment)
        return ConsoleLaunch(self.origin + "/#launch=" + token, token)

    def serve_forever(self) -> None:
        """Run on the caller's dedicated thread/process; socket is already loopback-bound."""
        self.server.serve_forever(poll_interval=0.1)

    def close(self) -> None:
        """Call after stopping serve_forever via server.shutdown from its supervising thread."""
        self.server.server_close()
        with self._lock:
            self._sessions.clear()
            self._launches.clear()

    def _purge(self) -> None:
        now = _clock()
        self._launches = {
            key: item for key, item in self._launches.items() if item.expires_at > now
        }
        self._sessions = {
            key: item for key, item in self._sessions.items() if item.expires_at > now
        }

    @staticmethod
    def _header(handler: BaseHTTPRequestHandler, name: str, *, required: bool = True) -> str | None:
        values = handler.headers.get_all(name, [])
        if len(values) > 1 or (required and len(values) != 1):
            raise _HttpError(403, "ambiguous_or_missing_header")
        return values[0] if values else None

    def _guard(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.client_address[0] != "127.0.0.1" or self._header(handler, "Host") != self.host:
            raise _HttpError(403, "host_denied")
        origin = self._header(handler, "Origin", required=handler.command == "POST")
        if origin is not None and origin != self.origin:
            raise _HttpError(403, "origin_denied")
        site = self._header(handler, "Sec-Fetch-Site", required=False)
        if site is not None and site not in {"none", "same-origin"}:
            raise _HttpError(403, "cross_site")
        if "?" in handler.path or "#" in handler.path or "%" in handler.path:
            raise _HttpError(400, "invalid_route")
        if handler.headers.get_all("Transfer-Encoding"):
            raise _HttpError(400, "unsupported_transfer")

    def _body(self, handler: BaseHTTPRequestHandler) -> str:
        if self._header(handler, "Content-Type") != "application/json":
            raise _HttpError(415, "json_required")
        length = self._header(handler, "Content-Length")
        if (
            length is None
            or not length.isascii()
            or not length.isdecimal()
            or not 0 < int(length) <= _MAX_BODY
        ):
            raise _HttpError(413, "body_limit")
        raw = handler.rfile.read(int(length))
        if len(raw) != int(length):
            raise _HttpError(400, "incomplete_body")
        try:
            text = raw.decode("utf-8")
            value = json.loads(text, object_pairs_hook=_pairs)
            if not isinstance(value, dict):
                raise ValueError("Object required")
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise _HttpError(400, "invalid_json") from exc
        return text

    def _session(self, handler: BaseHTTPRequestHandler) -> tuple[str, _Session]:
        cookie = self._header(handler, "Cookie")
        if cookie is None:
            raise _HttpError(403, "session_required")
        pairs = [part.strip().split("=", 1) for part in cookie.split(";")]
        values = [part[1] for part in pairs if len(part) == 2 and part[0] == _COOKIE]
        if len(values) != 1:
            raise _HttpError(403, "ambiguous_cookie")
        self._purge()
        session = self._sessions.get(values[0])
        if session is None:
            raise _HttpError(403, "session_expired")
        if handler.command == "POST":
            csrf = self._header(handler, "X-CSRF-Token")
            if (
                csrf is None
                or not csrf.isascii()
                or len(csrf) != 43
                or not hmac.compare_digest(csrf, session.csrf)
            ):
                raise _HttpError(403, "csrf_denied")
        return values[0], session

    @staticmethod
    def _headers(
        handler: BaseHTTPRequestHandler,
        status: int,
        mime: str,
        length: int,
        *,
        cookie: str | None = None,
        page_csp: str | None = None,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(length))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header(
            "Content-Security-Policy",
            page_csp
            or "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' blob:; media-src 'self' blob:; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'",
        )
        handler.send_header("Connection", "close")
        if cookie is not None:
            handler.send_header("Set-Cookie", cookie)
        handler.end_headers()
        handler.close_connection = True

    def _json(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        value: object,
        *,
        cookie: str | None = None,
    ) -> None:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        self._headers(handler, status, "application/json", len(raw), cookie=cookie)
        handler.wfile.write(raw)

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            self._guard(handler)
            if handler.command == "GET" and handler.path == "/":
                page = files("videogenie.adapters").joinpath("owner_console.html")
                if not page.is_file():
                    self._json(handler, 503, {"ok": False, "error": {"reason": "ui_not_installed"}})
                    return
                raw = page.read_bytes()
                # Only these exact bundled inline bytes execute; no unsafe-inline/eval.
                text = raw.decode("utf-8")
                policies = []
                for tag in ("script", "style"):
                    blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
                    hashes = " ".join(
                        "'sha256-"
                        + base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
                        + "'"
                        for block in blocks
                    )
                    policies.append(f"{tag}-src {hashes or chr(39) + 'none' + chr(39)}")
                csp = (
                    "default-src 'none'; "
                    + "; ".join(policies)
                    + "; connect-src 'self'; img-src 'self' blob:; media-src 'self' blob:; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                )
                self._headers(handler, 200, "text/html; charset=utf-8", len(raw), page_csp=csp)
                handler.wfile.write(raw)
                return
            if handler.command == "POST":
                body = self._body(handler)
            else:
                body = "{}"
            with self._lock:
                if handler.path == "/session" and handler.command == "POST":
                    if json.loads(body) != {}:
                        raise _HttpError(400, "unexpected_fields")
                    token = self._header(handler, "X-Console-Launch")
                    self._purge()
                    launch = self._launches.pop(token or "", None)
                    if launch is None:
                        raise _HttpError(403, "launch_denied")
                    session_token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    self._sessions[session_token] = _Session(
                        csrf, _clock() + _SESSION_SECONDS, launch.enrollment
                    )
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "csrf": csrf,
                            "application_instance_id": self.authority.auth.application_instance_id,
                            "enrollment_available": launch.enrollment is not None,
                        },
                        cookie=(
                            f"{_COOKIE}={session_token}; HttpOnly; SameSite=Strict; Path=/; "
                            f"Max-Age={_SESSION_SECONDS}"
                        ),
                    )
                    return
                token, session = self._session(handler)
                if handler.command == "GET" and handler.path.startswith("/artifacts/"):
                    self._artifact(handler)
                    return
                if handler.command != "POST":
                    raise _HttpError(404, "route_not_found")
                if handler.path == "/logout":
                    if json.loads(body) != {}:
                        raise _HttpError(400, "unexpected_fields")
                    del self._sessions[token]
                    self._json(
                        handler,
                        200,
                        {"ok": True},
                        cookie=f"{_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
                    )
                    return
                if handler.path == "/enrollment/options":
                    if (
                        json.loads(body) != {}
                        or session.enrollment is None
                        or session.enrollment_completed
                    ):
                        raise _HttpError(403, "enrollment_not_admitted")
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "challenge": session.enrollment.challenge.model_dump(mode="json"),
                            "next_recovery_secret": bytes_to_base64url(
                                session.enrollment.next_recovery_secret
                            ),
                        },
                    )
                    return
                if handler.path == "/enrollment/complete":
                    ticket = session.enrollment
                    if ticket is None:
                        raise _HttpError(403, "enrollment_not_admitted")
                    request = RegisterOwnerInput.model_validate_json(body)
                    if (
                        request.challenge_id != ticket.challenge.challenge_id
                        or request.application_instance_id
                        != ticket.challenge.application_instance_id
                    ):
                        raise _HttpError(403, "enrollment_scope")
                    receipt = self.authority.auth.complete_enrollment(request, bearer=ticket.bearer)
                    session.enrollment_completed = True
                    self._sessions = {token: session}
                    self._launches.clear()
                    self._json(handler, 200, {"ok": True, "data": receipt.model_dump(mode="json")})
                    return
                if handler.path == "/action/request":
                    challenge = self.authority.request_action(
                        OwnerActionRequest.model_validate_json(body)
                    )
                    self._json(
                        handler, 200, {"ok": True, "data": challenge.model_dump(mode="json")}
                    )
                    return
                if handler.path == "/action/review":
                    challenge = self.authority.get_challenge(
                        OwnerChallengeInput.model_validate_json(body)
                    )
                    action = challenge.action
                    draft: RecipeDraft | BrandDraft | None = None
                    if isinstance(action, PublishRecipeAction):
                        draft = self.authority.recipes.get_recipe_draft(
                            RecipeDraftInput(recipe_id=action.recipe_id)
                        )
                    elif isinstance(action, PublishBrandAction):
                        draft = self.authority.recipes.get_brand_draft(
                            BrandDraftInput(brand_id=action.brand_id, client_id=action.client_id)
                        )
                    if draft is not None and isinstance(
                        action, (PublishRecipeAction, PublishBrandAction)
                    ):
                        if (
                            draft.draft_revision != action.expected_draft_revision
                            or draft.content_sha256 != action.content_sha256
                        ):
                            raise _HttpError(409, "review_draft_changed")
                    baseline: PublishedRecipe | PublishedBrand | None = None
                    if draft is not None:
                        identity = (
                            draft.recipe_id if isinstance(draft, RecipeDraft) else draft.brand_id
                        )
                        with self.authority.service.connection() as connection:
                            row = connection.execute(
                                "SELECT version_id FROM content_versions WHERE identity_id=? "
                                "ORDER BY ordinal DESC LIMIT 1",
                                (identity,),
                            ).fetchone()
                        if row is not None:
                            if isinstance(draft, RecipeDraft):
                                baseline = self.authority.recipes.get_recipe_version(
                                    RecipeVersionInput(recipe_id=identity, version_id=row[0])
                                )
                            else:
                                baseline = self.authority.recipes.get_brand_version(
                                    BrandVersionInput(
                                        brand_id=identity,
                                        client_id=draft.client_id,
                                        version_id=row[0],
                                    )
                                )
                    summary = []
                    if isinstance(action, IssueGrantAction):
                        terms = action.terms
                        summary = [
                            {"label": label, "value": str(value)}
                            for label, value in (
                                ("Project", terms.project_id),
                                ("Storyline", terms.storyline_id),
                                ("Client", terms.client_id),
                                ("Maximum runs", terms.max_runs),
                                ("Attempts per run", terms.max_attempts_per_run),
                                ("Concurrent runs", terms.max_concurrent),
                                ("Valid from", terms.valid_from),
                                ("Expires", terms.expires_at),
                                ("Recipe version", terms.recipe.version_id),
                                ("Brand version", terms.brand.version_id),
                                ("Provider", ", ".join(terms.allowed_providers)),
                                ("Capabilities", ", ".join(terms.allowed_capabilities)),
                            )
                        ]
                        for cap in terms.monetary_caps_by_currency:
                            scale = cap.currency.exponent
                            digits = str(cap.minor_units).zfill(scale + 1)
                            amount = digits[:-scale] + "." + digits[-scale:] if scale else digits
                            summary.append(
                                {
                                    "label": "Budget cap · " + cap.currency.code,
                                    "value": amount
                                    + " ("
                                    + str(cap.minor_units)
                                    + " minor units; exponent "
                                    + str(scale)
                                    + ")",
                                }
                            )
                    before = baseline.content.model_dump_json(indent=2) if baseline else ""
                    after = draft.content.model_dump_json(indent=2) if draft else ""
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "data": challenge.model_dump(mode="json"),
                            "action_json": challenge.action.model_dump_json(indent=2),
                            "summary": summary,
                            "draft": draft.model_dump(mode="json") if draft is not None else None,
                            "draft_content_json": after,
                            "baseline_version_id": baseline.version_id if baseline else None,
                            "content_diff": "\n".join(
                                difflib.unified_diff(
                                    before.splitlines(),
                                    after.splitlines(),
                                    fromfile="published version " + baseline.version_id
                                    if baseline
                                    else "first publication",
                                    tofile="prepared draft",
                                    lineterm="",
                                )
                            ),
                            "preview_available": False,
                        },
                    )
                    return
                if handler.path == "/artifact/review":
                    scope = json.loads(body)
                    if set(scope) != {"project_id", "storyline_id", "job_id", "artifact_id"}:
                        raise _HttpError(400, "invalid_arguments")
                    project, storyline, job, artifact = [
                        validate_identifier(scope[key], key)
                        for key in ("project_id", "storyline_id", "job_id", "artifact_id")
                    ]
                    with self.authority.service.connection() as connection:
                        if (
                            connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE name='render_manifests'"
                            ).fetchone()
                            is None
                        ):
                            raise _HttpError(404, "artifact_not_found")
                        manifest = JobStorage(self.authority.service).read_manifest(
                            connection, project, job, artifact, "published"
                        )
                    if manifest.request.storyline_id != storyline:
                        raise _HttpError(404, "artifact_not_found")
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "data": manifest.model_dump(mode="json"),
                            "manifest_sha256": content_sha256(manifest),
                            "manifest_json": manifest.model_dump_json(indent=2),
                            "artifact_url": f"/artifacts/{project}/{storyline}/{job}/{artifact}",
                            "approval_available": False,
                        },
                    )
                    return
                if handler.path == "/action/select":
                    challenge = self.authority.get_challenge(
                        OwnerChallengeInput.model_validate_json(body)
                    )
                    session.selected_challenge, session.selected_digest = (
                        challenge.challenge_id,
                        challenge.action_sha256,
                    )
                    self._json(
                        handler, 200, {"ok": True, "data": challenge.model_dump(mode="json")}
                    )
                    return
                if handler.path == "/action/apply":
                    proof = VerifyOwnerActionInput.model_validate_json(body)
                    if proof.challenge_id != session.selected_challenge:
                        raise _HttpError(403, "action_not_selected")
                    challenge = self.authority.get_challenge(
                        OwnerChallengeInput(
                            application_instance_id=proof.application_instance_id,
                            challenge_id=proof.challenge_id,
                        )
                    )
                    if challenge.action_sha256 != session.selected_digest:
                        raise _HttpError(403, "action_changed")
                    result = self.authority.apply_action(proof)
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "data": result.model_dump(mode="json"),
                            "result_json": result.model_dump_json(indent=2),
                        },
                    )
                    return
                raise _HttpError(404, "route_not_found")
        except _HttpError as exc:
            self._json(handler, exc.status, {"ok": False, "error": {"reason": exc.reason}})
        except ValidationError:
            self._json(handler, 400, {"ok": False, "error": {"reason": "invalid_arguments"}})
        except VideoGenieError as exc:
            status = (
                404
                if exc.code == "NOT_FOUND"
                else 409
                if exc.code in {"REVISION_CONFLICT", "IDEMPOTENCY_CONFLICT"}
                else 403
                if exc.code == "ACCESS_DENIED"
                else 400
            )
            self._json(
                handler,
                status,
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "reason": exc.details.get("reason", "domain_error"),
                        "retry_class": exc.retry_class,
                    },
                },
            )
        except (OSError, ValueError):
            # Never include request data, paths, credentials or OS error text in a response/log.
            self._json(handler, 400, {"ok": False, "error": {"reason": "request_failed"}})

    def _artifact(self, handler: BaseHTTPRequestHandler) -> None:
        parts = handler.path.split("/")
        if len(parts) != 6:
            raise _HttpError(404, "artifact_not_found")
        project, storyline, job, artifact = [
            validate_identifier(value, "artifact_scope") for value in parts[2:]
        ]
        service = self.authority.service
        with service.connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='render_manifests'"
                ).fetchone()
                is None
            ):
                raise _HttpError(404, "artifact_not_found")
            manifest = JobStorage(service).read_manifest(
                connection, project, job, artifact, "published"
            )
            if manifest.request.storyline_id != storyline:
                raise _HttpError(404, "artifact_not_found")
        components = manifest.locator.split("/")
        if components[0] != "artifacts" or len(components) < 2:
            raise _HttpError(403, "artifact_location")
        # Walk relative to owned descriptors, rejecting every symlink component.
        descriptor = os.open(service.data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        snapshot = tempfile.SpooledTemporaryFile[bytes](max_size=1048576)
        try:
            for component in components[:-1]:
                following = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = following
            file_fd = os.open(
                components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
            )
            with os.fdopen(file_fd, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != manifest.byte_size:
                    raise _HttpError(409, "artifact_changed")
                digest = hashlib.sha256()
                remaining = manifest.byte_size
                while remaining:
                    block = source.read(min(65536, remaining))
                    if not block:
                        raise _HttpError(409, "artifact_changed")
                    remaining -= len(block)
                    digest.update(block)
                    snapshot.write(block)
                if source.read(1) or digest.hexdigest() != manifest.artifact_sha256:
                    raise _HttpError(409, "artifact_changed")
                after = os.fstat(source.fileno())
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise _HttpError(409, "artifact_changed")
            snapshot.seek(0)
            self._headers(handler, 200, "video/quicktime", manifest.byte_size)
            while block := snapshot.read(65536):
                handler.wfile.write(block)
        finally:
            os.close(descriptor)
            snapshot.close()
