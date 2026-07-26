"""Client-facing attestation endpoints: get a challenge, register an iOS key."""
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .attestation import (
    CHALLENGE_TTL,
    Mode,
    consume_challenge,
    current_mode,
    issue_challenge,
    register_ios_key,
)


class AttestationChallengeView(APIView):
    """Hand out a single-use nonce for the device to attest against."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        mode = current_mode()
        if mode == Mode.OFF:
            return Response({'detail': 'Attestation is disabled.', 'mode': mode},
                            status=status.HTTP_409_CONFLICT)
        challenge = issue_challenge(request.user)
        return Response({
            'nonce': challenge.nonce,
            'expires_in': int(CHALLENGE_TTL.total_seconds()),
            'mode': mode,
        }, status=status.HTTP_201_CREATED)


class RegisterAttestationKeyView(APIView):
    """iOS only: verify a first-time App Attest attestation and remember the
    device's key, so later assertions from it can be checked."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        key_id = request.data.get('key_id')
        attestation = request.data.get('attestation')
        nonce_b64 = request.data.get('nonce')

        if not key_id or not attestation:
            return Response({'detail': 'key_id and attestation are required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        nonce = consume_challenge(request.user, nonce_b64)
        if nonce is None:
            return Response({'detail': 'Challenge missing, expired, or already used.'},
                            status=status.HTTP_400_BAD_REQUEST)

        result = register_ios_key(request.user, key_id, attestation, nonce)
        if not result.ok:
            return Response({'detail': result.detail, 'status': result.status},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': result.detail, 'status': result.status},
                        status=status.HTTP_201_CREATED)
