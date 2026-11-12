"""Bounded loopback HTTP transport for the explicitly launched trusted owner console.

No HTTP route mints bootstrap/recovery admission. Same-account arbitrary code is
outside this boundary; a browser session never replaces per-action WebAuthn proof.
"""

import base64
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
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from pydantic import ValidationError
from webauthn.helpers import bytes_to_base64url

from videogenie.adapters.owner_auth import EnrollmentTicket
from videogenie.contracts.authority import (
    IssueGrantAction,
    OutputReviewTarget,
    OwnerAction,
    OwnerActionRequest,
    OwnerChallenge,
    OwnerChallengeInput,
    ProjectAuthorityScope,
    PublishBrandAction,
    PublishRecipeAction,
    RegisterOwnerInput,
    ReviewOutputAction,
    SelectOutputInput,
    VerifyOwnerActionInput,
)
from videogenie.contracts.costs import CostReport, Currency
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.previews import (
    BrandDraftPin,
    CompareBrandInput,
    CompareRecipeInput,
    CreatePreviewInput,
    PreviewReadInput,
    RecipeDraftPin,
)
from videogenie.contracts.recipes import (
    BrandDraft,
    BrandDraftInput,
    BrandScopeInput,
    RecipeDraft,
    RecipeDraftInput,
    content_sha256,
)
from videogenie.contracts.rendering import ArtifactInput, ListArtifactsInput
from videogenie.core.authority import OwnerAuthorityService
from videogenie.core.job_storage import JobStorage
from videogenie.core.recipe_previews import RecipePreviewService
from videogenie.core.rendering import RenderService
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
    reviewed_publications: set[str] = field(default_factory=set)
    preview: tuple[str, str] | None = None
    output_target: OutputReviewTarget | None = None
    applied_responses: dict[str, dict[str, object]] = field(default_factory=dict)


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
        self.previews = RecipePreviewService(authority.recipes)
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
                if handler.command == "GET" and handler.path.startswith("/previews/"):
                    parts = handler.path.split("/")
                    if len(parts) not in (5, 6):
                        raise _HttpError(404, "preview_not_found")
                    read = PreviewReadInput.model_validate(
                        {
                            "client_id": parts[2],
                            "preview_id": parts[3],
                            "kind": parts[4],
                            "frame_index": int(parts[5]) if len(parts) == 6 else None,
                        }
                    )
                    with self.previews.read_preview(read) as media:
                        self._headers(handler, 200, media.media_type, media.byte_size)
                        while block := media.stream.read(65536):
                            handler.wfile.write(block)
                    return
                if handler.command == "GET" and handler.path.startswith("/output-media/"):
                    parts = handler.path.split("/")
                    if len(parts) != 7:
                        raise _HttpError(404, "output_media_not_found")
                    scope = ArtifactInput(
                        project_id=parts[2],
                        storyline_id=parts[3],
                        client_id=parts[4],
                        artifact_id=parts[5],
                    )
                    frame = None if parts[6] == "video" else int(parts[6])
                    with RenderService(self.authority).read_output_preview(
                        scope, frame_index=frame
                    ) as media:
                        self._headers(handler, 200, media.media_type, media.byte_size)
                        while block := media.stream.read(65536):
                            handler.wfile.write(block)
                    return
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
                    comparison = None
                    if isinstance(draft, RecipeDraft):
                        comparison = self.previews.compare_recipe(
                            CompareRecipeInput(
                                draft=RecipeDraftPin(
                                    recipe_id=draft.recipe_id,
                                    draft_revision=draft.draft_revision,
                                    content_sha256=draft.content_sha256,
                                ),
                                prior_version_id=None,
                            )
                        )
                    elif isinstance(draft, BrandDraft):
                        comparison = self.previews.compare_brand(
                            CompareBrandInput(
                                draft=BrandDraftPin(
                                    brand_id=draft.brand_id,
                                    client_id=draft.client_id,
                                    draft_revision=draft.draft_revision,
                                    content_sha256=draft.content_sha256,
                                ),
                                prior_version_id=None,
                            )
                        )
                    if draft is not None:
                        if (
                            len(session.reviewed_publications) >= 128
                            and challenge.challenge_id not in session.reviewed_publications
                        ):
                            raise _HttpError(409, "review_capacity")
                        session.reviewed_publications.add(challenge.challenge_id)
                        session.preview = None
                    summary = []
                    if isinstance(action, IssueGrantAction):
                        terms = action.terms
                        summary = [
                            {"label": label, "value": str(value)}
                            for label, value in (
                                ("Project", self._project_label(terms.project_id)),
                                (
                                    "Storyline",
                                    self._storyline_label(terms.project_id, terms.storyline_id),
                                ),
                                ("Client", self._client_label(terms.client_id)),
                                ("Maximum runs", terms.max_runs),
                                ("Attempts per run", terms.max_attempts_per_run),
                                ("Concurrent runs", terms.max_concurrent),
                                (
                                    "Attempt timeout (seconds)",
                                    terms.resource_bounds.timeout_seconds,
                                ),
                                (
                                    "Maximum artifact bytes",
                                    terms.resource_bounds.max_artifact_bytes,
                                ),
                                (
                                    "Name / role character limits",
                                    str(terms.parameter_bounds.name_max_length)
                                    + " / "
                                    + str(terms.parameter_bounds.role_max_length),
                                ),
                                ("Permitted inputs", ", ".join(terms.permitted_input_classes)),
                                ("Valid from", terms.valid_from),
                                ("Expires", terms.expires_at),
                                (
                                    "Recipe",
                                    "Lower third · "
                                    + terms.recipe.recipe_id[:8]
                                    + " / version "
                                    + terms.recipe.version_id[:8],
                                ),
                                (
                                    "Brand kit",
                                    self._client_label(terms.client_id)
                                    + " · "
                                    + terms.brand.brand_id[:8]
                                    + " / version "
                                    + terms.brand.version_id[:8],
                                ),
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
                            "draft_revision_text": str(draft.draft_revision) if draft else None,
                            "baseline_version_id": None,
                            "baseline_kind": comparison.baseline_kind if comparison else None,
                            "content_diff": comparison.diff
                            if comparison and comparison.has_published_history
                            else "",
                            "preview_available": False,
                        },
                    )
                    return
                if handler.path == "/catalog":
                    if json.loads(body) != {}:
                        raise _HttpError(400, "unexpected_fields")
                    recipes = self.authority.recipes
                    clients = recipes.studio.list_clients(include_archived=True)
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "clients": [item.model_dump(mode="json") for item in clients],
                            "projects": [
                                item.model_dump(mode="json")
                                for item in self.authority.service.list_projects(
                                    include_archived=True
                                )
                            ],
                            "recipes": [
                                item.model_dump(mode="json")
                                | {
                                    "draft_revision_text": str(item.draft_revision),
                                    "pin_json": RecipeDraftPin(
                                        recipe_id=item.recipe_id,
                                        draft_revision=item.draft_revision,
                                        content_sha256=item.content_sha256,
                                    ).model_dump_json(),
                                }
                                for item in recipes.list_recipe_drafts()
                            ],
                            "brands": [
                                item.model_dump(mode="json")
                                | {
                                    "draft_revision_text": str(item.draft_revision),
                                    "pin_json": BrandDraftPin(
                                        client_id=item.client_id,
                                        brand_id=item.brand_id,
                                        draft_revision=item.draft_revision,
                                        content_sha256=item.content_sha256,
                                    ).model_dump_json(),
                                }
                                for client in clients
                                for item in recipes.list_brand_drafts(
                                    BrandScopeInput(client_id=client.client_id)
                                )
                            ],
                        },
                    )
                    return
                if handler.path == "/output/review":
                    scope = ArtifactInput.model_validate_json(body)
                    session.output_target = None
                    renderer = RenderService(self.authority)
                    view = renderer.prepare_output_review(scope)
                    selection = renderer.get_selection(
                        ProjectAuthorityScope(
                            project_id=scope.project_id,
                            storyline_id=scope.storyline_id,
                            client_id=scope.client_id,
                        )
                    )
                    session.output_target = view.target
                    base = (
                        f"/output-media/{scope.project_id}/{scope.storyline_id}/"
                        f"{scope.client_id}/{scope.artifact_id}"
                    )
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "target": view.target.model_dump(mode="json"),
                            "target_json": view.target.model_dump_json(),
                            "evidence": view.evidence.model_dump(mode="json"),
                            "evidence_json": view.evidence.model_dump_json(indent=2),
                            "costs_json": view.costs.model_dump_json(indent=2),
                            "cost_summary": self._cost_summary(view.costs),
                            "summary": [
                                {
                                    "label": "Project",
                                    "value": self._project_label(scope.project_id),
                                },
                                {
                                    "label": "Storyline",
                                    "value": self._storyline_label(
                                        scope.project_id, scope.storyline_id
                                    ),
                                },
                                {"label": "Client", "value": self._client_label(scope.client_id)},
                                {
                                    "label": "Project revision",
                                    "value": str(view.target.project_revision_at_review),
                                },
                                {
                                    "label": "Configuration revision",
                                    "value": str(view.target.config_revision),
                                },
                            ],
                            "selection_revision_text": str(selection.revision if selection else 0),
                            "selection_json": selection.review.model_dump_json(indent=2)
                            if selection
                            else None,
                            "media_base": base,
                        },
                    )
                    return
                if handler.path == "/output/decision":
                    decision_request = OwnerActionRequest.model_validate_json(body)
                    if (
                        not isinstance(decision_request.action, ReviewOutputAction)
                        or decision_request.action.target != session.output_target
                    ):
                        raise _HttpError(409, "output_context_changed")
                    challenge = self.authority.request_action(decision_request)
                    session.selected_challenge, session.selected_digest = (
                        challenge.challenge_id,
                        challenge.action_sha256,
                    )
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "data": challenge.model_dump(mode="json"),
                            "action_json": challenge.action.model_dump_json(indent=2),
                        },
                    )
                    return
                if handler.path == "/output/select":
                    selection_request = SelectOutputInput.model_validate_json(body)
                    selection = RenderService(self.authority).select_output(selection_request)
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "revision_text": str(selection.revision),
                            "review": selection.review.model_dump(mode="json"),
                            "review_json": selection.review.model_dump_json(indent=2),
                        },
                    )
                    return
                if handler.path == "/outputs":
                    list_scope = ListArtifactsInput.model_validate_json(body)
                    artifacts = RenderService(self.authority).list_artifacts(list_scope)
                    self._json(
                        handler,
                        200,
                        {"ok": True, "data": [item.model_dump(mode="json") for item in artifacts]},
                    )
                    return
                if handler.path in ("/comparison/recipe", "/comparison/brand"):
                    comparison = (
                        self.previews.compare_recipe(CompareRecipeInput.model_validate_json(body))
                        if handler.path.endswith("recipe")
                        else self.previews.compare_brand(
                            CompareBrandInput.model_validate_json(body)
                        )
                    )
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "baseline_kind": comparison.baseline_kind,
                            "diff": comparison.diff
                            if comparison.baseline_kind != "no_history"
                            else "",
                            "current_json": comparison.current.content.model_dump_json(indent=2),
                            "prior_json": comparison.prior.content.model_dump_json(indent=2)
                            if comparison.prior
                            else None,
                        },
                    )
                    return
                if handler.path == "/preview/create":
                    preview_request = CreatePreviewInput.model_validate_json(body)
                    challenge = self._selected_publication(session)
                    self._match_preview(challenge.action, preview_request)
                    preview_result = self.previews.create_preview(preview_request)
                    session.preview = (
                        preview_result.manifest.client_id,
                        preview_result.manifest.preview_id,
                    )
                    self._json(
                        handler,
                        200,
                        {
                            "ok": True,
                            "data": preview_result.model_dump(mode="json"),
                            "manifest_json": preview_result.manifest.model_dump_json(indent=2),
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
                    proof_hash = hashlib.sha256(proof.model_dump_json().encode()).hexdigest()
                    if proof_hash in session.applied_responses:
                        self._json(handler, 200, session.applied_responses[proof_hash])
                        return
                    if proof.challenge_id in session.reviewed_publications:
                        if session.preview is None:
                            raise _HttpError(409, "publication_preview_required")
                        client, preview_id = session.preview
                        result_preview = self.previews.get_preview(client, preview_id)
                        self._match_preview(
                            challenge.action, result_preview.manifest.provenance.request
                        )
                        # Revalidate all supplied media, not only the metadata row.
                        for read in (
                            PreviewReadInput(client_id=client, preview_id=preview_id, kind="video"),
                            *(
                                PreviewReadInput(
                                    client_id=client,
                                    preview_id=preview_id,
                                    kind="keyframe",
                                    frame_index=index,
                                )
                                for index in (0, 16, 75, 129, 149)
                            ),
                        ):
                            with self.previews.read_preview(read):
                                pass
                    if len(session.applied_responses) >= 128:
                        raise _HttpError(409, "review_capacity")
                    result = self.authority.apply_action(proof)
                    session.applied_responses[proof_hash] = {
                        "ok": True,
                        "data": result.model_dump(mode="json"),
                        "result_json": result.model_dump_json(indent=2),
                    }
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

    def _selected_publication(self, session: _Session) -> OwnerChallenge:
        if (
            session.selected_challenge is None
            or session.selected_challenge not in session.reviewed_publications
        ):
            raise _HttpError(409, "publication_review_required")
        return self.authority.get_challenge(
            OwnerChallengeInput(
                application_instance_id=self.authority.auth.application_instance_id,
                challenge_id=session.selected_challenge,
            )
        )

    @staticmethod
    def _match_preview(action: OwnerAction, request: CreatePreviewInput) -> None:
        if isinstance(action, PublishRecipeAction):
            pin = request.recipe
            matches = (pin.recipe_id, pin.draft_revision, pin.content_sha256) == (
                action.recipe_id,
                action.expected_draft_revision,
                action.content_sha256,
            )
        elif isinstance(action, PublishBrandAction):
            pin_brand = request.brand
            matches = (
                pin_brand.client_id,
                pin_brand.brand_id,
                pin_brand.draft_revision,
                pin_brand.content_sha256,
            ) == (
                action.client_id,
                action.brand_id,
                action.expected_draft_revision,
                action.content_sha256,
            )
        else:
            matches = False
        if not matches:
            raise _HttpError(409, "preview_action_mismatch")

    @staticmethod
    def _cost_summary(report: CostReport) -> list[dict[str, str]]:
        """Keep exact integer money and explicit unknown/unpriced categories for display."""
        rows = []
        for total in report.currencies:
            currency = total.currency

            def money(value: int, currency: Currency = currency) -> str:
                digits = str(abs(value)).zfill(currency.exponent + 1)
                amount = (
                    digits[: -currency.exponent] + "." + digits[-currency.exponent :]
                    if currency.exponent
                    else digits
                )
                return ("-" if value < 0 else "") + amount + " " + currency.code

            for label, value in (
                ("Provider actual", total.actual_provider_minor),
                ("Adjustments", total.adjustments_minor),
                ("Net actual", total.net_actual_minor),
                ("Reserved exposure", total.reserved_exposure_minor),
                ("Unreserved forecast", total.unreserved_forecast_minor),
            ):
                rows.append({"label": label, "value": money(value)})
            for label, optional in (
                ("Client charge", total.client_charge),
                ("Internal cost", total.internal_cost),
                ("Total economic cost", total.total_economic_cost),
            ):
                rows.append(
                    {
                        "label": label,
                        "value": "Not configured / unknown"
                        if optional is None
                        else money(optional.minor_units),
                    }
                )
        for label, quantities in (
            ("Unpriced measured quantity", report.unpriced_quantities),
            ("Unknown quantity", report.unknown_quantities),
        ):
            for q in quantities:
                if q.unit == "worker_wall_seconds":
                    seconds = Decimal(q.numerator) / Decimal(q.denominator)
                    rows.append(
                        {
                            "label": "Render time",
                            "value": f"{seconds:.2f} s (no price set)"
                            if label.startswith("Unpriced")
                            else f"{seconds:.2f} s (outcome unknown)",
                        }
                    )
                else:
                    rows.append(
                        {"label": label, "value": f"{q.numerator}/{q.denominator} {q.unit}"}
                    )
        for note in report.coverage_notes:
            rows.append({"label": "Coverage", "value": note})
        return rows

    def _project_label(self, identity: str) -> str:
        try:
            return self.authority.service.get_project(identity).name + " · " + identity[:8]
        except VideoGenieError:
            return "Unavailable project · " + identity[:8]

    def _storyline_label(self, project: str, identity: str) -> str:
        try:
            value = next(
                item
                for item in self.authority.service.get_project(project).storylines
                if item.storyline_id == identity
            )
            return value.name + " · " + identity[:8]
        except (VideoGenieError, StopIteration):
            return "Unavailable storyline · " + identity[:8]

    def _client_label(self, identity: str) -> str:
        try:
            return self.authority.recipes.studio.get_client(identity).name + " · " + identity[:8]
        except VideoGenieError:
            return "Unavailable client · " + identity[:8]

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
        if not (
            (components[0] == "artifacts" and len(components) >= 2)
            or (components[:2] == ["render-execution", "artifacts"] and len(components) == 3)
        ):
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
