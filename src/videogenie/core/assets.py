"""Immutable local assets with source-preserving references and bounded managed copies."""

import hashlib
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from pydantic import ValidationError

from videogenie.contracts.assets import (
    AssetLookupInput,
    AssetPin,
    AssetVersion,
    RegisterAssetInput,
)
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.recipes import content_sha256
from videogenie.core.service import VideoGenieService
from videogenie.workers.probe import Fingerprint, require_unchanged

_TABLES = {"asset_schema", "asset_versions", "asset_requests"}
_MAX_BYTES = 268435456
_HASH_SECONDS = 30.0


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _fingerprint(
    source: BinaryIO, max_bytes: int, destination: BinaryIO | None = None
) -> Fingerprint:
    """One bounded pass detects mutations and optionally writes a private staging file."""
    try:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= max_bytes:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "Asset must be a nonempty regular file within max_bytes"
            )
        deadline = time.monotonic() + _HASH_SECONDS
        digest = hashlib.sha256()
        total = 0
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes or time.monotonic() > deadline:
                raise VideoGenieError(
                    "MEDIA_UNAVAILABLE", "Asset exceeds its byte or hashing-time bound"
                )
            digest.update(chunk)
            if destination is not None:
                destination.write(chunk)
        after = os.fstat(source.fileno())
        if _identity(before) != _identity(after) or total != after.st_size:
            raise VideoGenieError("INPUT_CHANGED", "Asset changed during fingerprinting")
        return Fingerprint(
            sha256=digest.hexdigest(),
            size=total,
            device=after.st_dev,
            inode=after.st_ino,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
        )
    except OSError as exc:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Asset bytes cannot be read or staged") from exc


