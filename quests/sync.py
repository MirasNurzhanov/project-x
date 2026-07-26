"""Materialise the quest catalogue into Quest rows.

Called by the seeding migration and by the `sync_quests` management command,
so interests added later (the 305-interest seed, user-created "Other"
entries) get their quests without another migration.

Written against `apps.get_model` where possible so the migration can reuse it.
"""
from .content import daily_quest_specs, weekly_quest_specs


def sync_quests(Quest, Interest, quest_type_daily='daily', quest_type_weekly='weekly',
                condition_photo='photo_proof'):
    """Create any missing quests. Returns (daily_created, weekly_created).

    Idempotent: existing rows are matched on (template_key, interest) and
    left alone, so re-running never duplicates or overwrites edited text.
    """
    existing = set(
        Quest.objects.exclude(template_key='').values_list('template_key', 'interest_id')
    )

    to_create = []

    for interest in Interest.objects.all().iterator():
        for spec in daily_quest_specs(interest.name):
            if (spec['template_key'], interest.id) in existing:
                continue
            to_create.append(Quest(
                title=spec['title'],
                description=spec['description'],
                reward_points=spec['reward_points'],
                quest_type=quest_type_daily,
                condition_type=condition_photo,
                interest=interest,
                template_key=spec['template_key'],
                template_params=spec['template_params'],
            ))

    daily_count = len(to_create)

    for spec in weekly_quest_specs():
        if (spec['template_key'], None) in existing:
            continue
        to_create.append(Quest(
            title=spec['title'],
            description=spec['description'],
            reward_points=spec['reward_points'],
            quest_type=quest_type_weekly,
            condition_type=condition_photo,
            interest=None,
            template_key=spec['template_key'],
            template_params=spec['template_params'],
        ))

    Quest.objects.bulk_create(to_create, batch_size=500)
    return daily_count, len(to_create) - daily_count
