"""The one door reports and screening go through.

Views call `file_report()` and `screen()`. Nothing else. That is the whole point
of the module: the alternative - each view knowing how to look up an author,
snapshot the text, dedupe a repeat press and decide whether enough reports have
arrived to hide something - is five copies of a rule that will not stay in step.

Two rules the callers rely on:

  * `file_report` is **idempotent**. One reporter is one report on one target,
    however many times they press it. It returns `(report, created)` and never
    raises on a repeat.
  * Filing a report never removes anything. It flags, and at a threshold it
    quarantines. Removal is a human setting `Moderation.REMOVED` and only that -
    the argument is on `swifts.SwiftReport`, and it does not change because the
    content is a story instead of a video.
"""
from django.db import IntegrityError, transaction
from django.db.models import Count

from .models import ContentReport, ModerationAction
from .reasons import AUTO_QUARANTINE_REPORTS, Action, Moderation, Reason, State
from . import text as text_filter


# ─── Resolving a target ───────────────────────────────────────────────────────
#: How to get from (target_type, id) to the row, who is answerable for it, and
#: what it said. One table instead of a chain of `if` in every view.
#:
#: Each entry is (import path, author attribute, text attributes). The text
#: attributes are tried in order and the first non-empty one wins, so a story
#: with no caption falls back to naming its place rather than snapshotting ''.
_TARGETS = {
    ContentReport.Target.USER: ('users.models.User', None, ('username', 'bio')),
    ContentReport.Target.STORY: ('stories.models.Story', 'user', ('content', 'place_name')),
    ContentReport.Target.COMMENT: ('swifts.models.SwiftComment', 'author', ('text',)),
    ContentReport.Target.MESSAGE: ('friends.models.Message', 'sender', ('content',)),
    ContentReport.Target.CLUB_MESSAGE: ('clubs.models.ClubMessage', 'sender', ('content',)),
    # The owner's row: the track is a field on it. See `User.zone_track_report_snapshot`.
    ContentReport.Target.ZONE_AUDIO: ('users.models.User', None, ('zone_track_report_snapshot',)),
}

#: The target types whose rows carry a `moderation` column, and can therefore be
#: quarantined rather than only reported. A direct message is not on this list
#: on purpose: it has exactly one reader, who already has block and delete, and
#: a server that hides half a private conversation from the person it was sent
#: to has invented a problem rather than solved one.
_MODERATABLE = frozenset({
    ContentReport.Target.STORY,
    ContentReport.Target.COMMENT,
    ContentReport.Target.CLUB_MESSAGE,
})


def _model_for(target_type):
    from django.apps import apps

    path = _TARGETS[target_type][0]
    module, _, name = path.rpartition('.')
    app_label = module.split('.')[0]
    return apps.get_model(app_label, name)


def resolve_target(target_type, target_id):
    """`(instance, author, snapshot)` for a reportable row, or `(None, None, '')`.

    Never raises on a missing row. A report filed against something that has
    since been deleted is still a report worth keeping - it is often the only
    surviving description of what was there.
    """
    if target_type not in _TARGETS:
        return None, None, ''
    _, author_attr, text_attrs = _TARGETS[target_type]
    model = _model_for(target_type)
    instance = model.objects.filter(pk=target_id).first()
    if instance is None:
        return None, None, ''

    author = instance if author_attr is None else getattr(instance, author_attr, None)

    snapshot = ''
    for attr in text_attrs:
        value = (getattr(instance, attr, '') or '').strip()
        if value:
            snapshot = value[:2000]
            break
    return instance, author, snapshot


