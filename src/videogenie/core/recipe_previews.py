"""Fictional draft previews with exact live pins; no publication or execution grants."""

import fcntl
import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from videogenie.contracts.models import ContractModel, VideoGenieError
from videogenie.contracts.previews import (
    CompareBrandInput,
    CompareRecipeInput,
    CreatePreviewInput,
    DraftComparison,
    PreviewReadInput,
    PreviewResult,
    content_diff,
)
from videogenie.contracts.recipes import BrandDraft, RecipeDraft, content_sha256
from videogenie.core.recipes import RecipeService
from videogenie.workers.lower_third import render_draft_preview


def _checked[T: ContractModel](model: type[T], value: T) -> T:
    try:
        return model.model_validate(value.model_dump())
    except (ValidationError, AttributeError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "Invalid preview request") from exc


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _mkdir(path: Path) -> None:
    with _directory(path.parent) as parent:
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
        with _directory(path):
            pass


@dataclass(frozen=True)
class PreviewMedia:
    """Verified private spool; usable only inside read_preview's context manager."""

    stream: tempfile.SpooledTemporaryFile[bytes]
    media_type: str
    byte_size: int
    sha256: str


class RecipePreviewService:
    """Store preview evidence separately; current reads revalidate drafts and assets."""

    def __init__(self, recipes: RecipeService) -> None:
        self.recipes = recipes
        self.service = recipes.service
        self.root = self.service.data_dir / "draft-previews"
        with self.service.connection(write=True) as connection:
            present = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            tables = {"draft_preview_schema", "draft_previews"}
            if present & tables:
                if not tables <= present or [
                    row[0] for row in connection.execute("SELECT version FROM draft_preview_schema")
                ] != [1]:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete preview storage")
                columns = [
                    row[1] for row in connection.execute("PRAGMA table_info(draft_previews)")
                ]
                if columns != [
                    "preview_id",
                    "client_id",
                    "idempotency_key",
                    "request_sha256",
                    "stage_name",
                    "result_json",
                ]:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Invalid preview storage")
                guards = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }
                if not {"draft_preview_no_update", "draft_preview_no_delete"} <= guards:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Missing preview history guards")
            else:
                connection.execute("CREATE TABLE draft_preview_schema(version INTEGER NOT NULL)")
                connection.execute("""CREATE TABLE draft_previews(
                    preview_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL REFERENCES studio_clients(client_id),
                    idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                    stage_name TEXT NOT NULL, result_json TEXT NOT NULL,
                    UNIQUE(client_id,idempotency_key))""")
                for operation in ("update", "delete"):
                    connection.execute(f"""CREATE TRIGGER draft_preview_no_{operation}
                        BEFORE {operation.upper()} ON draft_previews
                        BEGIN SELECT RAISE(ABORT,'immutable preview evidence'); END""")
                connection.execute("INSERT INTO draft_preview_schema VALUES(1)")

    def _snapshots(
        self, connection: sqlite3.Connection, request: CreatePreviewInput
    ) -> tuple[RecipeDraft, BrandDraft]:
        if (
            self.recipes.studio.require_client(connection, request.brand.client_id).status
            != "active"
        ):
            raise VideoGenieError("ACCESS_DENIED", "Preview client is archived")
        recipe = self.recipes._draft(connection, "recipe", request.recipe.recipe_id)
        brand = self.recipes._draft(
            connection, "brand", request.brand.brand_id, request.brand.client_id
        )
        assert isinstance(recipe, RecipeDraft) and isinstance(brand, BrandDraft)
        if (recipe.draft_revision, recipe.content_sha256) != (
            request.recipe.draft_revision,
            request.recipe.content_sha256,
        ) or (brand.draft_revision, brand.content_sha256) != (
            request.brand.draft_revision,
            request.brand.content_sha256,
        ):
            raise VideoGenieError("REVISION_CONFLICT", "Preview draft pins are stale")
        self.recipes._verify_assets(brand.content)
        return recipe, brand

    def _row(
        self, connection: sqlite3.Connection, request: PreviewReadInput
    ) -> tuple[PreviewResult, str]:
        row = connection.execute(
            "SELECT * FROM draft_previews WHERE preview_id=? AND client_id=?",
            (request.preview_id, request.client_id),
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Preview is outside the selected client scope")
        try:
            result = PreviewResult.model_validate_json(row["result_json"])
        except ValidationError as exc:
            raise VideoGenieError("INTERNAL_ERROR", "Stored preview evidence is invalid") from exc
        manifest = result.manifest
        name = row["stage_name"]
        if (
            (manifest.preview_id, manifest.client_id, manifest.request_sha256)
            != (request.preview_id, request.client_id, row["request_sha256"])
            or not isinstance(name, str)
            or not name.startswith("videogenie-render-")
            or (Path(name).name != name or "/" in name or "\\" in name)
        ):
            raise VideoGenieError("INTERNAL_ERROR", "Stored preview index is inconsistent")
        recipe, brand = self._snapshots(connection, manifest.provenance.request)
        if (recipe, brand) != (manifest.provenance.recipe, manifest.provenance.brand):
            raise VideoGenieError("REVISION_CONFLICT", "Preview snapshots changed")
        return result, name

    def get_preview(self, client_id: str, preview_id: str) -> PreviewResult:
        """Read current metadata; media bytes are independently verified by read_preview."""
        try:
            request = PreviewReadInput(client_id=client_id, preview_id=preview_id, kind="poster")
        except ValidationError as exc:
            raise VideoGenieError("INVALID_ARGUMENT", "Invalid preview identity") from exc
        with self.service.connection() as connection:
            return self._row(connection, request)[0]

    def create_preview(self, request: CreatePreviewInput) -> PreviewResult:
        request = _checked(CreatePreviewInput, request)
        try:
            _mkdir(self.root)
            _mkdir(self.root / "locks")
            lock_name = hashlib.sha256(
                (request.brand.client_id + "\0" + request.idempotency_key).encode()
            ).hexdigest()
            with _directory(self.root / "locks") as locks:
                descriptor = os.open(
                    lock_name,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=locks,
                )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise VideoGenieError("ACCESS_DENIED", "Invalid preview lock")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise VideoGenieError(
                        "LEASE_HELD", "This preview is already rendering"
                    ) from exc
                with self.service.connection() as connection:
                    existing = connection.execute(
                        "SELECT preview_id,request_sha256 FROM draft_previews "
                        "WHERE client_id=? AND idempotency_key=?",
                        (request.brand.client_id, request.idempotency_key),
                    ).fetchone()
                    if existing and existing["request_sha256"] != content_sha256(request):
                        raise VideoGenieError(
                            "IDEMPOTENCY_CONFLICT", "Preview key binds another request"
                        )
                    recipe, brand = self._snapshots(connection, request)
                if existing:
                    # Replay remains current evidence only if all persisted media still verify.
                    for kind, index in [
                        ("video", None),
                        *(("keyframe", i) for i in (0, 16, 75, 129, 149)),
                    ]:
                        read = PreviewReadInput.model_validate(
                            dict(
                                client_id=request.brand.client_id,
                                preview_id=existing["preview_id"],
                                kind=kind,
                                frame_index=index,
                            )
                        )
                        with self.read_preview(read):
                            pass
                    return self.get_preview(request.brand.client_id, existing["preview_id"])
                preview_id = str(uuid4())
                parent = self.root / preview_id
                with _directory(self.root) as directory:
                    os.mkdir(parent.name, mode=0o700, dir_fd=directory)
                    os.fsync(directory)
                try:
                    paths = {
                        pin: self.recipes.assets.resolve_asset(pin)
                        for pin in {brand.content.name_font, brand.content.role_font}
                    }
                    rendered = render_draft_preview(
                        preview_id, request, recipe, brand, paths, parent
                    )
                    with _directory(parent) as stage_parent:
                        os.fsync(stage_parent)
                    result = PreviewResult(
                        manifest=rendered.manifest,
                        manifest_sha256=content_sha256(rendered.manifest),
                    )
                    with self.service.connection(write=True) as connection:
                        if self._snapshots(connection, request) != (recipe, brand):
                            raise VideoGenieError(
                                "REVISION_CONFLICT", "Draft changed during preview"
                            )
                        connection.execute(
                            "INSERT INTO draft_previews VALUES(?,?,?,?,?,?)",
                            (
                                preview_id,
                                request.brand.client_id,
                                request.idempotency_key,
                                content_sha256(request),
                                rendered.path.parent.name,
                                result.model_dump_json(),
                            ),
                        )
                    return result
                except BaseException as primary:
                    # A transaction error is not proof of absent commit. Only
                    # remove our unique parent after a successful absence read.
                    uncertain_cleanup = isinstance(primary, VideoGenieError) and bool(
                        primary.details.keys()
                        & {
                            "cleanup",
                            "stage_cleanup_errno",
                            "group_signal_errno",
                            "child_signal_errno",
                            "group_cleanup",
                            "child_reap",
                            "child_wait_errno",
                            "pipe_close_errno",
                        }
                    )
                    if not uncertain_cleanup:
                        try:
                            with self.service.connection() as connection:
                                registered = (
                                    connection.execute(
                                        "SELECT 1 FROM draft_previews WHERE preview_id=?",
                                        (preview_id,),
                                    ).fetchone()
                                    is not None
                                )
                            if not registered:
                                with _directory(self.root) as directory:
                                    shutil.rmtree(parent.name, dir_fd=directory)
                                    os.fsync(directory)
                        except (OSError, VideoGenieError):
                            primary.add_note("Owned preview cleanup remains unconfirmed")
                            if isinstance(primary, VideoGenieError):
                                primary.details["preview_cleanup"] = "unconfirmed"
                    raise
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "Managed preview storage is unavailable"
            ) from exc

    @contextmanager
    def read_preview(self, request: PreviewReadInput) -> Iterator[PreviewMedia]:
        """Copy exact no-follow bytes to a verified spool before exposing any media."""
        request = _checked(PreviewReadInput, request)
        with self.service.connection() as connection:
            result, stage = self._row(connection, request)
        manifest = result.manifest
        if request.kind == "video":
            name, size, digest, media = (
                "candidate.mov",
                manifest.video_byte_size,
                manifest.video_sha256,
                "video/quicktime",
            )
        else:
            index = 75 if request.kind == "poster" else request.frame_index
            sample = next(item for item in manifest.keyframes if item.frame_index == index)
            name, size, digest, media = (
                f"frame-{index}.png",
                sample.png_byte_size,
                sample.png_sha256,
                "image/png",
            )
        try:
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as spool:
                with _directory(self.root / request.preview_id / stage) as directory:
                    descriptor = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                    )
                    with os.fdopen(descriptor, "rb") as source:
                        before = os.fstat(source.fileno())
                        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                            raise VideoGenieError("INPUT_CHANGED", "Preview file size/type changed")
                        observed = hashlib.sha256()
                        remaining = size
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise VideoGenieError("INPUT_CHANGED", "Preview file was truncated")
                            observed.update(chunk)
                            spool.write(chunk)
                            remaining -= len(chunk)
                        after = os.fstat(source.fileno())
                        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                            after.st_size,
                            after.st_mtime_ns,
                            after.st_ctime_ns,
                        ) or observed.hexdigest() != digest:
                            raise VideoGenieError("INPUT_CHANGED", "Preview bytes changed")
                with self.service.connection() as connection:
                    self._row(connection, request)
                spool.seek(0)
                yield PreviewMedia(spool, media, size, digest)
        except OSError as exc:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Managed preview bytes unavailable") from exc

    def compare_recipe(self, request: CompareRecipeInput) -> DraftComparison:
        return self._compare(_checked(CompareRecipeInput, request))

    def compare_brand(self, request: CompareBrandInput) -> DraftComparison:
        return self._compare(_checked(CompareBrandInput, request))

    def _compare(self, request: CompareRecipeInput | CompareBrandInput) -> DraftComparison:
        brand_kind = isinstance(request, CompareBrandInput)
        kind: Literal["recipe", "brand"] = "brand" if brand_kind else "recipe"
        identity = (
            request.draft.brand_id
            if isinstance(request, CompareBrandInput)
            else request.draft.recipe_id
        )
        client = request.draft.client_id if isinstance(request, CompareBrandInput) else None
        with self.service.connection() as connection:
            if (
                client is not None
                and self.recipes.studio.require_client(connection, client).status != "active"
            ):
                raise VideoGenieError("ACCESS_DENIED", "Comparison client is archived")
            current = self.recipes._draft(connection, kind, identity, client)
            if (current.draft_revision, current.content_sha256) != (
                request.draft.draft_revision,
                request.draft.content_sha256,
            ):
                raise VideoGenieError("REVISION_CONFLICT", "Comparison draft pin is stale")
            prior = (
                None
                if request.prior_version_id is None
                else self.recipes._version(
                    connection, kind, identity, request.prior_version_id, client
                )
            )
            history = (
                connection.execute(
                    "SELECT 1 FROM content_versions WHERE kind=? AND identity_id=? "
                    "AND client_id IS ? LIMIT 1",
                    (kind, identity, client),
                ).fetchone()
                is not None
            )
        return DraftComparison(
            request=request,
            current=current,
            prior=prior,
            has_published_history=history,
            baseline_kind="published_version"
            if prior
            else "explicit_empty"
            if history
            else "no_history",
            diff=content_diff(current, prior),
        )
