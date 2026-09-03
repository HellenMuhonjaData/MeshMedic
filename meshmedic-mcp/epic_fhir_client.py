"""Backend-service (SMART Backend Services) client for Epic's non-production
FHIR sandbox. Handles JWT-assertion OAuth2 token exchange and Patient
retrieval (REQ-009). Setup: an Epic app registered as "Backend Systems" with
CLIENT_ID below, a JWKS published at the URL configured in that app pointing
at the public half of PRIVATE_KEY_PATH's key pair, and Patient.Read/Search
(R4) selected as Incoming APIs. See PROGRESS.md for the registration steps.
"""

import time
import uuid
from pathlib import Path

import httpx
import jwt

from logging_utils import (
    EXTERNAL_CALL_FINISHED,
    EXTERNAL_CALL_STARTED,
    configure_json_logging,
    log_event,
)

_logger = configure_json_logging()

FHIR_BASE_URL = "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"
TOKEN_URL = "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token"
CLIENT_ID = "f7872397-d9b8-419c-8d08-7b2e3555d3a3"
KID = "meshmedic-fhir-cdce91f85d6f"
PRIVATE_KEY_PATH = Path(__file__).parent / "epic_fhir_private_key.pem"

REQUEST_TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 2  # 1 retry on network-level failure; HTTP error responses fail fast, not retried


class EpicFHIRError(Exception):
    """Raised for any failure talking to Epic's sandbox (auth or data
    retrieval). Callers get one exception type; messages never include the
    raw response body, since it could echo back request details."""


def _build_client_assertion_jwt() -> str:
    if not PRIVATE_KEY_PATH.exists():
        raise EpicFHIRError(
            f"No private key found at {PRIVATE_KEY_PATH} for Epic backend-service auth."
        )
    private_key = PRIVATE_KEY_PATH.read_text(encoding="utf-8")
    now = int(time.time())
    claims = {
        "iss": CLIENT_ID,
        "sub": CLIENT_ID,
        "aud": TOKEN_URL,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "nbf": now,
        "exp": now + 240,  # Epic requires <= 5 minutes; stay under with margin
    }
    return jwt.encode(claims, private_key, algorithm="RS384", headers={"kid": KID})


def _get_access_token(correlation_id: str) -> str:
    """Exchange a signed JWT assertion for a bearer access token."""
    data = {
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": _build_client_assertion_jwt(),
    }
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log_event(
            _logger, "info", EXTERNAL_CALL_STARTED, correlation_id,
            target="epic_token_exchange", attempt=attempt,
        )
        start = time.perf_counter()
        try:
            response = httpx.post(TOKEN_URL, data=data, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.RequestError as e:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            log_event(
                _logger, "warning", EXTERNAL_CALL_FINISHED, correlation_id,
                target="epic_token_exchange", attempt=attempt,
                duration_ms=duration_ms, outcome="failure", error_class="RequestError",
            )
            last_error = e
            continue
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        if response.status_code >= 400:
            log_event(
                _logger, "warning", EXTERNAL_CALL_FINISHED, correlation_id,
                target="epic_token_exchange", attempt=attempt, duration_ms=duration_ms,
                outcome="failure", status_code=response.status_code, error_class="UpstreamUnavailable",
            )
            raise EpicFHIRError(
                f"Epic token endpoint rejected the request (status {response.status_code})."
            )
        log_event(
            _logger, "info", EXTERNAL_CALL_FINISHED, correlation_id,
            target="epic_token_exchange", attempt=attempt, duration_ms=duration_ms,
            outcome="success", status_code=response.status_code,
        )
        return response.json()["access_token"]
    raise EpicFHIRError(
        f"Could not reach Epic token endpoint after {MAX_ATTEMPTS} attempts."
    ) from last_error


def fetch_patient(fhir_patient_id: str, correlation_id: str) -> dict | None:
    """Retrieve a Patient resource from Epic's non-production FHIR sandbox by
    its Epic-assigned FHIR ID. Returns None if Epic reports no such patient
    (404) so the caller can decide how to surface "not found"; raises
    EpicFHIRError for any other failure (auth, network, unexpected status).

    `correlation_id` is the calling tool invocation's id (see logging_utils)
    -- threaded through so the token-exchange and patient-fetch external
    calls trace back to the same tool call that triggered them."""
    access_token = _get_access_token(correlation_id)
    url = f"{FHIR_BASE_URL}/Patient/{fhir_patient_id}"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/fhir+json"}

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log_event(
            _logger, "info", EXTERNAL_CALL_STARTED, correlation_id,
            target="epic_patient_fetch", attempt=attempt,
        )
        start = time.perf_counter()
        try:
            response = httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.RequestError as e:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            log_event(
                _logger, "warning", EXTERNAL_CALL_FINISHED, correlation_id,
                target="epic_patient_fetch", attempt=attempt,
                duration_ms=duration_ms, outcome="failure", error_class="RequestError",
            )
            last_error = e
            continue
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        if response.status_code == 404:
            log_event(
                _logger, "info", EXTERNAL_CALL_FINISHED, correlation_id,
                target="epic_patient_fetch", attempt=attempt, duration_ms=duration_ms,
                outcome="success", status_code=404,
            )
            return None
        if response.status_code >= 400:
            log_event(
                _logger, "warning", EXTERNAL_CALL_FINISHED, correlation_id,
                target="epic_patient_fetch", attempt=attempt, duration_ms=duration_ms,
                outcome="failure", status_code=response.status_code, error_class="UpstreamUnavailable",
            )
            raise EpicFHIRError(
                f"Epic FHIR API rejected the request (status {response.status_code})."
            )
        log_event(
            _logger, "info", EXTERNAL_CALL_FINISHED, correlation_id,
            target="epic_patient_fetch", attempt=attempt, duration_ms=duration_ms,
            outcome="success", status_code=response.status_code,
        )
        return response.json()
    raise EpicFHIRError(
        f"Could not reach Epic FHIR API after {MAX_ATTEMPTS} attempts."
    ) from last_error
