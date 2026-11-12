"""Local owner verifier; no CLI/MCP endpoint or transferable approval capability.

The trusted console controls enrollment. Same-account arbitrary code/filesystem
access is outside this boundary. T285 must consume proofs and apply the exact
domain action in one transaction; a receipt-shaped object is never authority.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import ValidationError
from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorAssertionResponse,
    AuthenticatorAttestationResponse,
    RegistrationCredential,
)

from videogenie.contracts.authority import (
    OwnerAction,
    OwnerActionReceipt,
    OwnerActionRequest,
    OwnerChallenge,
    OwnerChallengeInput,
    OwnerEnrollmentChallenge,
    OwnerEnrollmentReceipt,
    RegisterOwnerInput,
    VerifyOwnerActionInput,
)
from videogenie.contracts.models import ContractModel, VideoGenieError
from videogenie.contracts.recipes import content_sha256
from videogenie.core.service import VideoGenieService

TTL_SECONDS = 300
_TABLES = {"owner_auth_config", "owner_credentials", "owner_challenges", "owner_receipts"}
_COLUMNS = {
    "owner_auth_config": "singleton version instance_id rp_id origin epoch recovery_hash",
    "owner_credentials": "credential_id public_key user_handle sign_count epoch registered_at "
    "revoked_at device_type backed_up",
    "owner_challenges": "challenge_id kind record_json epoch request_key request_sha256 "
    "bearer_hash next_recovery_hash invalidated",
    "owner_receipts": "receipt_id challenge_id kind proof_sha256 record_json",
}
_GUARDS = {
    "owner_receipts_no_update",
    "owner_receipts_no_delete",
    "owner_challenge_identity",
    "owner_config_identity",
    "owner_credential_identity",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _deny(reason: str) -> VideoGenieError:
    return VideoGenieError("ACCESS_DENIED", "Owner verification failed", details={"reason": reason})


def _checked[T: ContractModel](model: type[T], value: T) -> T:
    try:
        return model.model_validate(value.model_dump())
    except (ValidationError, AttributeError) as exc:
        raise VideoGenieError("INVALID_ARGUMENT", "Invalid owner request") from exc


def _client_data(raw: bytes, origin: str, kind: str) -> None:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    try:
        data = json.loads(raw, object_pairs_hook=pairs)
        if (
            not isinstance(data, dict)
            or data.get("type") != kind
            or data.get("origin") != origin
            or ("crossOrigin" in data and data["crossOrigin"] is not False)
            or "topOrigin" in data
        ):
            raise ValueError("unexpected client context")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _deny("client_context") from exc


@dataclass(frozen=True)
class VerifiedAction:
    """Internal result of live database verification, never a caller-supplied token."""

    action: OwnerAction
    receipt: OwnerActionReceipt
    replayed: bool


@dataclass(frozen=True)
class EnrollmentTicket:
    """Console-only secrets: display/store recovery secret before committing enrollment.

    Never serialize this object to agent tools, status endpoints or logs. Repr
    hiding is defense against accidental logging, not same-account isolation.
    """

    challenge: OwnerEnrollmentChallenge
    bearer: bytes = field(repr=False)
    next_recovery_secret: bytes = field(repr=False)


class OwnerAuth:
    """Bind an immutable localhost RP/origin policy to one explicit local registry."""

    def __init__(self, service: VideoGenieService, *, origin: str) -> None:
        self.service = service
        try:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname != "localhost"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.port is None
                or parsed.port == 0
                or origin != f"{parsed.scheme}://localhost:{parsed.port}"
            ):
                raise ValueError("invalid local origin")
        except (ValueError, TypeError) as exc:
            raise VideoGenieError(
                "INVALID_ARGUMENT", "Use an exact localhost origin and port"
            ) from exc
        self.origin = origin
        self.rp_id = "localhost"
        self._initialize()

    def _initialize(self) -> None:
        with self.service.connection(write=True) as connection:
            present = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            } & _TABLES
            if present:
                if present != _TABLES:
                    raise VideoGenieError(
                        "UNSUPPORTED_SCHEMA", "Incomplete owner authentication schema"
                    )
                for table, columns in _COLUMNS.items():
                    if [
                        row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                    ] != columns.split():
                        raise VideoGenieError("UNSUPPORTED_SCHEMA", "Incomplete owner table layout")
                rows = connection.execute("SELECT * FROM owner_auth_config").fetchall()
                if len(rows) != 1 or rows[0]["version"] != 1:
                    raise VideoGenieError(
                        "UNSUPPORTED_SCHEMA", "Unsupported owner authentication schema"
                    )
                if rows[0]["origin"] != self.origin or rows[0]["rp_id"] != self.rp_id:
                    raise _deny("rp_policy_changed")
                guards = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }
                if not _GUARDS <= guards:
                    raise VideoGenieError("UNSUPPORTED_SCHEMA", "Missing owner immutability guards")
                self.application_instance_id = rows[0]["instance_id"]
                return
            connection.execute("""CREATE TABLE owner_auth_config (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
                instance_id TEXT NOT NULL UNIQUE, rp_id TEXT NOT NULL, origin TEXT NOT NULL,
                epoch INTEGER NOT NULL, recovery_hash TEXT
            )""")
            connection.execute("""CREATE TABLE owner_credentials (
                credential_id TEXT PRIMARY KEY, public_key BLOB NOT NULL,
                user_handle BLOB NOT NULL, sign_count INTEGER NOT NULL,
                epoch INTEGER NOT NULL, registered_at TEXT NOT NULL, revoked_at TEXT,
                device_type TEXT NOT NULL, backed_up INTEGER NOT NULL
            )""")
            connection.execute("""CREATE TABLE owner_challenges (
                challenge_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                record_json TEXT NOT NULL, epoch INTEGER NOT NULL,
                request_key TEXT UNIQUE, request_sha256 TEXT,
                bearer_hash TEXT, next_recovery_hash TEXT,
                invalidated INTEGER NOT NULL DEFAULT 0
            )""")
            connection.execute("""CREATE TABLE owner_receipts (
                receipt_id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL UNIQUE
                    REFERENCES owner_challenges(challenge_id),
                kind TEXT NOT NULL, proof_sha256 TEXT NOT NULL, record_json TEXT NOT NULL
            )""")
            for operation in ("UPDATE", "DELETE"):
                connection.execute(f"""CREATE TRIGGER owner_receipts_no_{operation.lower()}
                    BEFORE {operation} ON owner_receipts
                    BEGIN SELECT RAISE(ABORT,'Owner receipts are immutable'); END""")
            for name, table, columns in (
                (
                    "owner_challenge_identity",
                    "owner_challenges",
                    "challenge_id,kind,record_json,epoch,request_key,request_sha256,bearer_hash,next_recovery_hash",
                ),
                (
                    "owner_config_identity",
                    "owner_auth_config",
                    "singleton,version,instance_id,rp_id,origin",
                ),
                (
                    "owner_credential_identity",
                    "owner_credentials",
                    "credential_id,public_key,user_handle,epoch,registered_at,device_type",
                ),
            ):
                connection.execute(f"""CREATE TRIGGER {name} BEFORE UPDATE OF {columns} ON {table}
                    BEGIN SELECT RAISE(ABORT,'Owner identity is immutable'); END""")
            self.application_instance_id = str(uuid4())
            connection.execute(
                "INSERT INTO owner_auth_config VALUES(1,1,?,?,?,0,NULL)",
                (self.application_instance_id, self.rp_id, self.origin),
            )

    @contextmanager
    def _transaction(self, connection: sqlite3.Connection) -> Iterator[None]:
        databases = connection.execute("PRAGMA database_list").fetchall()
        if not connection.in_transaction or not any(
            row[1] == "main" and Path(row[2]).resolve() == self.service.db_path.resolve()
            for row in databases
        ):
            raise VideoGenieError(
                "INVALID_ARGUMENT", "Owner consumption needs this registry transaction"
            )
        connection.execute("SAVEPOINT owner_auth_consume")
        try:
            yield
        except BaseException:
            connection.execute("ROLLBACK TO owner_auth_consume")
            connection.execute("RELEASE owner_auth_consume")
            raise
        else:
            connection.execute("RELEASE owner_auth_consume")

    def begin_bootstrap(self) -> EnrollmentTicket:
        """Trusted owner-console control path only; ordinary agent transports must not call it.

        Initial supervision is the console's bootstrap trust boundary, not
        inferred from this Python call. No credential history may exist.
        """
        return self._begin_enrollment(None)

    def begin_recovery(self, recovery_secret: bytes) -> EnrollmentTicket:
        """Trusted offline owner recovery path, requiring the previously delivered secret."""
        if type(recovery_secret) is not bytes or len(recovery_secret) != 32:
            raise _deny("recovery_secret")
        return self._begin_enrollment(recovery_secret)

    def _begin_enrollment(self, recovery_secret: bytes | None) -> EnrollmentTicket:
        with self.service.connection(write=True) as connection:
            config = connection.execute("SELECT * FROM owner_auth_config").fetchone()
            mode: Literal["bootstrap", "recovery"]
            if recovery_secret is None:
                if (
                    config["epoch"] != 0
                    or connection.execute("SELECT 1 FROM owner_credentials").fetchone()
                ):
                    raise _deny("bootstrap_closed")
                mode = "bootstrap"
            else:
                if not config["recovery_hash"] or not hmac.compare_digest(
                    hashlib.sha256(recovery_secret).hexdigest(), config["recovery_hash"]
                ):
                    raise _deny("recovery_secret")
                mode = "recovery"
            now = _now()
            challenge = OwnerEnrollmentChallenge(
                application_instance_id=self.application_instance_id,
                challenge_id=str(uuid4()),
                mode=mode,
                nonce=bytes_to_base64url(secrets.token_bytes(32)),
                user_id=bytes_to_base64url(secrets.token_bytes(32)),
                origin=self.origin,
                issued_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=TTL_SECONDS)).isoformat(),
            )
            bearer, recovery = secrets.token_bytes(32), secrets.token_bytes(32)
            connection.execute(
                "INSERT INTO owner_challenges(challenge_id,kind,record_json,epoch,"
                "bearer_hash,next_recovery_hash) "
                "VALUES(?,'enrollment',?,?,?,?)",
                (
                    challenge.challenge_id,
                    challenge.model_dump_json(),
                    config["epoch"],
                    hashlib.sha256(bearer).hexdigest(),
                    hashlib.sha256(recovery).hexdigest(),
                ),
            )
            return EnrollmentTicket(challenge, bearer, recovery)

    def complete_enrollment(
        self,
        request: RegisterOwnerInput,
        *,
        bearer: bytes,
    ) -> OwnerEnrollmentReceipt:
        """Atomically commit UV registration, credential rotation and pending invalidation."""
        request = _checked(RegisterOwnerInput, request)
        if type(bearer) is not bytes or len(bearer) != 32:
            raise _deny("enrollment_bearer")
        with self.service.connection(write=True) as connection:
            row = self._challenge_row(connection, request, "enrollment")
            if not hmac.compare_digest(hashlib.sha256(bearer).hexdigest(), row["bearer_hash"]):
                raise _deny("enrollment_bearer")
            proof = content_sha256(request)
            old = connection.execute(
                "SELECT * FROM owner_receipts WHERE challenge_id=?", (request.challenge_id,)
            ).fetchone()
            if old is not None:
                if old["proof_sha256"] != proof:
                    raise _deny("replay_changed")
                return OwnerEnrollmentReceipt.model_validate_json(old["record_json"])
            challenge = OwnerEnrollmentChallenge.model_validate_json(row["record_json"])
            epoch = connection.execute("SELECT epoch FROM owner_auth_config").fetchone()[0]
            now = _now()
            if row["invalidated"] or row["epoch"] != epoch:
                raise _deny("challenge_invalidated")
            if (
                not datetime.fromisoformat(challenge.issued_at)
                <= now
                < datetime.fromisoformat(challenge.expires_at)
            ):
                raise _deny("challenge_expired")
            if challenge.mode == "bootstrap" and (
                epoch != 0 or connection.execute("SELECT 1 FROM owner_credentials").fetchone()
            ):
                raise _deny("bootstrap_closed")
            client = base64url_to_bytes(request.client_data_json)
            _client_data(client, self.origin, "webauthn.create")
            credential_id = base64url_to_bytes(request.credential_id)
            try:
                verified = verify_registration_response(
                    credential=RegistrationCredential(
                        id=request.credential_id,
                        raw_id=credential_id,
                        response=AuthenticatorAttestationResponse(
                            client_data_json=client,
                            attestation_object=base64url_to_bytes(request.attestation_object),
                        ),
                    ),
                    expected_challenge=base64url_to_bytes(challenge.nonce),
                    expected_rp_id=self.rp_id,
                    expected_origin=self.origin,
                    require_user_presence=True,
                    require_user_verification=True,
                )
            except Exception as exc:
                raise _deny("invalid_registration") from exc
            if verified.credential_id != credential_id or not verified.user_verified:
                raise _deny("credential_mismatch")
            if connection.execute(
                "SELECT 1 FROM owner_credentials WHERE credential_id=?", (request.credential_id,)
            ).fetchone():
                raise _deny("credential_reused")
            stamp = now.isoformat()
            connection.execute(
                "UPDATE owner_credentials SET revoked_at=? WHERE revoked_at IS NULL", (stamp,)
            )
            connection.execute(
                "INSERT INTO owner_credentials VALUES(?,?,?,?,?,?,NULL,?,?)",
                (
                    request.credential_id,
                    verified.credential_public_key,
                    base64url_to_bytes(challenge.user_id),
                    verified.sign_count,
                    epoch + 1,
                    stamp,
                    verified.credential_device_type.value,
                    int(verified.credential_backed_up),
                ),
            )
            connection.execute(
                "UPDATE owner_auth_config SET epoch=?,recovery_hash=?",
                (epoch + 1, row["next_recovery_hash"]),
            )
            connection.execute("UPDATE owner_challenges SET invalidated=1")
            receipt = OwnerEnrollmentReceipt(
                application_instance_id=self.application_instance_id,
                challenge_id=challenge.challenge_id,
                receipt_id=str(uuid4()),
                credential_id=request.credential_id,
                mode=challenge.mode,
                credential_epoch=epoch + 1,
                user_present=True,
                user_verified=True,
                verified_at=stamp,
            )
            connection.execute(
                "INSERT INTO owner_receipts VALUES(?,?,'enrollment',?,?)",
                (receipt.receipt_id, challenge.challenge_id, proof, receipt.model_dump_json()),
            )
            return receipt

    def create_challenge(self, request: OwnerActionRequest) -> OwnerChallenge:
        """Agent-accessible preparation only; no actor field can grant approval."""
        request = _checked(OwnerActionRequest, request)
        with self.service.connection(write=True) as connection:
            epoch = connection.execute("SELECT epoch FROM owner_auth_config").fetchone()[0]
            if epoch == 0:
                raise _deny("owner_not_enrolled")
            old = connection.execute(
                "SELECT * FROM owner_challenges WHERE request_key=?", (request.idempotency_key,)
            ).fetchone()
            digest = content_sha256(request)
            if old is not None:
                if old["request_sha256"] != digest:
                    raise VideoGenieError("IDEMPOTENCY_CONFLICT", "Owner request key was reused")
                return OwnerChallenge.model_validate_json(old["record_json"])
            now = _now()
            challenge = OwnerChallenge(
                challenge_id=str(uuid4()),
                application_instance_id=self.application_instance_id,
                nonce=bytes_to_base64url(secrets.token_bytes(32)),
                action=request.action,
                action_sha256=content_sha256(request.action),
                issued_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=TTL_SECONDS)).isoformat(),
            )
            connection.execute(
                "INSERT INTO owner_challenges(challenge_id,kind,record_json,epoch,"
                "request_key,request_sha256) "
                "VALUES(?,'action',?,?,?,?)",
                (
                    challenge.challenge_id,
                    challenge.model_dump_json(),
                    epoch,
                    request.idempotency_key,
                    digest,
                ),
            )
            return challenge

    def get_challenge(self, request: OwnerChallengeInput) -> OwnerChallenge:
        """Read the exact pending or historical payload without revealing secret bootstrap data."""
        request = _checked(OwnerChallengeInput, request)
        with self.service.connection() as connection:
            row = self._challenge_row(connection, request, "action")
            return OwnerChallenge.model_validate_json(row["record_json"])

    def _challenge_row(
        self, connection: sqlite3.Connection, request: OwnerChallengeInput, kind: str
    ) -> sqlite3.Row:
        if request.application_instance_id != self.application_instance_id:
            raise _deny("application_instance")
        row = connection.execute(
            "SELECT * FROM owner_challenges WHERE challenge_id=? AND kind=?",
            (request.challenge_id, kind),
        ).fetchone()
        if row is None:
            raise VideoGenieError("NOT_FOUND", "Owner challenge not found")
        if not isinstance(row, sqlite3.Row):
            raise VideoGenieError("INVALID_ARGUMENT", "Owner transaction requires named rows")
        return row

    def consume_action(
        self,
        request: VerifyOwnerActionInput,
        expected_action: OwnerAction,
        *,
        connection: sqlite3.Connection,
    ) -> VerifiedAction:
        """Verify/consume in T285's transaction; T285 must commit its domain action atomically.

        Caller rechecks all current domain revisions and scope before committing.
        Exact replay returns the persisted receipt; it must not reapply the action.
        """
        request = _checked(VerifyOwnerActionInput, request)
        with self._transaction(connection):
            row = self._challenge_row(connection, request, "action")
            challenge = OwnerChallenge.model_validate_json(row["record_json"])
            if content_sha256(expected_action) != challenge.action_sha256:
                raise _deny("action_changed")
            proof_hash = content_sha256(request)
            old = connection.execute(
                "SELECT * FROM owner_receipts WHERE challenge_id=?", (request.challenge_id,)
            ).fetchone()
            if old is not None:
                if old["proof_sha256"] != proof_hash:
                    raise _deny("replay_changed")
                return VerifiedAction(
                    challenge.action,
                    OwnerActionReceipt.model_validate_json(old["record_json"]),
                    True,
                )
            now = _now()
            epoch = connection.execute("SELECT epoch FROM owner_auth_config").fetchone()[0]
            if row["invalidated"] or row["epoch"] != epoch:
                raise _deny("challenge_invalidated")
            if (
                not datetime.fromisoformat(challenge.issued_at)
                <= now
                < datetime.fromisoformat(challenge.expires_at)
            ):
                raise _deny("challenge_expired")
            assertion = request.assertion
            stored = connection.execute(
                "SELECT * FROM owner_credentials WHERE credential_id=? "
                "AND revoked_at IS NULL AND epoch=?",
                (assertion.credential_id, epoch),
            ).fetchone()
            if stored is None:
                raise _deny("credential_not_enrolled")
            client = base64url_to_bytes(assertion.client_data_json)
            _client_data(client, self.origin, "webauthn.get")
            handle = base64url_to_bytes(assertion.user_handle) if assertion.user_handle else None
            if handle is not None and handle != stored["user_handle"]:
                raise _deny("user_handle")
            credential_id = base64url_to_bytes(assertion.credential_id)
            try:
                verified = verify_authentication_response(
                    credential=AuthenticationCredential(
                        id=assertion.credential_id,
                        raw_id=credential_id,
                        response=AuthenticatorAssertionResponse(
                            client_data_json=client,
                            authenticator_data=base64url_to_bytes(assertion.authenticator_data),
                            signature=base64url_to_bytes(assertion.signature),
                            user_handle=handle,
                        ),
                    ),
                    expected_challenge=base64url_to_bytes(challenge.nonce),
                    expected_rp_id=self.rp_id,
                    expected_origin=self.origin,
                    credential_public_key=stored["public_key"],
                    credential_current_sign_count=stored["sign_count"],
                    require_user_verification=True,
                )
            except Exception as exc:
                raise _deny("invalid_proof") from exc
            if verified.credential_id != credential_id or not verified.user_verified:
                raise _deny("credential_mismatch")
            if verified.credential_device_type.value != stored["device_type"]:
                raise _deny("backup_eligibility_changed")
            receipt = OwnerActionReceipt(
                receipt_id=str(uuid4()),
                application_instance_id=self.application_instance_id,
                challenge_id=challenge.challenge_id,
                action_sha256=challenge.action_sha256,
                credential_id=assertion.credential_id,
                user_present=True,
                user_verified=True,
                verified_at=now.isoformat(),
            )
            connection.execute(
                "UPDATE owner_credentials SET sign_count=?,backed_up=? WHERE credential_id=?",
                (
                    verified.new_sign_count,
                    int(verified.credential_backed_up),
                    assertion.credential_id,
                ),
            )
            connection.execute(
                "INSERT INTO owner_receipts VALUES(?,?,'action',?,?)",
                (receipt.receipt_id, challenge.challenge_id, proof_hash, receipt.model_dump_json()),
            )
            return VerifiedAction(challenge.action, receipt, False)
