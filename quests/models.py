from django.db import models
from django.conf import settings
from django.core.files.storage import storages


def quest_proof_storage():
    """Resolved via the STORAGES registry so tests can swap in a local
    backend while production uses authenticated Cloudinary delivery."""
    return storages['quest_proofs']


class Quest(models.Model):
    class QuestType(models.TextChoices):
        DAILY = 'daily', 'Daily'
        WEEKLY = 'weekly', 'Weekly'

    class ConditionType(models.TextChoices):
        SEND_HI = 'send_hi', 'Send Hi'
        SEND_MESSAGE = 'send_message', 'Send Message'
        UPDATE_LOCATION = 'update_location', 'Update Location'
        MAKE_FRIEND = 'make_friend', 'Make Friend'
        SELECT_INTERESTS = 'select_interests', 'Select Interests'
        HAVE_ENCOUNTER = 'have_encounter', 'Have Encounter'
        MEET_FRIEND_IN_PERSON = 'meet_friend_in_person', 'Meet Friend in Person'
        PHOTO_PROOF = 'photo_proof', 'Photo Proof'

    title = models.CharField(max_length=120)
    description = models.TextField()
    reward_points = models.PositiveIntegerField()
    quest_type = models.CharField(max_length=10, choices=QuestType.choices)
    condition_type = models.CharField(max_length=25, choices=ConditionType.choices)
    interest = models.ForeignKey(
        'interests.Interest',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='quests',
    )
    # Translation handle: `template_key` identifies the authored string in
    # quests.content and `template_params` carries its substitutions (e.g.
    # {"interest": "Guitar"}), so a future i18n layer can render the quest in
    # any language without re-deriving it from the English title below.
    template_key = models.CharField(max_length=120, blank=True, db_index=True)
    template_params = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            # Keeps quest syncing idempotent — one row per template per
            # interest, re-runnable as new interests appear.
            models.UniqueConstraint(
                fields=['template_key', 'interest'],
                name='unique_quest_template_per_interest',
                condition=~models.Q(template_key=''),
            ),
        ]

    def __str__(self):
        return f'[{self.quest_type}] {self.title}'


class UserQuest(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='user_quests',
    )
    quest = models.ForeignKey(Quest, on_delete=models.CASCADE, related_name='user_quests')
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    reset_date = models.DateField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'quest')

    def __str__(self):
        return f'{self.user} — {self.quest.title} ({"done" if self.completed else "pending"})'


class UserAuraPoints(models.Model):
    """All-time total. This is a denormalised cache of the PointEvent ledger,
    recomputed on every write by quests.ledger.record_points — never bumped
    directly, so it can always be rebuilt from the events."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='aura_points',
    )
    total_points = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f'{self.user}: {self.total_points} pts'


class PointEvent(models.Model):
    """Auditable ledger of every point movement.

    Positive rows award, negative rows claw back — nothing is ever edited or
    deleted, so a user's total is always explainable from their history.

    `period_start` is the Monday of the leaderboard week the points count
    toward, taken from the proof's quest window rather than the review date,
    so moderation lag never moves a player between weeks. It is NULL for
    points that belong to the all-time total but to no leaderboard period
    (legacy pre-ledger balances, and non-photo-verified awards).
    """

    class Reason(models.TextChoices):
        QUEST_APPROVED = 'quest_approved', 'Quest proof approved'
        QUEST_REVOKED = 'quest_revoked', 'Quest approval revoked'
        LEGACY_BALANCE = 'legacy_balance', 'Pre-ledger balance'
        UNVERIFIED_QUEST = 'unverified_quest', 'Unverified quest completion'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='point_events'
    )
    quest = models.ForeignKey(
        Quest, null=True, blank=True, on_delete=models.SET_NULL, related_name='point_events'
    )
    proof = models.ForeignKey(
        'QuestProof', null=True, blank=True, on_delete=models.SET_NULL, related_name='point_events'
    )
    points = models.IntegerField()  # signed
    reason = models.CharField(max_length=20, choices=Reason.choices)
    period_start = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            # Drives the weekly leaderboard: scans one week, not every user.
            models.Index(fields=['period_start', 'user']),
            models.Index(fields=['user']),
        ]

    def __str__(self):
        return f'{self.user}: {self.points:+d} ({self.reason})'


class QuestProof(models.Model):
    """A camera photo submitted as evidence that a quest was completed.

    The client is untrusted: `submitted_at` is the server's own clock, and the
    EXIF fields are extracted server-side from the uploaded bytes (never from
    client-sent metadata). Points are awarded only when a moderator approves —
    and exactly once per proof.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='quest_proofs'
    )
    quest = models.ForeignKey(Quest, on_delete=models.CASCADE, related_name='proofs')
    image = models.ImageField(upload_to='quest_proofs/', storage=quest_proof_storage)

    # Server-side bookkeeping (client clock is never trusted)
    submitted_at = models.DateTimeField(auto_now_add=True)
    window_start = models.DateField()  # quest window the proof applies to

    # EXIF extracted server-side from the uploaded file, for moderators
    exif_capture_time = models.DateTimeField(null=True, blank=True)
    exif_latitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    exif_longitude = models.DecimalField(max_digits=11, decimal_places=8, null=True, blank=True)
    exif_data = models.JSONField(default=dict, blank=True)

    # Device attestation outcome for this submission (see quests.attestation).
    # Recorded even in audit mode: "attestation failed" is real signal for a
    # moderator, whether or not it blocked the upload.
    attestation_status = models.CharField(max_length=12, default='skipped')
    attestation_platform = models.CharField(max_length=10, blank=True)
    attestation_detail = models.CharField(max_length=250, blank=True)

    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='reviewed_quest_proofs',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    class Meta:
        ordering = ['submitted_at']  # moderation queue: oldest first
        constraints = [
            # One in-flight proof per quest per user; retry allowed after a
            # rejection, so the constraint only covers pending rows.
            models.UniqueConstraint(
                fields=['user', 'quest'],
                condition=models.Q(status='pending'),
                name='one_pending_proof_per_user_quest',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'submitted_at']),
        ]

    def __str__(self):
        return f'{self.user} — {self.quest.title} ({self.status})'


class AttestationChallenge(models.Model):
    """A single-use nonce handed to a device before it attests. Burning it on
    use is what stops an attestation token being replayed."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='attestation_challenges'
    )
    nonce = models.CharField(max_length=64, db_index=True)  # base64 of 32 bytes
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['user', 'nonce'])]

    def __str__(self):
        return f'challenge for {self.user} ({"used" if self.used_at else "open"})'


class AttestedDevice(models.Model):
    """An iOS App Attest key registered to a user.

    Apple attests a Secure Enclave key once, then the device signs later
    requests with it. We keep the leaf certificate to check those signatures
    and the signature counter to reject replays.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='attested_devices'
    )
    platform = models.CharField(max_length=10, default='ios')
    key_id = models.CharField(max_length=128)
    certificate = models.TextField()  # base64 DER of the attestation leaf cert
    sign_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'key_id')

    def __str__(self):
        return f'{self.platform} device for {self.user}'
