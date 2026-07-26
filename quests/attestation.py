"""Device attestation for quest photo proofs.

Closes the gap that EXIF alone cannot: a photo picked from the gallery has the
same metadata as one just taken. Attestation does not inspect the photo — it
proves the *app* is genuine and unmodified on genuine hardware. Combined with
the fact that a genuine build only ever opens the camera for quest proofs
(there is no gallery code path), that chain gives us:

    genuine, unmodified app  +  request provably from that app
    ⇒ the photo came through the camera.

What it still cannot stop: someone pointing a real camera at a screen. No
software check closes that one; the moderator does.

Verification runs server-side via `pyattest` for both platforms:
  * Android — Play Integrity token, decrypted locally with the response
    encryption keys from Play Console.
  * iOS — App Attest. Two steps: a device registers a Secure Enclave key once
    (attestation), then signs each later request with it (assertion).

Replay is prevented by single-use, short-lived, per-user challenges issued by
`issue_challenge` and burned by `consume_challenge`.
"""
import base64
import secrets
from dataclasses import dataclass
from datetime import timedelta

from asgiref.sync import async_to_sync
from cryptography.x509 import load_der_x509_certificate
from django.conf import settings
from django.db import transaction
from django.utils import timezone

CHALLENGE_TTL = timedelta(minutes=10)


class Mode:
    OFF = 'off'          # attestation ignored entirely
    AUDIT = 'audit'      # verified and recorded when supplied, never blocks
    ENFORCE = 'enforce'  # a passing attestation is required to submit


def current_mode():
    mode = getattr(settings, 'QUEST_ATTESTATION_MODE', Mode.AUDIT)
    return mode if mode in (Mode.OFF, Mode.AUDIT, Mode.ENFORCE) else Mode.AUDIT


class Status:
    SKIPPED = 'skipped'          # attestation not in use
    VERIFIED = 'verified'        # passed
    FAILED = 'failed'            # supplied but did not verify
    MISSING = 'missing'          # client sent nothing
    UNSUPPORTED = 'unsupported'  # device/OS cannot attest
    ERROR = 'error'              # server misconfigured or verifier blew up


@dataclass
class AttestationResult:
    status: str
    platform: str = ''
    detail: str = ''

    @property
    def ok(self):
        return self.status == Status.VERIFIED

    @property
    def blocks_submission(self):
        """Whether this result should stop the proof being accepted."""
        if current_mode() != Mode.ENFORCE:
            return False
        return not self.ok


# ── Challenges ────────────────────────────────────────────────────────────────

def issue_challenge(user):
    """A fresh single-use nonce bound to this user."""
    from .models import AttestationChallenge

    nonce = secrets.token_bytes(32)
    challenge = AttestationChallenge.objects.create(
        user=user, nonce=base64.b64encode(nonce).decode(),
    )
    # Purge this user's stale challenges so the table cannot grow unbounded.
    AttestationChallenge.objects.filter(
        user=user, created_at__lt=timezone.now() - CHALLENGE_TTL,
    ).delete()
    return challenge


def consume_challenge(user, nonce_b64):
    """Burn a challenge, returning its raw bytes, or None if it is invalid,
    already used, expired, or belongs to someone else."""
    from .models import AttestationChallenge

    if not nonce_b64:
        return None
    cutoff = timezone.now() - CHALLENGE_TTL
    with transaction.atomic():
        challenge = (
            AttestationChallenge.objects
            .select_for_update()
            .filter(user=user, nonce=nonce_b64, used_at__isnull=True, created_at__gte=cutoff)
            .first()
        )
        if challenge is None:
            return None
        challenge.used_at = timezone.now()
        challenge.save(update_fields=['used_at'])
    try:
        return base64.b64decode(nonce_b64)
    except Exception:
        return None


# ── Platform configs ──────────────────────────────────────────────────────────

def _apple_config(key_id_b64):
    from pyattest.configs.apple import AppleConfig

    app_id = getattr(settings, 'APPLE_APP_ID', '')
    if not app_id:
        raise RuntimeError('APPLE_APP_ID is not configured.')
    kwargs = {
        'key_id': base64.b64decode(key_id_b64),
        'app_id': app_id,
        'production': getattr(settings, 'APPLE_ATTEST_PRODUCTION', False),
    }
    root_ca = getattr(settings, 'APPLE_ATTEST_ROOT_CA', None)
    if root_ca:
        kwargs['root_ca'] = root_ca
    return AppleConfig(**kwargs)