# ─── Filing a report ──────────────────────────────────────────────────────────
def file_report(reporter, target_type, target_id, *, reason=Reason.OTHER, note='',
                source=ContentReport.Source.REPORT):
    """Record that `reporter` objected to something. Returns `(report, created)`.

    Idempotent per (reporter, target, type). A repeat press returns the existing
    row untouched rather than updating it - the first reason somebody gave is
    the one they gave, and letting a second press rewrite it would let a
    reporter launder a weak report into an urgent one.

    A block that raises a report lands here with `source=BLOCK`, which is what
    keeps it distinguishable in the queue from somebody who actually chose a
    category.
    """
    if reporter is None or target_type not in _TARGETS:
        return None, False

    instance, author, snapshot = resolve_target(target_type, target_id)

    # Reporting yourself is not a thing. Silently a no-op rather than an error:
    # the only way to reach it is a client bug, and a 400 in the middle of a
    # block flow would surface as "could not block" to somebody who needs it to
    # work.
    if author is not None and getattr(author, 'pk', None) == reporter.pk:
        return None, False

    try:
        with transaction.atomic():
            report, created = ContentReport.objects.get_or_create(
                reporter=reporter,
                target_type=target_type,
                target_id=target_id,
                defaults={
                    'reason': reason,
                    'note': (note or '')[:500],
                    'source': source,
                    'target_user': author,
                    'target_repr': snapshot,
                },
            )
    except IntegrityError:
        report = ContentReport.objects.filter(
            reporter=reporter, target_type=target_type, target_id=target_id,
        ).first()
        created = False

    if created and instance is not None:
        _flag(instance, target_type)
        _maybe_quarantine(instance, target_type, target_id)

    return report, created


def _flag(instance, target_type):
    """Mark content as awaiting review, without hiding it.

    Never overwrites a verdict a moderator has already reached: a REMOVED row
    that gets reported again must not quietly come back to FLAGGED, which is a
    state that is still visible. Same guard, and the same reason, as
    `swifts.views.SwiftReportView`.
    """
    if target_type not in _MODERATABLE:
        return
    type(instance).objects.filter(
        pk=instance.pk, moderation=Moderation.OK,
    ).update(moderation=Moderation.FLAGGED)


def _maybe_quarantine(instance, target_type, target_id):
    """Hide content once enough *distinct* people have independently objected.

    The threshold exists because "flag and wait for a human" is not a timely
    response at three in the morning, and App Review asks for timely. It is
    reversible by a moderator and the author is told, so being wrong costs a
    delay rather than a deletion.

    Distinct reporters is the only count that means anything here - the unique
    constraint on `ContentReport` already guarantees it, and this reads the same
    table rather than a counter that could drift from it.
    """
    if target_type == ContentReport.Target.ZONE_AUDIO:
        _maybe_mute_zone_track(instance, target_id)
        return
    if target_type not in _MODERATABLE:
        return
    open_reports = ContentReport.objects.filter(
        target_type=target_type,
        target_id=target_id,
        source=ContentReport.Source.REPORT,
    ).count()
    if open_reports < AUTO_QUARANTINE_REPORTS:
        return
    updated = type(instance).objects.filter(
        pk=instance.pk, moderation__in=(Moderation.OK, Moderation.FLAGGED),
    ).update(moderation=Moderation.QUARANTINED)
    if updated:
        ModerationAction.objects.create(
            kind=ModerationAction.Kind.CONTENT_REMOVED,
            subject=getattr(instance, 'user', None) or getattr(instance, 'author', None)
            or getattr(instance, 'sender', None),
            detail=(
                f'{target_type} #{target_id} quarantined automatically after '
                f'{open_reports} reports'
            ),
        )


def _maybe_mute_zone_track(owner, target_id):
    """The Zone Music counterpart of quarantine: mute the track for every
    listener once enough distinct people have reported it.

    Counts *open* reports only. The target id is the owner, not the track, so
    reports a moderator has already closed — about this track or an earlier
    one — must not count towards muting whatever is playing now. Same threshold
    and the same reversibility as quarantine: a moderator's Restore unmutes it,
    Remove takes it down.
    """
    open_reports = ContentReport.objects.filter(
        target_type=ContentReport.Target.ZONE_AUDIO,
        target_id=target_id,
        source=ContentReport.Source.REPORT,
        state=State.OPEN,
    ).count()
    if open_reports < AUTO_QUARANTINE_REPORTS:
        return
    updated = type(owner).objects.filter(pk=owner.pk, zone_audio_moderation='ok').update(
        zone_audio_moderation='quarantined')
    if updated:
        ModerationAction.objects.create(
            kind=ModerationAction.Kind.CONTENT_REMOVED,
            subject=owner,
            detail=f'zone_audio #{target_id} muted automatically after {open_reports} reports',
        )


