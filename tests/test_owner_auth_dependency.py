"""Exercise the pinned verifier, not an owner ceremony or application authority.

Synthetic credentials use cryptography's signing API. No production verifier or
private owner key is implemented here. T284 must bind credential IDs to enrolled
keys and enforce challenge expiry, single consumption, and action scope.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from importlib.metadata import requires, version
from pathlib import Path

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorAssertionResponse,
    AuthenticatorAttestationResponse,
    RegistrationCredential,
)

CHALLENGE = bytes(range(32))
RP_ID = "localhost"
ORIGIN = "http://localhost:8456"
CREDENTIAL_ID = b"fictional-test-credential"


def _key() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    public = cbor2.dumps(
        {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32), -3: numbers.y.to_bytes(32)}
    )
    return key, public


def _client_data(kind: str) -> bytes:
    return json.dumps(
        {"type": kind, "challenge": bytes_to_base64url(CHALLENGE), "origin": ORIGIN}
    ).encode()


def _assertion(flags: int = 5) -> tuple[AuthenticationCredential, bytes]:
    key, public = _key()
    client = _client_data("webauthn.get")
    auth = hashlib.sha256(RP_ID.encode()).digest() + bytes([flags]) + (1).to_bytes(4)
    signature = key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
    return AuthenticationCredential(
        id=bytes_to_base64url(CREDENTIAL_ID),
        raw_id=CREDENTIAL_ID,
        response=AuthenticatorAssertionResponse(client, auth, signature),
    ), public


def _registration(flags: int = 69, fmt: object = "none") -> RegistrationCredential:
    _, public = _key()
    auth = (
        hashlib.sha256(RP_ID.encode()).digest()
        + bytes([flags])
        + (0).to_bytes(4)
        + bytes(16)
        + len(CREDENTIAL_ID).to_bytes(2)
        + CREDENTIAL_ID
        + public
    )
    return RegistrationCredential(
        id=bytes_to_base64url(CREDENTIAL_ID),
        raw_id=CREDENTIAL_ID,
        response=AuthenticatorAttestationResponse(
            _client_data("webauthn.create"),
            cbor2.dumps({"fmt": fmt, "authData": auth, "attStmt": {}}),
        ),
    )


def test_pinned_dependency_metadata() -> None:
    assert version("webauthn") == "3.0.1"
    assert "webauthn==3.0.1" in (requires("video-genie") or [])


def test_real_signed_assertion_and_registration() -> None:
    credential, public = _assertion()
    result = verify_authentication_response(
        credential=credential,
        expected_challenge=CHALLENGE,
        expected_rp_id=RP_ID,
        expected_origin=ORIGIN,
        credential_public_key=public,
        credential_current_sign_count=0,
        require_user_verification=True,
    )
    assert result.credential_id == CREDENTIAL_ID
    assert result.user_verified and result.new_sign_count == 1
    registered = verify_registration_response(
        credential=_registration(),
        expected_challenge=CHALLENGE,
        expected_rp_id=RP_ID,
        expected_origin=ORIGIN,
        require_user_verification=True,
    )
    assert registered.credential_id == CREDENTIAL_ID
    assert registered.user_verified


@pytest.mark.parametrize(
    "failure", ["challenge", "origin", "rp_id", "uv", "up", "signature", "key", "counter", "id"]
)
def test_authentication_rejects_invalid_proof(failure: str) -> None:
    credential, public = _assertion(flags={"uv": 1, "up": 4}.get(failure, 5))
    if failure == "signature":
        credential.response.signature = b"invalid-signature"
    elif failure == "key":
        _, public = _key()
    elif failure == "id":
        credential = replace(credential, id=bytes_to_base64url(b"different"))
    with pytest.raises(InvalidAuthenticationResponse):
        verify_authentication_response(
            credential=credential,
            expected_challenge=b"wrong" if failure == "challenge" else CHALLENGE,
            expected_rp_id="other.localhost" if failure == "rp_id" else RP_ID,
            expected_origin="http://localhost:9999" if failure == "origin" else ORIGIN,
            credential_public_key=public,
            credential_current_sign_count=1 if failure == "counter" else 0,
            require_user_verification=True,
        )


@pytest.mark.parametrize("failure", ["challenge", "origin", "rp_id", "uv", "up", "format"])
def test_registration_rejects_invalid_proof(failure: str) -> None:
    with pytest.raises(InvalidRegistrationResponse):
        verify_registration_response(
            credential=_registration(
                flags={"uv": 65, "up": 68}.get(failure, 69),
                fmt=123 if failure == "format" else "none",
            ),
            expected_challenge=b"wrong" if failure == "challenge" else CHALLENGE,
            expected_rp_id="other.localhost" if failure == "rp_id" else RP_ID,
            expected_origin="http://localhost:9999" if failure == "origin" else ORIGIN,
            require_user_verification=True,
        )


def test_installed_wheel_verifier(tmp_path: Path) -> None:
    """A fresh interpreter outside the checkout verifies a signed wire response."""
    uv = shutil.which("uv")
    assert uv is not None
    repository = Path(__file__).resolve().parents[1]
    provided = os.environ.get("VIDEOGENIE_TEST_WHEEL")
    if provided is None:
        subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
            cwd=repository,
            check=True,
            capture_output=True,
            timeout=60,
        )
        wheel = next((tmp_path / "dist").glob("*.whl"))
    else:
        wheel = Path(provided)
        assert wheel.is_absolute() and wheel.is_file() and wheel.suffix == ".whl"
    runtime = tmp_path / "runtime"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(runtime)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    python = runtime / "bin" / "python"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    credential, public = _assertion()
    wire = {
        "id": credential.id,
        "rawId": bytes_to_base64url(credential.raw_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(credential.response.client_data_json),
            "authenticatorData": bytes_to_base64url(credential.response.authenticator_data),
            "signature": bytes_to_base64url(credential.response.signature),
        },
    }
    payload = json.dumps({"credential": wire, "public": public.hex()})
    proof = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            """
import json, sys
from pathlib import Path
from importlib.metadata import distribution, version
import videogenie, webauthn
from webauthn.helpers.exceptions import InvalidAuthenticationResponse
for module in (videogenie, webauthn):
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
direct = json.loads(distribution('video-genie').read_text('direct_url.json'))
assert not direct.get('dir_info', {}).get('editable', False)
assert version('webauthn') == '3.0.1'
data = json.load(sys.stdin)
args = dict(credential=data['credential'], expected_challenge=bytes(range(32)),
    expected_rp_id='localhost', expected_origin='http://localhost:8456',
    credential_public_key=bytes.fromhex(data['public']), credential_current_sign_count=0,
    require_user_verification=True)
result = webauthn.verify_authentication_response(**args)
assert result.user_verified and result.credential_id == b'fictional-test-credential'
args['expected_challenge'] = b'wrong'
try:
    webauthn.verify_authentication_response(**args)
except InvalidAuthenticationResponse:
    pass
else:
    raise AssertionError('wrong challenge accepted')
print(json.dumps({'version': version('webauthn'), 'direct_url': direct,
    'verifier': str(Path(webauthn.__file__).resolve()), 'verified': True}))
""",
        ],
        cwd=tmp_path,
        input=payload,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    receipt = {
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "proof": json.loads(proof.stdout),
    }
    (tmp_path / "installed-verifier.json").write_text(json.dumps(receipt, indent=2))
    assert receipt["proof"]["verified"] is True
