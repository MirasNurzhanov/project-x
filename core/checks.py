from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def private_media_configuration(app_configs, **kwargs):
    if settings.DEBUG:
        return []
    private = settings.STORAGES.get('private_media', {}).get('OPTIONS', {})
    public = settings.STORAGES.get('default', {}).get('OPTIONS', {})
    if not private.get('bucket_name') or private.get('bucket_name') == public.get('bucket_name'):
        return [Error('Configure a separate R2_PRIVATE_BUCKET_NAME with public access disabled.', id='core.E001')]
    if private.get('custom_domain') or not private.get('querystring_auth'):
        return [Error('Private media must use signed URLs, not a public custom domain.', id='core.E002')]
    return []
