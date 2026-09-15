"""Redact bearer credentials even in ASGI server-generated access/error logs."""
import logging
import re
import traceback

_QUERY = re.compile(r'([?&](?:ticket|token|access_token|refresh_token)=)[^&\s\"\']*', re.I)
_BEARER = re.compile(r'\bBearer\s+[^\s\"\',]+', re.I)
_JWT = re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+')
_PUSH = re.compile(r'(?:Expo|Exponent)PushToken\[[^\]]*\]')
_URL_QUERY = re.compile(r'(https?://[^\s?\"\'<>]+)\?[^\s\"\'<>]*', re.I)


def redact(value):
    if isinstance(value, str):
        value = _URL_QUERY.sub(r'\1?[redacted]', value)
        return _PUSH.sub('[redacted]', _JWT.sub('[redacted]', _BEARER.sub('Bearer [redacted]', _QUERY.sub(r'\1[redacted]', value))))
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, dict):
        return {key: '[redacted]' if str(key).lower() in (
            'password', 'token', 'access', 'refresh', 'access_token', 'refresh_token',
            'authorization', 'push_token', 'secret', 'latitude', 'longitude', 'body',
        ) else redact(item) for key, item in value.items()}
    return value


class CredentialRedactionFilter(logging.Filter):
    def filter(self, record):
        # Preserve uvicorn's five-argument access record shape for formatters.
        record.msg = redact(record.msg)
        record.args = redact(record.args)
        if record.name.startswith('uvicorn') and isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if isinstance(args[2], str) and '?' in args[2]:
                args[2] = args[2].split('?', 1)[0] + '?[redacted]'
            record.args = tuple(args)
        if record.exc_info:
            record.exc_text = redact(''.join(traceback.format_exception(*record.exc_info)))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True
