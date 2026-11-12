"""Isolated synthetic credentials exercise real verification, never a human owner ceremony."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from videogenie.adapters import owner_auth as module
from videogenie.adapters.owner_auth import EnrollmentTicket, OwnerAuth
from videogenie.contracts.authority import (
    OwnerActionRequest,
    OwnerChallenge,
    OwnerChallengeInput,
    PublishRecipeAction,
    RegisterOwnerInput,
    VerifyOwnerActionInput,
    WebAuthnAssertion,
)
from videogenie.contracts.models import VideoGenieError
from videogenie.core.service import VideoGenieService

ORIGIN = "http://localhost:8456"


@dataclass
class Authenticator:
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    credential_id: bytes = field(default_factory=lambda: uuid4().bytes)

    def public_key(self) -> bytes:
        point = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: point.x.to_bytes(32), -3: point.y.to_bytes(32)})

    def register(
        self,
        ticket: EnrollmentTicket,
        *,
        flags: int = 69,
        origin: str = ORIGIN,
        rp_id: str = "localhost",
        nonce: str | None = None,
    ) -> RegisterOwnerInput:
        challenge = ticket.challenge
        client = json.dumps(
            {"type": "webauthn.create", "origin": origin, "challenge": nonce or challenge.nonce}
        ).encode()
        auth = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([flags])
            + bytes(4)
            + bytes(16)
            + len(self.credential_id).to_bytes(2)
            + self.credential_id
            + self.public_key()
        )
        return RegisterOwnerInput(
            application_instance_id=challenge.application_instance_id,
            challenge_id=challenge.challenge_id,
            credential_id=bytes_to_base64url(self.credential_id),
            client_data_json=bytes_to_base64url(client),
            attestation_object=bytes_to_base64url(
                cbor2.dumps({"fmt": "none", "authData": auth, "attStmt": {}})
            ),
        )

    def assert_action(
        self,
        challenge: OwnerChallenge,
        *,
        flags: int = 5,
        count: int = 1,
        change: dict[str, object] | None = None,
        rp_id: str = "localhost",
    ) -> VerifyOwnerActionInput:
        client = json.dumps(
            {"type": "webauthn.get", "origin": ORIGIN, "challenge": challenge.nonce}
            | (change or {})
        ).encode()
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + count.to_bytes(4)
        signature = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return VerifyOwnerActionInput(
            application_instance_id=challenge.application_instance_id,
            challenge_id=challenge.challenge_id,
            assertion=WebAuthnAssertion(
                credential_id=bytes_to_base64url(self.credential_id),
                client_data_json=bytes_to_base64url(client),
                authenticator_data=bytes_to_base64url(auth),
                signature=bytes_to_base64url(signature),
            ),
        )


def action() -> PublishRecipeAction:
    return PublishRecipeAction(
        recipe_id=str(uuid4()), expected_draft_revision=1, content_sha256="a" * 64
    )


def challenge(auth: OwnerAuth) -> OwnerChallenge:
    return auth.create_challenge(OwnerActionRequest(action=action(), idempotency_key=str(uuid4())))


@pytest.fixture
def enrolled(tmp_path: Path) -> tuple[OwnerAuth, Authenticator, EnrollmentTicket]:
    auth = OwnerAuth(VideoGenieService(tmp_path), origin=ORIGIN)
    ticket = auth.begin_bootstrap()
    signer = Authenticator()
    auth.complete_enrollment(signer.register(ticket), bearer=ticket.bearer)
    return auth, signer, ticket


def state(auth: OwnerAuth) -> tuple[tuple[object, ...], ...]:
    with auth.service.connection() as connection:
        return tuple(
            tuple(row)
            for table in sorted(module._TABLES)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
        )


def test_real_proof_restart_and_exact_replay(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket],
) -> None:
    auth, signer, ticket = enrolled
    request = signer.register(ticket)
    receipt = auth.complete_enrollment(request, bearer=ticket.bearer)
    assert receipt.credential_epoch == 1 and receipt.user_verified
    assert ticket.bearer.hex() not in repr(ticket)
    assert ticket.next_recovery_secret.hex() not in repr(ticket)
    assert not hasattr(ticket, "model_dump")
    target = challenge(auth)
    proof = signer.assert_action(target)
    with auth.service.connection(write=True) as connection:
        first = auth.consume_action(proof, target.action, connection=connection)
    assert not first.replayed and first.receipt.user_verified
    restarted = OwnerAuth(VideoGenieService(auth.service.data_dir), origin=ORIGIN)
    assert restarted.application_instance_id == auth.application_instance_id
    before = state(auth)
    with auth.service.connection(write=True) as connection:
        replay = restarted.consume_action(proof, target.action, connection=connection)
    assert replay.replayed and replay.receipt == first.receipt
    assert state(auth) == before


@pytest.mark.parametrize(
    "bad",
    [
        "uv",
        "up",
        "challenge",
        "origin",
        "rp",
        "key",
        "credential",
        "handle",
        "cross",
        "cross_type",
        "top",
        "signature",
        "instance",
        "action",
    ],
)
def test_invalid_authentication_has_no_mutation(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket],
    bad: str,
) -> None:
    auth, signer, _ = enrolled
    target = challenge(auth)
    changes: dict[str, dict[str, object]] = {
        "challenge": {"challenge": bytes_to_base64url(b"wrong")},
        "origin": {"origin": "http://localhost:9999"},
        "cross": {"crossOrigin": True},
        "cross_type": {"crossOrigin": 0},
        "top": {"topOrigin": ORIGIN},
    }
    if bad == "key":
        signer = Authenticator(credential_id=signer.credential_id)
    elif bad == "credential":
        signer = Authenticator()
    proof = signer.assert_action(
        target,
        flags={"uv": 1, "up": 4}.get(bad, 5),
        change=changes.get(bad),
        rp_id="elsewhere" if bad == "rp" else "localhost",
    )
    if bad in {"handle", "signature"}:
        proof = proof.model_copy(
            update={
                "assertion": proof.assertion.model_copy(
                    update={
                        "user_handle" if bad == "handle" else "signature": bytes_to_base64url(
                            b"wrong"
                        )
                    }
                )
            }
        )
    if bad == "instance":
        proof = proof.model_copy(update={"application_instance_id": str(uuid4())})
    expected = action() if bad == "action" else target.action
    before = state(auth)
    with auth.service.connection(write=True) as connection, pytest.raises(VideoGenieError):
        auth.consume_action(proof, expected, connection=connection)
    assert state(auth) == before


def test_domain_failure_rolls_back_counter_and_consumption(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket],
) -> None:
    auth, signer, _ = enrolled
    target = challenge(auth)
    proof = signer.assert_action(target)
    before = state(auth)
    with pytest.raises(RuntimeError), auth.service.connection(write=True) as connection:
        auth.consume_action(proof, target.action, connection=connection)
        connection.execute("CREATE TABLE fictional_action(value TEXT)")
        raise RuntimeError("domain revision changed")
    assert state(auth) == before
    with auth.service.connection(write=True) as connection:
        verified = auth.consume_action(proof, target.action, connection=connection)
    assert not verified.replayed


def test_expiry_changed_replay_and_counter_reject(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth, signer, _ = enrolled
    first, expired, reused_counter = challenge(auth), challenge(auth), challenge(auth)
    proof = signer.assert_action(first)
    with auth.service.connection(write=True) as connection:
        auth.consume_action(proof, first.action, connection=connection)
    with auth.service.connection(write=True) as connection, pytest.raises(VideoGenieError):
        auth.consume_action(
            signer.assert_action(first, count=2), first.action, connection=connection
        )
    with auth.service.connection(write=True) as connection, pytest.raises(VideoGenieError):
        auth.consume_action(
            signer.assert_action(reused_counter), reused_counter.action, connection=connection
        )
    now = module._now()
    monkeypatch.setattr(module, "_now", lambda: now + timedelta(seconds=301))
    with auth.service.connection(write=True) as connection, pytest.raises(VideoGenieError):
        auth.consume_action(
            signer.assert_action(expired, count=2), expired.action, connection=connection
        )
    with auth.service.connection(write=True) as connection:
        assert auth.consume_action(proof, first.action, connection=connection).replayed


def test_concurrent_exact_proof_applies_once(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket],
) -> None:
    auth, signer, _ = enrolled
    target = challenge(auth)
    proof = signer.assert_action(target)
    with auth.service.connection(write=True) as connection:
        connection.execute("CREATE TABLE fictional_mutations(receipt TEXT UNIQUE)")

    def apply() -> bool:
        with auth.service.connection(write=True) as connection:
            result = auth.consume_action(proof, target.action, connection=connection)
            if not result.replayed:
                connection.execute(
                    "INSERT INTO fictional_mutations VALUES(?)", (result.receipt.receipt_id,)
                )
            return result.replayed

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: apply(), range(2))) == [False, True]
    with auth.service.connection() as connection:
        assert connection.execute("SELECT count(*) FROM fictional_mutations").fetchone()[0] == 1


@pytest.mark.parametrize("bad", ["uv", "up", "challenge", "origin", "rp", "bearer"])
def test_registration_failure_preserves_bootstrap(tmp_path: Path, bad: str) -> None:
    auth = OwnerAuth(VideoGenieService(tmp_path), origin=ORIGIN)
    ticket = auth.begin_bootstrap()
    signer = Authenticator()
    request = signer.register(
        ticket,
        flags={"uv": 65, "up": 68}.get(bad, 69),
        origin="http://localhost:9999" if bad == "origin" else ORIGIN,
        rp_id="wrong" if bad == "rp" else "localhost",
        nonce=bytes_to_base64url(b"wrong") if bad == "challenge" else None,
    )
    before = state(auth)
    with pytest.raises(VideoGenieError):
        auth.complete_enrollment(request, bearer=bytes(32) if bad == "bearer" else ticket.bearer)
    assert state(auth) == before
    auth.complete_enrollment(signer.register(ticket), bearer=ticket.bearer)


def test_recovery_rotates_only_on_completion(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket],
) -> None:
    auth, old_signer, original = enrolled
    pending = challenge(auth)
    with pytest.raises(VideoGenieError):
        auth.begin_bootstrap()
    before = state(auth)
    with pytest.raises(VideoGenieError):
        auth.begin_recovery(bytes(32))
    assert state(auth) == before
    ticket = auth.begin_recovery(original.next_recovery_secret)
    competitor = auth.begin_recovery(original.next_recovery_secret)
    # Preparing recovery does not revoke the existing credential.
    with auth.service.connection(write=True) as connection:
        auth.consume_action(
            old_signer.assert_action(pending), pending.action, connection=connection
        )
    invalidated = challenge(auth)
    new_signer = Authenticator()
    request = new_signer.register(ticket)
    result = auth.complete_enrollment(request, bearer=ticket.bearer)
    assert result.mode == "recovery" and result.credential_epoch == 2
    assert auth.complete_enrollment(request, bearer=ticket.bearer) == result
    with pytest.raises(VideoGenieError):
        auth.begin_bootstrap()
    with pytest.raises(VideoGenieError):
        auth.begin_recovery(original.next_recovery_secret)
    with pytest.raises(VideoGenieError):
        auth.complete_enrollment(Authenticator().register(competitor), bearer=competitor.bearer)
    with auth.service.connection(write=True) as connection, pytest.raises(VideoGenieError):
        auth.consume_action(
            old_signer.assert_action(invalidated, count=2),
            invalidated.action,
            connection=connection,
        )
    current = challenge(auth)
    with auth.service.connection(write=True) as connection:
        assert not auth.consume_action(
            new_signer.assert_action(current), current.action, connection=connection
        ).replayed
    auth.begin_recovery(ticket.next_recovery_secret)


def test_foreign_or_missing_transaction_and_policy_change_reject(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket], tmp_path: Path
) -> None:
    auth, signer, _ = enrolled
    target = challenge(auth)
    proof = signer.assert_action(target)
    with sqlite3.connect(auth.service.db_path) as connection, pytest.raises(VideoGenieError):
        auth.consume_action(proof, target.action, connection=connection)
    other = VideoGenieService(tmp_path / "other")
    with other.connection(write=True) as connection, pytest.raises(VideoGenieError):
        auth.consume_action(proof, target.action, connection=connection)
    with pytest.raises(VideoGenieError):
        OwnerAuth(auth.service, origin="http://localhost:9999")


def test_strict_models_idempotency_and_no_secret_lookup(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket],
) -> None:
    auth, signer, ticket = enrolled
    request = OwnerActionRequest(action=action(), idempotency_key="same")
    target = auth.create_challenge(request)
    assert auth.create_challenge(request) == target
    with pytest.raises(VideoGenieError):
        auth.create_challenge(request.model_copy(update={"action": action()}))
    with pytest.raises(VideoGenieError):
        auth.get_challenge(
            OwnerChallengeInput(
                application_instance_id=auth.application_instance_id,
                challenge_id=ticket.challenge.challenge_id,
            )
        )
    proof = signer.assert_action(target)
    with pytest.raises(ValidationError):
        VerifyOwnerActionInput.model_validate(proof.model_dump() | {"verified": True})
    with pytest.raises(ValidationError):
        RegisterOwnerInput.model_validate(signer.register(ticket).model_dump() | {"owner": True})
    assert base64url_to_bytes(ticket.challenge.nonce)


def test_enrollment_expiry_and_race_do_not_reset_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = OwnerAuth(VideoGenieService(tmp_path), origin=ORIGIN)
    expired = auth.begin_bootstrap()
    now = module._now()
    monkeypatch.setattr(module, "_now", lambda: now + timedelta(seconds=301))
    before = state(auth)
    with pytest.raises(VideoGenieError):
        auth.complete_enrollment(Authenticator().register(expired), bearer=expired.bearer)
    assert state(auth) == before
    first, second = auth.begin_bootstrap(), auth.begin_bootstrap()
    signers = [Authenticator(), Authenticator()]

    def finish(index: int) -> bool:
        ticket = (first, second)[index]
        try:
            auth.complete_enrollment(signers[index].register(ticket), bearer=ticket.bearer)
            return True
        except VideoGenieError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(finish, range(2))) == [False, True]
    with auth.service.connection() as connection:
        assert connection.execute("SELECT count(*) FROM owner_credentials").fetchone()[0] == 1
        assert connection.execute("SELECT epoch FROM owner_auth_config").fetchone()[0] == 1
    with pytest.raises(VideoGenieError):
        auth.begin_bootstrap()


@pytest.mark.parametrize(
    "raw",
    [
        b"null",
        b"[]",
        b"not-json",
        b"\xff",
        b'{"type":"webauthn.get","type":"webauthn.get","origin":"http://localhost:8456"}',
    ],
)
def test_malformed_client_data_rejects(raw: bytes) -> None:
    with pytest.raises(VideoGenieError):
        module._client_data(raw, ORIGIN, "webauthn.get")


def test_counterless_credentials_still_consume_challenges_once(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket],
) -> None:
    auth, signer, _ = enrolled
    target = challenge(auth)
    proof = signer.assert_action(target, count=0)
    with auth.service.connection(write=True) as connection:
        assert not auth.consume_action(proof, target.action, connection=connection).replayed
    with auth.service.connection(write=True) as connection:
        assert auth.consume_action(proof, target.action, connection=connection).replayed


def test_receipt_and_identity_guards_and_missing_schema_reject(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket],
) -> None:
    auth, _, _ = enrolled
    for sql in (
        "UPDATE owner_receipts SET record_json='{}'",
        "DELETE FROM owner_receipts",
        "UPDATE owner_challenges SET record_json='{}'",
        "UPDATE owner_credentials SET public_key=x'00'",
        "UPDATE owner_auth_config SET origin='http://localhost:9999'",
    ):
        with (
            auth.service.connection(write=True) as connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute(sql)
    with auth.service.connection(write=True) as connection:
        connection.execute("DROP TRIGGER owner_receipts_no_update")
    with pytest.raises(VideoGenieError) as error:
        OwnerAuth(auth.service, origin=ORIGIN)
    assert error.value.code == "UNSUPPORTED_SCHEMA"


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:0",
        "https://example.com:443",
        "http://127.0.0.1:8456",
        "http://localhost:8456/path",
        "http://user@localhost:8456",
        "http://localhost",
    ],
)
def test_invalid_rp_origin_policy_rejects(tmp_path: Path, origin: str) -> None:
    with pytest.raises(VideoGenieError):
        OwnerAuth(VideoGenieService(tmp_path), origin=origin)


def test_backup_eligibility_is_immutable_but_backup_state_may_change(tmp_path: Path) -> None:
    auth = OwnerAuth(VideoGenieService(tmp_path), origin=ORIGIN)
    ticket = auth.begin_bootstrap()
    signer = Authenticator()
    auth.complete_enrollment(signer.register(ticket, flags=77), bearer=ticket.bearer)
    for count, flags in ((1, 29), (2, 13)):
        target = challenge(auth)
        with auth.service.connection(write=True) as connection:
            auth.consume_action(
                signer.assert_action(target, flags=flags, count=count),
                target.action,
                connection=connection,
            )
        with auth.service.connection() as connection:
            assert connection.execute("SELECT backed_up FROM owner_credentials").fetchone()[0] == (
                flags == 29
            )
    target = challenge(auth)
    before = state(auth)
    with auth.service.connection(write=True) as connection, pytest.raises(VideoGenieError) as error:
        auth.consume_action(
            signer.assert_action(target, flags=5, count=3), target.action, connection=connection
        )
    assert error.value.details["reason"] == "backup_eligibility_changed"
    assert state(auth) == before


def test_partial_owner_layout_refuses_restart(
    enrolled: tuple[OwnerAuth, Authenticator, EnrollmentTicket],
) -> None:
    auth, _, _ = enrolled
    with auth.service.connection(write=True) as connection:
        connection.execute("ALTER TABLE owner_credentials ADD COLUMN unknown_future TEXT")
    with pytest.raises(VideoGenieError) as error:
        OwnerAuth(auth.service, origin=ORIGIN)
    assert error.value.code == "UNSUPPORTED_SCHEMA"
