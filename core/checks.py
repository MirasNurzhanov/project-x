"""Configuration that must be right before the process serves anything.

These run as ordinary system checks -- deliberately *not* `deploy=True`. A
deploy-only check answers `manage.py check --deploy` and nothing else, so a
misconfigured private bucket used to boot cleanly and fail later, one publish at
a time, with a 500 and an empty body. `core.apps.CoreConfig.ready` calls the
same function so that uvicorn, which never runs the check framework, refuses to
start on the same conditions.
"""
from django.conf import settings
from django.core.checks import Error, Tags, register

#: Every option a private upload needs before botocore will attempt a request.
#: `bucket_name` is checked separately, because "blank" and "same as the public
#: bucket" are different mistakes with different fixes.
REQUIRED_OPTIONS = ('access_key', 'secret_key', 'endpoint_url')


def _is_r2(entry):
    """Whether a STORAGES entry is actually backed by S3/R2.

    The gate exists because `DEBUG` cannot carry this on its own: Django's test
    runner forces `settings.DEBUG` to False, so a suite running on local
    filesystem storage would otherwise fail every one of these checks about an
    R2 account it is deliberately not using. Production pins the S3 backend in
    settings, not from the environment, so nothing an operator sets can slip a
    real deployment past this.
    """
    return 's3' in entry.get('BACKEND', '').lower()


def private_media_problems(storages, debug=False):
    """Every configuration fault in `storages`, as (id, message) pairs.

    A plain function over a plain dict: it takes the storages mapping rather
    than reading settings, so both callers share one implementation and the
    tests can pose a configuration without `override_settings` or a live app
    registry.

    Returns *all* faults rather than the first. An operator setting Railway
    variables fixes them in one pass; a check that stops at the first sends
    them round the loop again for the second.
    """
    if debug:
        return []

    private_entry = storages.get('private_media', {})
    public_entry = storages.get('default', {})
    if not _is_r2(private_entry):
        return []

    private = private_entry.get('OPTIONS', {})
    public = public_entry.get('OPTIONS', {})
    problems = []

    private_bucket = (private.get('bucket_name') or '').strip()
    public_bucket = (public.get('bucket_name') or '').strip()

    if not private_bucket:
        problems.append((
            'core.E001',
            'R2_PRIVATE_BUCKET_NAME is missing or blank. Private uploads (Swift '
            'video and posters, chat and club images) have nowhere to go, and '
            'every publish fails with a 500 before any request is sent.',
        ))
    elif private_bucket == public_bucket:
        problems.append((
            'core.E002',
            'R2_PRIVATE_BUCKET_NAME is the same bucket as R2_BUCKET_NAME. '
            'Private media would be readable over the public domain. Create a '
            'separate bucket with public access disabled.',
        ))

    # Signed URLs are the whole mechanism: a custom domain serves objects
    # straight off a public hostname, and `querystring_auth=False` drops the
    # signature from the URL. Either one silently republishes private media.
    if private.get('custom_domain') or not private.get('querystring_auth'):
        problems.append((
            'core.E003',
            'Private media must be served by signed URLs: leave custom_domain '
            'unset and querystring_auth on for the private_media storage.',
        ))

    # Names of the missing options only -- never their values, which are keys.
    for purpose, entry, options in (
        ('private_media', private_entry, private), ('default', public_entry, public),
    ):
        if not _is_r2(entry):
            continue
        missing = [name for name in REQUIRED_OPTIONS if not options.get(name)]
        # The private bucket has E001 and E002 of its own; reporting it here too
        # would hand an operator the same fault twice under two different ids.
        if purpose == 'default' and not options.get('bucket_name'):
            missing.append('bucket_name')
        if missing:
            problems.append((
                'core.E004',
                f'The {purpose} storage is missing required R2 configuration: '
                f'{", ".join(missing)}.',
            ))

    return problems


@register(Tags.security)
def private_media_configuration(app_configs, **kwargs):
    return [
        Error(message, id=identifier)
        for identifier, message in private_media_problems(settings.STORAGES, settings.DEBUG)
    ]
