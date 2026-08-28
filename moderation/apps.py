from django.apps import AppConfig


class ModerationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'moderation'
    verbose_name = 'Moderation'

    def ready(self):
        # Registers the startup checks for the Cloudflare configuration. The
        # import has the side effect; there is nothing to call.
        from . import checks  # noqa: F401
