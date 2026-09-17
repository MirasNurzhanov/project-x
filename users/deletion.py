"""Reauthenticated deletion, a grace period, and preservation of shared evidence.

The finalizer deletes the original account. Shared content and audit relations
move to one inert, synthetic identity per account, so existing non-null author
contracts and each recipient's copy remain valid. No credentials, profile,
location, devices, or social links are copied to that identity.
"""
from datetime import timedelta
from collections.abc import Mapping
import math
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from . import socialauth
from .models import User

GRACE_DAYS = 30
REAUTH_MAX_AGE_SECONDS = 300


def verify_reauthentication(user, data):
    """Check a password now, or a freshly issued token for an already linked sub.

    This never resolves/links a social account based on email. An application
    access or refresh JWT alone is deliberately insufficient.
    """
    if not isinstance(data, Mapping):
        raise ValidationError({'detail': 'An object containing reauthentication credentials is required.'})
    password = data.get('password')
    provider = data.get('provider')
    if provider is None:
        if (isinstance(password, str) and user.has_usable_password()
                and user.check_password(password)):
            return
    elif provider in ('google', 'apple'):
        try:
            claims = socialauth.verify_identity_token(provider, data.get('id_token'))
            issued = claims.get('iat')
            fresh = (type(issued) in (int, float) and math.isfinite(issued)
                     and 0 <= timezone.now().timestamp() - issued <= REAUTH_MAX_AGE_SECONDS)
            if fresh and user.social_accounts.filter(provider=provider, subject=claims['sub']).exists():
                return
        except socialauth.SocialAuthError as exc:
            if exc.status == 503:
                raise ValidationError({'detail': 'Identity provider unavailable. Please try again.'}) from exc
    raise ValidationError({
        'detail': 'Confirm your current password or sign in again with a linked Google or Apple account.',
        'code': 'reauthentication_required',
    })


@transaction.atomic
def request_deletion(user_id, data):
    # Serialize concurrent deletion requests and finalization. Provider
    # verification precedes any lifecycle mutation.
    user = User.objects.select_for_update().get(pk=user_id)
    if not user.is_active or user.deletion_requested_at or user.retired_account_id:
        raise ValidationError({'detail': 'This account is unavailable.'})
    verify_reauthentication(user, data)
    now = timezone.now()
    user.deletion_requested_at = now
    user.delete_after = now + timedelta(days=GRACE_DAYS)
    user.is_active = False
    user.ghost_mode = True
    user.push_token = None
    user.save(update_fields=['deletion_requested_at', 'delete_after', 'is_active', 'ghost_mode', 'push_token'])
    user.devices.update(push_token='')
    for token in OutstandingToken.objects.filter(user=user).iterator():
        BlacklistedToken.objects.get_or_create(token=token)
    return user


@transaction.atomic
def finalize_deletion(user_id, *, now=None):
    """Purge one due account atomically. Safe to retry and to run concurrently.

    The explicit allowlist is evidence ownership, not all reverse relations:
    memberships, credentials, devices, preferences and location are deleted
    with the real account. Content/report primary keys never change.
    """
    from clubs.models import Club, ClubEvent, ClubMessage
    from friends.models import BlockedUser, Message
    from moderation.models import ContentReport, ModerationAction
    from stories.models import Story
    from swifts.models import SwiftComment, SwiftReport, SwiftVideo

    user = User.objects.select_for_update().filter(pk=user_id).first()
    now = now or timezone.now()
    if (user is None or user.retired_account_id is not None or user.is_active
            or user.deletion_requested_at is None or user.delete_after is None or user.delete_after > now):
        return False

    label = uuid.uuid4().hex
    retired = User(
        username=f'deleted_{label}', email=f'{label}@deleted.invalid',
        is_active=False, ghost_mode=True, is_private=True,
        discoverable_by_username=False, discoverable_by_interest=False,
        retired_account_id=user.pk,
    )
    retired.set_unusable_password()
    retired.save()
    retained = (
        (BlockedUser, ('blocker', 'blocked')),
        (Message, ('sender', 'receiver')),
        (ClubMessage, ('sender',)),
        (Club, ('creator',)),
        (ClubEvent, ('created_by', 'approved_by')),
        (Story, ('user',)),
        (SwiftVideo, ('author',)),
        (SwiftComment, ('author',)),
        (SwiftReport, ('reporter', 'reviewed_by')),
        (ContentReport, ('reporter', 'target_user', 'reviewed_by')),
        (ModerationAction, ('subject', 'moderator')),
    )
    # Hide only the deleting participant's DM copy, including unread messages.
    Message.objects.filter(sender=user).update(deleted_for_sender=True)
    Message.objects.filter(receiver=user).update(deleted_for_receiver=True)
    for model, fields in retained:
        for field in fields:
            model.objects.filter(**{field: user}).update(**{field: retired})
    user.delete()
    return True