class AssetService:
    """Data-root-local registry, without implicit project state or tenant-isolation claims."""

    def __init__(self, service: VideoGenieService) -> None:
        self.service = service
        self._initialize()

    def _initialize(self) -> None:
        with self.service.connection(write=True) as connection:
            present = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            } & _TABLES
            if present:
                if present != _TABLES or [
                    row[0] for row in connection.execute("SELECT version FROM asset_schema")
                ] != [1]:
                    raise VideoGenieError(
                        "UNSUPPORTED_SCHEMA", "Unsupported or incomplete asset schema"
                    )
                return
            connection.execute("CREATE TABLE asset_schema (version INTEGER NOT NULL)")
            connection.execute("""CREATE TABLE asset_versions (
                version_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, record_json TEXT NOT NULL,
                storage_mode TEXT NOT NULL CHECK(storage_mode IN ('reference','copy')),
                location TEXT NOT NULL
            )""")
            connection.execute(
                "CREATE INDEX assets_identity ON asset_versions(asset_id,version_id)"
            )
            connection.execute("""CREATE TABLE asset_requests (
                idempotency_key TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                version_id TEXT NOT NULL REFERENCES asset_versions(version_id)
            )""")
            connection.execute("INSERT INTO asset_schema VALUES (1)")

    @contextmanager
    def _blobs(self, *, create: bool = False) -> Iterator[int]:
        """Anchor managed writes to no-follow directory handles, not caller-controlled paths."""
        root_fd = asset_fd = blob_fd = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_fd = os.open(self.service.data_dir, flags)
            if create:
                try:
                    os.mkdir("assets", mode=0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
            asset_fd = os.open("assets", flags, dir_fd=root_fd)
            if create:
                try:
                    os.mkdir("blobs", mode=0o700, dir_fd=asset_fd)
                except FileExistsError:
                    pass
            blob_fd = os.open("blobs", flags, dir_fd=asset_fd)
            if create:
                os.fsync(asset_fd)
                os.fsync(root_fd)
            yield blob_fd
        except OSError as exc:
            raise VideoGenieError(
                "ACCESS_DENIED", "Managed asset storage is unavailable or unsafe"
            ) from exc
        finally:
            for descriptor in (blob_fd, asset_fd, root_fd):
                if descriptor is not None:
                    os.close(descriptor)

    def _check_blob_anchor(self, descriptor: int) -> None:
        path = self.service.data_dir / "assets" / "blobs"
        if (self.service.data_dir / "assets").is_symlink() or path.is_symlink():
            raise VideoGenieError("ACCESS_DENIED", "Managed asset directory is a symlink")
        try:
            if _identity(path.stat())[:2] != _identity(os.fstat(descriptor))[:2]:
                raise VideoGenieError("INPUT_CHANGED", "Managed asset directory changed")
        except OSError as exc:
            raise VideoGenieError("INPUT_CHANGED", "Managed asset directory disappeared") from exc

    @staticmethod
    def _open_source(path: Path) -> BinaryIO:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            return os.fdopen(descriptor, "rb")
        except OSError as exc:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Asset source cannot be opened") from exc

    @staticmethod
    def _read(connection: sqlite3.Connection, lookup: AssetLookupInput) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM asset_versions WHERE asset_id=? AND version_id=?",
            (lookup.asset_id, lookup.version_id),
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Asset version not found in this data root")
        return row

    @staticmethod
    def _record(row: sqlite3.Row) -> AssetVersion:
        try:
            record = AssetVersion.model_validate_json(row["record_json"])
            if (record.asset_id, record.version_id) != (row["asset_id"], row["version_id"]):
                raise ValueError("Stored identity mismatch")
            return record
        except (ValidationError, ValueError) as exc:
            raise VideoGenieError("INTERNAL_ERROR", "Stored asset record is invalid") from exc

    @staticmethod
    def _lookup(asset_id: str, version_id: str) -> AssetLookupInput:
        try:
            return AssetLookupInput(asset_id=asset_id, version_id=version_id)
        except ValidationError as exc:
            raise VideoGenieError(
                "INVALID_ARGUMENT", "Asset and version IDs must be UUIDs"
            ) from exc

    def get_asset(self, asset_id: str, version_id: str) -> AssetVersion:
        """Read immutable metadata, even if external source bytes later become unavailable."""
        lookup = self._lookup(asset_id, version_id)
        with self.service.connection() as connection:
            return self._record(self._read(connection, lookup))

    def _prior(
        self, key: str, request_digest: str, input_digest: str | None
    ) -> AssetVersion | None:
        with self.service.connection() as connection:
            return self._prior_in(connection, key, request_digest, input_digest)

    def _prior_in(
        self,
        connection: sqlite3.Connection,
        key: str,
        request_digest: str,
        input_digest: str | None,
    ) -> AssetVersion | None:
        row = connection.execute(
            "SELECT * FROM asset_requests WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest or (
            input_digest is not None and row["input_sha256"] != input_digest
        ):
            raise VideoGenieError(
                "IDEMPOTENCY_CONFLICT", "Registration key was used with different inputs"
            )
        record = connection.execute(
            "SELECT * FROM asset_versions WHERE version_id=?", (row["version_id"],)
        ).fetchone()
        assert record is not None
        return self._record(record)

    def register_asset(self, request: RegisterAssetInput) -> AssetVersion:
        """Register exact bytes; same key/request/content replays, changed content conflicts.

        Completed copy replay still checks the supplied source. Read an existing version
        by its IDs if that source is gone. A new key creates an independent version.
        """
        try:
            request = RegisterAssetInput.model_validate(request.model_dump())
        except (ValidationError, AttributeError) as exc:
            raise VideoGenieError("INVALID_ARGUMENT", "Invalid asset registration request") from exc
        request_digest = content_sha256(request)
        self._prior(request.idempotency_key, request_digest, None)
        try:
            source = Path(request.source_path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Asset source cannot be resolved") from exc
        # Registry-managed files are consumed by exact pins, never reimported as mutable sources.
        if source.is_relative_to(self.service.data_dir):
            raise VideoGenieError(
                "INVALID_ARGUMENT", "Registration source must be outside the data root"
            )
        if request.asset_id is not None:
            with self.service.connection() as connection:
                if not connection.execute(
                    "SELECT 1 FROM asset_versions WHERE asset_id=?", (request.asset_id,)
                ).fetchone():
                    raise VideoGenieError(
                        "NOT_FOUND", "Asset identity does not exist in this data root"
                    )
        with self._open_source(source) as stream:
            if request.storage_mode == "reference":
                identity = _fingerprint(stream, request.max_bytes)
                require_unchanged(source, identity)
                return self._commit(request, request_digest, identity, str(source), None)
            with self._blobs(create=True) as descriptor:
                stage = f".stage-{uuid4()}"
                try:
                    stage_fd = os.open(
                        stage,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=descriptor,
                    )
                    with os.fdopen(stage_fd, "wb") as target:
                        identity = _fingerprint(stream, request.max_bytes, target)
                        target.flush()
                        os.fsync(target.fileno())
                    require_unchanged(source, identity)
                    self._prior(request.idempotency_key, request_digest, identity.sha256)
                    # link is atomic/no-replace. Existing or orphaned committed blobs are
                    # reused only after a full hash check; private unfinished stages are ignored.
                    try:
                        os.link(
                            stage,
                            identity.sha256,
                            src_dir_fd=descriptor,
                            dst_dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                    except FileExistsError:
                        existing = self._blob_identity(descriptor, identity.sha256)
                        if (existing.sha256, existing.size) != (identity.sha256, identity.size):
                            raise VideoGenieError(
                                "INPUT_CHANGED", "Existing managed blob differs from its digest"
                            ) from None
                    os.unlink(stage, dir_fd=descriptor)
                    os.fsync(descriptor)
                    self._check_blob_anchor(descriptor)
                    blob_identity = self._blob_identity(descriptor, identity.sha256)
                    if (blob_identity.sha256, blob_identity.size) != (
                        identity.sha256,
                        identity.size,
                    ):
                        raise VideoGenieError(
                            "INPUT_CHANGED", "Managed copy differs from its source"
                        )
                    return self._commit(
                        request,
                        request_digest,
                        identity,
                        identity.sha256,
                        descriptor,
                        blob_identity,
                    )
                finally:
                    try:
                        os.unlink(stage, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass

    @staticmethod
    def _blob_identity(descriptor: int, name: str) -> Fingerprint:
        # A concurrent publisher may unlink its staging hardlink during this read,
        # changing ctime without changing bytes. Retry once, always hashing again.
        try:
            return AssetService._read_blob_identity(descriptor, name)
        except VideoGenieError as exc:
            if exc.code != "INPUT_CHANGED":
                raise
        return AssetService._read_blob_identity(descriptor, name)

    @staticmethod
    def _read_blob_identity(descriptor: int, name: str) -> Fingerprint:
        try:
            blob_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(blob_fd, "rb") as stream:
                identity = _fingerprint(stream, _MAX_BYTES)
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if _identity(info) != (
                identity.device,
                identity.inode,
                identity.size,
                identity.mtime_ns,
                identity.ctime_ns,
            ):
                raise VideoGenieError("INPUT_CHANGED", "Managed blob changed during verification")
            return identity
        except OSError as exc:
            raise VideoGenieError(
                "MEDIA_UNAVAILABLE", "Managed asset bytes are unavailable or unsafe"
            ) from exc

    def _commit(
        self,
        request: RegisterAssetInput,
        request_digest: str,
        identity: Fingerprint,
        location: str,
        descriptor: int | None,
        blob_identity: Fingerprint | None = None,
    ) -> AssetVersion:
        record = AssetVersion(
            asset_id=request.asset_id or str(uuid4()),
            version_id=str(uuid4()),
            sha256=identity.sha256,
            byte_size=identity.size,
            media_type=request.media_type,
            origin=request.origin,
            license_provenance=request.license_provenance,
        )
        with self.service.connection(write=True) as connection:
            prior = self._prior_in(
                connection, request.idempotency_key, request_digest, identity.sha256
            )
            if prior is not None:
                return prior
            try:
                source = Path(request.source_path).resolve(strict=True)
                require_unchanged(source, identity)
            except (OSError, RuntimeError) as exc:
                raise VideoGenieError(
                    "INPUT_CHANGED", "Asset source disappeared before commitment"
                ) from exc
            if descriptor is not None:
                self._check_blob_anchor(descriptor)
                blob = os.stat(location, dir_fd=descriptor, follow_symlinks=False)
                assert blob_identity is not None
                if not stat.S_ISREG(blob.st_mode) or _identity(blob) != (
                    blob_identity.device,
                    blob_identity.inode,
                    blob_identity.size,
                    blob_identity.mtime_ns,
                    blob_identity.ctime_ns,
                ):
                    raise VideoGenieError("INPUT_CHANGED", "Managed blob changed before commitment")
            connection.execute(
                "INSERT INTO asset_versions VALUES (?,?,?,?,?)",
                (
                    record.version_id,
                    record.asset_id,
                    record.model_dump_json(),
                    request.storage_mode,
                    location,
                ),
            )
            connection.execute(
                "INSERT INTO asset_requests VALUES (?,?,?,?)",
                (request.idempotency_key, request_digest, identity.sha256, record.version_id),
            )
        return record

    def verify_asset(self, asset_id: str, version_id: str) -> AssetVersion:
        """Hash the current location against its immutable pin; never rewrite old metadata."""
        record = self.get_asset(asset_id, version_id)
        self.resolve_asset(
            AssetPin(asset_id=record.asset_id, version_id=record.version_id, sha256=record.sha256)
        )
        return record

    def resolve_asset(self, pin: AssetPin) -> Path:
        """Return an internal local path only after checking exact ID, digest and current bytes.

        A path is not an immutable handle. Workers must recheck identity around execution;
        unrestricted same-account changes after this call remain outside isolation claims.
        """
        try:
            pin = AssetPin.model_validate(pin.model_dump())
        except (ValidationError, AttributeError) as exc:
            raise VideoGenieError("INVALID_ARGUMENT", "Invalid asset pin") from exc
        with self.service.connection() as connection:
            row = self._read(
                connection, AssetLookupInput(asset_id=pin.asset_id, version_id=pin.version_id)
            )
            record = self._record(row)
        if record.sha256 != pin.sha256:
            raise VideoGenieError("INPUT_CHANGED", "Asset pin digest does not match its version")
        if row["storage_mode"] == "copy":
            if row["location"] != record.sha256:
                raise VideoGenieError("INTERNAL_ERROR", "Invalid managed asset location")
            with self._blobs() as descriptor:
                identity = self._blob_identity(descriptor, record.sha256)
                self._check_blob_anchor(descriptor)
            path = self.service.data_dir / "assets" / "blobs" / record.sha256
        else:
            path = Path(row["location"])
            if not path.is_absolute() or path.is_relative_to(self.service.data_dir):
                raise VideoGenieError("INTERNAL_ERROR", "Invalid reference asset location")
            with self._open_source(path) as stream:
                identity = _fingerprint(stream, _MAX_BYTES)
            require_unchanged(path, identity)
        if (identity.sha256, identity.size) != (record.sha256, record.byte_size):
            raise VideoGenieError("INPUT_CHANGED", "Asset bytes changed after registration")
        return path
