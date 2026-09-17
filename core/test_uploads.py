from io import BytesIO
from unittest.mock import patch, MagicMock
from urllib.parse import urlsplit, parse_qs

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from PIL import Image
from rest_framework.exceptions import ValidationError

from core.media_storage import ProtectedMediaStorage
from core.uploads import validate_image, validate_video
from swifts.testing import fake_video
from swifts.serializers import SwiftCreateSerializer


def picture(fmt='PNG'):
    output = BytesIO()
    Image.new('RGB', (3, 3), 'blue').save(output, format=fmt)
    return output.getvalue()


class UploadValidationTests(SimpleTestCase):
    def test_images_use_actual_format_and_remove_trailing_active_content(self):
        for fmt, mime, suffix in [('PNG', 'image/png', '.png'), ('JPEG', 'image/jpeg', '.jpg'), ('WEBP', 'image/webp', '.webp')]:
            clean = validate_image(SimpleUploadedFile('payload.html', picture(fmt) + b'<script>evil()</script>', content_type='text/html'))
            self.assertEqual(clean.content_type, mime)
            self.assertTrue(clean.name.endswith(suffix))
            self.assertNotIn(b'<script>', clean.read())

    def test_invalid_svg_html_truncated_and_empty_images_are_rejected(self):
        for content in (b'', b'<svg/>', b'<html>active</html>', b'GIF89a', picture()[:20]):
            with self.subTest(content=content[:8]), self.assertRaises(ValidationError):
                validate_image(SimpleUploadedFile('photo.jpg', content, content_type='image/jpeg'))

    def test_byte_and_pixel_limits_are_enforced(self):
        with patch('core.uploads.MAX_IMAGE_BYTES', 10), self.assertRaises(ValidationError):
            validate_image(SimpleUploadedFile('photo.png', picture()))
        with patch('core.uploads.MAX_IMAGE_PIXELS', 4), self.assertRaises(ValidationError):
            validate_image(SimpleUploadedFile('photo.png', picture()))

    def test_exif_is_stripped_and_animated_images_are_refused(self):
        image = Image.new('RGB', (2, 2), 'red')
        exif = Image.Exif(); exif[270] = 'private location metadata'
        output = BytesIO(); image.save(output, 'JPEG', exif=exif)
        clean = validate_image(SimpleUploadedFile('photo.jpg', output.getvalue()))
        self.assertNotIn(b'private location metadata', clean.read())
        output = BytesIO()
        image.save(output, 'GIF', save_all=True, append_images=[Image.new('RGB', (2, 2), 'blue')])
        with self.assertRaises(ValidationError):
            validate_image(SimpleUploadedFile('animated.gif', output.getvalue()))

    def test_video_metadata_is_server_controlled(self):
        for brand, mime, suffix in [(b'isom', 'video/mp4', '.mp4'), (b'qt  ', 'video/quicktime', '.mov')]:
            raw = fake_video().read().replace(b'isom', brand)
            upload = SimpleUploadedFile('malicious.html', raw, content_type='text/html')
            self.assertIs(validate_video(upload), upload)
            self.assertTrue(upload.name.endswith(suffix))
            self.assertEqual(upload.content_type, mime)
            self.assertEqual(upload.tell(), 0)

    def test_video_rejects_non_video_container_damage_and_size_overflow(self):
        raw = fake_video().read()
        for content in (b'<html>evil</html>', raw[:-2], raw.replace(b'vide', b'soun'), raw.replace(b'ftyp', b'xxxx'), raw + b'garbage'):
            with self.subTest(length=len(content)), self.assertRaises(ValidationError):
                validate_video(SimpleUploadedFile('video.mp4', content, content_type='video/mp4'))
        with patch('swifts.media.MAX_UPLOAD_BYTES', 8), self.assertRaises(ValidationError):
            validate_video(fake_video())

    def test_swift_serializer_validates_both_upload_fields(self):
        serializer = SwiftCreateSerializer(data={
            'video': fake_video(), 'thumbnail': SimpleUploadedFile('fake.png', b'<svg/>'),
            'duration_ms': 5000,
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('thumbnail', serializer.errors)

    def test_storage_never_writes_new_files_to_public_backend(self):
        public, private = MagicMock(), MagicMock()
        private.exists.return_value = False
        private.save.side_effect = lambda name, content: name
        with patch('core.media_storage.storages', {'default': public, 'private_media': private}):
            storage = ProtectedMediaStorage()
            name = storage.save('chat_images/photo.png', SimpleUploadedFile('photo.png', picture()))
            self.assertTrue(name.startswith('private/'))
            public.save.assert_not_called()
            storage.url(name); private.url.assert_called_once_with(name)
            storage.url('chat_images/legacy.png'); public.url.assert_called_once_with('chat_images/legacy.png')

    def test_private_r2_url_is_signed_and_uses_private_bucket(self):
        from storages.backends.s3 import S3Storage
        storage = S3Storage(access_key='test-access', secret_key='test-secret', bucket_name='private-test',
                            endpoint_url='https://storage.example.test', region_name='auto',
                            custom_domain=None, default_acl=None, querystring_auth=True, querystring_expire=300)
        url = urlsplit(storage.url('private/test.mp4'))
        self.assertIn('private-test', url.path)
        query = parse_qs(url.query)
        self.assertIn('X-Amz-Signature', query)
        self.assertEqual(query['X-Amz-Expires'], ['300'])