_google_config_class = None


def _standard_api_config_class():
    """pyattest checks `requestDetails.nonce`, which only the *classic* Play
    Integrity request returns. @expo/app-integrity issues a *standard*
    request, where Google echoes back `requestDetails.requestHash` verbatim
    instead. Accept either so both request styles verify."""
    global _google_config_class
    if _google_config_class is not None:
        return _google_config_class

    from pyattest.configs.google_play_integrity_api import GooglePlayIntegrityApiConfig
    from pyattest.exceptions import InvalidNonceException
    from pyattest.verifiers.google_play_integrity_attestation import (
        GooglePlayIntegrityAttestationVerifier,
    )

    class StandardRequestVerifier(GooglePlayIntegrityAttestationVerifier):
        def check_request_details(self, request_details):
            request_hash = request_details.get('requestHash')
            if request_hash is not None:
                # Standard API: opaque string, returned exactly as we sent it.
                if request_hash != base64.b64encode(self.attestation.nonce).decode():
                    raise InvalidNonceException
            else:
                self.verify_nonce(request_details.get('nonce'))
            self.verify_apk_package_name(request_details.get('requestPackageName'))

    class StandardApiConfig(GooglePlayIntegrityApiConfig):
        attestation_verifier_class = StandardRequestVerifier

    _google_config_class = StandardApiConfig
    return StandardApiConfig


def _google_config():
    GooglePlayIntegrityApiConfig = _standard_api_config_class()

    decryption_key = getattr(settings, 'PLAY_INTEGRITY_DECRYPTION_KEY', '')
    verification_key = getattr(settings, 'PLAY_INTEGRITY_VERIFICATION_KEY', '')
    if not decryption_key or not verification_key:
        raise RuntimeError('Play Integrity response keys are not configured.')
    return GooglePlayIntegrityApiConfig(
        decryption_key=decryption_key,
        verification_key=verification_key,
        apk_package_name=getattr(settings, 'ANDROID_PACKAGE_NAME', ''),
        production=getattr(settings, 'PLAY_INTEGRITY_PRODUCTION', True),
        allow_non_play_distribution=getattr(
            settings, 'PLAY_INTEGRITY_ALLOW_NON_PLAY', False
        ),
        required_device_verdict=getattr(
            settings, 'PLAY_INTEGRITY_REQUIRED_VERDICT', 'MEETS_DEVICE_INTEGRITY'
        ),
    )


# ── Android ───────────────────────────────────────────────────────────────────

def verify_android(token, nonce):
    """Verify a Play Integrity token whose requestHash was the challenge."""
    from pyattest.attestation import Attestation
    from pyattest.exceptions import PyAttestException

    try:
        config = _google_config()
    except Exception as exc:
        # Missing or malformed server-side keys are our problem, not the
        # user's — reported as ERROR so it is distinguishable from a bad token.
        return AttestationResult(Status.ERROR, 'android', _reason(exc))

    try:
        attestation = Attestation(token, nonce, config)
        async_to_sync(attestation.verify)()
    except PyAttestException as exc:
        return AttestationResult(Status.FAILED, 'android', _reason(exc))
    except Exception as exc:  # malformed token, decryption failure, …
        return AttestationResult(Status.FAILED, 'android', _reason(exc))

    return AttestationResult(Status.VERIFIED, 'android', 'Play Integrity verdict accepted.')


# ── iOS ───────────────────────────────────────────────────────────────────────