def report_user(reporter, target_user, *, reason=Reason.OTHER, note='',
                source=ContentReport.Source.REPORT):
    """Convenience wrapper for the commonest target."""
    return file_report(
        reporter, ContentReport.Target.USER, target_user.pk,
        reason=reason, note=note, source=source,
    )


# ─── Screening a write ────────────────────────────────────────────────────────
class Blocked(Exception):
    """Raised by `screen()` when text must not be published at all.

    Carries the verdict so a view can turn it into a 400 with the right message
    without re-running the classifier.
    """

    def __init__(self, verdict):
        super().__init__(verdict.message())
        self.verdict = verdict


def screen(*texts, raise_on_block=True):
    """Classify user text before it is stored. Returns the `Verdict`.

    The contract callers depend on:

        verdict.blocked   refuse the write (raised as `Blocked` by default)
        verdict.flagged   store it, but set `moderation=QUARANTINED`
        verdict.allowed   nothing to do

    `raise_on_block=False` is for the two callers that need to decide for
    themselves - a serializer field validator, which must raise DRF's own
    `ValidationError` instead, and the admin.
    """
    verdict = text_filter.classify_many(*texts)
    if verdict.blocked and raise_on_block:
        raise Blocked(verdict)
    return verdict


def moderation_for(verdict):
    """The `moderation` column value a freshly screened row should be saved with.

    A blocked verdict never reaches here - the write was refused - so this only
    has to answer for flagged and allowed.
    """
    return Moderation.QUARANTINED if verdict.flagged else Moderation.OK


def record_automated_flag(instance, target_type, verdict, author=None):
    """File the automated flag as a report, so quarantined content is in the queue.

    Without this, `moderation=QUARANTINED` would be a state nothing lists: the
    admin queue is built from reports, and content held back by the filter has
    no reporter. The report is attributed to the author - there is nobody else -
    and marked `source=AUTOMATED` so it never reads as a person's complaint.
    """
    if author is None or instance is None:
        return None
    report, _ = ContentReport.objects.get_or_create(
        reporter=author,
        target_type=target_type,
        target_id=instance.pk,
        defaults={
            'reason': verdict.reason or Reason.OTHER,
            'note': f'Automated filter: {", ".join(verdict.matched)}'[:500],
            'source': ContentReport.Source.AUTOMATED,
            'target_user': author,
            'target_repr': '',
        },
    )
    return report


# ─── Closing a report ─────────────────────────────────────────────────────────
def resolve_report(report, moderator, *, state, action=Action.NONE, note=''):
    """Close a report and record who closed it, when, and what they did.

    Used by the admin actions and by tests. The three columns are written
    together so a report can never be marked reviewed without a reviewer, which
    is the state an audit cannot interpret.
    """
    from django.utils import timezone

    report.state = state
    report.action_taken = action
    report.moderator_note = (note or '')[:500]
    report.reviewed_at = timezone.now()
    report.reviewed_by = moderator
    report.save(update_fields=[
        'state', 'action_taken', 'moderator_note', 'reviewed_at', 'reviewed_by',
    ])
    if state == State.REVIEWED:
        ModerationAction.objects.create(
            kind=ModerationAction.Kind.REPORT_DISMISSED,
            subject=report.target_user,
            moderator=moderator,
            detail=f'Report #{report.pk} dismissed',
        )
    return report


def open_report_counts(target_type, target_ids):
    """`{target_id: n}` open reports, in one query. For the admin's list view."""
    rows = (
        ContentReport.objects
        .filter(target_type=target_type, target_id__in=target_ids, state=State.OPEN)
        .values_list('target_id')
        .annotate(n=Count('pk'))
    )
    return dict(rows)
