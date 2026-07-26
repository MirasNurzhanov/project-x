import os

from cloudinary import uploader, utils
from cloudinary_storage.storage import MediaCloudinaryStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class QuestProofCloudinaryStorage(MediaCloudinaryStorage):
    """Cloudinary storage using the `authenticated` delivery type.

    Proof photos must not be publicly guessable: with this type Cloudinary
    refuses plain public URLs, and every access goes through a signed URL
    generated server-side (`.url` below). Only the moderation API — which is
    permission-gated — hands those URLs out.

    Note: `sign_url=True` alone yields a signature that cannot be forged
    without the API secret but does NOT expire. For time-limited links,
    configure CLOUDINARY auth_token (token-based auth) in the dashboard;
    cloudinary_url() picks it up automatically once set.
    """

    DELIVERY_TYPE = 'authenticated'

    def _upload(self, name, content):
        options = {
            'use_filename': True,
            'resource_type': self._get_resource_type(name),
            'tags': self.TAG,
            'type': self.DELIVERY_TYPE,
        }
        folder = os.path.dirname(name)
        if folder:
            options['folder'] = folder
        return uploader.upload(content, **options)

    def _get_url(self, name):
        name = self._prepend_prefix(name)
        url, _ = utils.cloudinary_url(
            name,
            resource_type=self._get_resource_type(name),
            type=self.DELIVERY_TYPE,
            sign_url=True,
        )
        return url

    def delete(self, name):
        response = uploader.destroy(
            name,
            invalidate=True,
            resource_type=self._get_resource_type(name),
            type=self.DELIVERY_TYPE,
        )
        return response['result'] == 'ok'
