"""Who is in for a meetup, as the club sees it.

Members other than the proposer answer "I'm in" or "Nope"; the card in the
club chat shows how many of each and a few faces, and opens the full list.
The proposer is not asked — proposing it is their answer.

Once it is on the map, members tap "I'm here" when they arrive, and the map
shows arrivals against everyone in — see `ClubEventArrivalView`.
"""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import ClubEventResponse

#: Faces on the card. Enough to recognise a friend, few enough to fit.
PREVIEW = 3


def _people(rows):
    from .serializers import BasicUserSerializer

    return [dict(BasicUserSerializer(row.user).data) for row in rows]


def summary(event, viewer=None):
    """Counts, a few faces ("in" first), and the viewer's own answer.

    Reads `event.responses.all()`, so a page that prefetched them — see
    `ClubMessageListView` — costs nothing per card.
    """
    rows = [row for row in event.responses.all() if row.user.is_active]
    going = [row for row in rows if row.answer == ClubEventResponse.Answer.IN]
    out = [row for row in rows if row.answer == ClubEventResponse.Answer.OUT]
    mine = next((row for row in rows if viewer is not None and row.user_id == viewer.pk), None)
    return {
        'in_count': len(going),
        'out_count': len(out),
        # Of those in, how many are standing there — the map's "3/9".
        'here_count': sum(1 for row in going if row.arrived_at),
        'preview': _people((going + out)[:PREVIEW]),
        'my_answer': mine.answer if mine else None,
        'my_arrived': bool(mine and mine.arrived_at),
    }


def full_list(event):
    rows = [
        row for row in event.responses.select_related('user').order_by('-updated_at')
        if row.user.is_active
    ]
    return {
        'in': _people([row for row in rows if row.answer == ClubEventResponse.Answer.IN]),
        'out': _people([row for row in rows if row.answer == ClubEventResponse.Answer.OUT]),
    }


def broadcast(event):
    """Tell the open club chats. Without `my_answer`: that is per reader."""
    data = summary(event)
    data.pop('my_answer')
    data.pop('my_arrived')
    async_to_sync(get_channel_layer().group_send)(
        f'club_{event.club_id}',
        {'type': 'club.event_responses', 'event_id': event.pk, 'responses': data},
    )
