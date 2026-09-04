import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import epic_fhir_client


def test_build_client_assertion_jwt_raises_when_client_id_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("EPIC_CLIENT_ID", raising=False)
    key_file = tmp_path / "key.pem"
    key_file.write_text("irrelevant -- fails before this is read", encoding="utf-8")
    monkeypatch.setenv("EPIC_PRIVATE_KEY_PATH", str(key_file))

    with pytest.raises(epic_fhir_client.EpicFHIRError, match="EPIC_CLIENT_ID"):
        epic_fhir_client._build_client_assertion_jwt()


def test_build_client_assertion_jwt_raises_when_private_key_path_missing(monkeypatch):
    monkeypatch.setenv("EPIC_CLIENT_ID", "some-client-id")
    monkeypatch.delenv("EPIC_PRIVATE_KEY_PATH", raising=False)

    with pytest.raises(epic_fhir_client.EpicFHIRError, match="EPIC_PRIVATE_KEY_PATH"):
        epic_fhir_client._build_client_assertion_jwt()


def test_build_client_assertion_jwt_raises_when_key_file_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("EPIC_CLIENT_ID", "some-client-id")
    monkeypatch.setenv("EPIC_PRIVATE_KEY_PATH", str(tmp_path / "does_not_exist.pem"))

    with pytest.raises(epic_fhir_client.EpicFHIRError, match="does not point to an existing file"):
        epic_fhir_client._build_client_assertion_jwt()


def test_build_client_assertion_jwt_succeeds_with_valid_env_and_key(monkeypatch, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(pem)
    monkeypatch.setenv("EPIC_CLIENT_ID", "some-client-id")
    monkeypatch.setenv("EPIC_PRIVATE_KEY_PATH", str(key_file))

    token = epic_fhir_client._build_client_assertion_jwt()

    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "some-client-id"
    assert claims["sub"] == "some-client-id"
    assert claims["aud"] == epic_fhir_client.TOKEN_URL