def register_ios_key(user, key_id_b64, attestation_b64, nonce):
    """Verify a first-time App Attest attestation and store the device's
    public key, so later assertions from it can be checked."""
    from pyattest.attestation import Attestation
    from pyattest.exceptions import PyAttestException

    from .models import AttestedDevice

    try:
        config = _apple_config(key_id_b64)
    except Exception as exc:
        return AttestationResult(Status.ERROR, 'ios', _reason(exc))

    try:
        raw = base64.b64decode(attestation_b64)
        attestation = Attestation(raw, nonce, config)
        async_to_sync(attestation.verify)()
        leaf_der = attestation.data['data']['raw']['attStmt']['x5c'][0]
    except PyAttestException as exc:
        return AttestationResult(Status.FAILED, 'ios', _reason(exc))
    except Exception as exc:
        return AttestationResult(Status.FAILED, 'ios', _reason(exc))

    AttestedDevice.objects.update_or_create(
        user=user,
        key_id=key_id_b64,
        defaults={
            'platform': 'ios',
            'certificate': base64.b64encode(leaf_der).decode(),
            'sign_count': 0,
            'last_used_at': timezone.now(),
        },
    )
    return AttestationResult(Status.VERIFIED, 'ios', 'App Attest key registered.')


def verify_ios_assertion(user, key_id_b64, assertion_b64, nonce):
    """Verify an App Attest assertion from a device registered earlier."""
    from pyattest.assertion import Assertion
    from pyattest.exceptions import PyAttestException

    from .models import AttestedDevice

    device = AttestedDevice.objects.filter(user=user, key_id=key_id_b64).first()
    if device is None:
        return AttestationResult(
            Status.FAILED, 'ios', 'Device key is not registered; attest first.'
        )

    try:
        config = _apple_config(key_id_b64)
    except Exception as exc:
        return AttestationResult(Status.ERROR, 'ios', _reason(exc))

    try:
        from hashlib import sha256

        certificate = load_der_x509_certificate(base64.b64decode(device.certificate))
        assertion = Assertion(
            base64.b64decode(assertion_b64),
            sha256(nonce).digest(),  # Apple's clientDataHash
            certificate.public_key(),
            config,
        )
        assertion.verify()
        counter = assertion.data.get('counter', 0)
    except PyAttestException as exc:
        return AttestationResult(Status.FAILED, 'ios', _reason(exc))
    except Exception as exc:
        return AttestationResult(Status.FAILED, 'ios', _reason(exc))

    # The Secure Enclave counter only ever increases; a repeat or a rewind
    # means a replayed assertion.
    if counter <= device.sign_count:
        return AttestationResult(
            Status.FAILED, 'ios',
            f'Assertion counter did not advance ({counter} <= {device.sign_count}).',
        )

    device.sign_count = counter
    device.last_used_at = timezone.now()
    device.save(update_fields=['sign_count', 'last_used_at'])
    return AttestationResult(Status.VERIFIED, 'ios', 'App Attest assertion accepted.')


# ── Entry point used by the proof submission view ─────────────────────────────

def attest_submission(user, data):
    """Resolve the attestation attached to a proof submission.

    `data` is the request payload; the client sends `attestation_platform`,
    `attestation_nonce` (the challenge it was issued) and either
    `attestation_token` (Android) or `attestation_assertion` + `attestation_key_id`
    (iOS). Never raises — every path returns an AttestationResult so the
    caller decides what to do with it based on the configured mode.
    """
    mode = current_mode()
    if mode == Mode.OFF:
        return AttestationResult(Status.SKIPPED, '', 'Attestation disabled.')

    platform = (data.get('attestation_platform') or '').lower()
    if platform not in ('ios', 'android'):
        return AttestationResult(Status.MISSING, platform, 'No attestation supplied.')

    if str(data.get('attestation_unsupported', '')).lower() in ('1', 'true', 'yes'):
        return AttestationResult(
            Status.UNSUPPORTED, platform, 'Device cannot perform attestation.'
        )

    nonce = consume_challenge(user, data.get('attestation_nonce'))
    if nonce is None:
        return AttestationResult(
            Status.FAILED, platform, 'Challenge missing, expired, or already used.'
        )

    try:
        if platform == 'android':
            token = data.get('attestation_token')
            if not token:
                return AttestationResult(Status.MISSING, platform, 'No integrity token.')
            return verify_android(token, nonce)

        assertion = data.get('attestation_assertion')
        key_id = data.get('attestation_key_id')
        if not assertion or not key_id:
            return AttestationResult(Status.MISSING, platform, 'No assertion supplied.')
        return verify_ios_assertion(user, key_id, assertion, nonce)
    except Exception as exc:  # never let attestation break the upload path
        return AttestationResult(Status.ERROR, platform, _reason(exc))


def _reason(exc):
    text = str(exc) or exc.__class__.__name__
    return text[:250]
