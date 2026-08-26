"""JWT authentication that a suspension can actually stop.

Django's own `is_active` is checked at *login* and nowhere else, so flipping it
leaves every access token already in the wild working until it expires - up to
an hour of a banned account carrying on as normal. For an App Review requirement
that says suspended accounts must not be able to keep operating, "in about an
hour" is not an answer.

This is the central chokepoint instead. `DEFAULT_AUTHENTICATION_CLASSES` names
it, every DRF view goes through it, and it re-reads the suspension columns on
every request - so the moment a moderator presses Ban in the admin, the next
request from that phone is a 401 carrying the reason.

The cost is one extra pair of columns on a query the request was already making
(simplejwt loads the user row regardless), so this adds no queries at all.
"""
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed


class SuspensionAwareJWTAuthentication(JWTAuthentication):
    """`JWTAuthentication`, plus: a suspended account is not authenticated.

    Answers 401 rather than 403 deliberately. The app's api layer already
    treats a 401 as "this session is over" and tears down to the sign-in screen
    (see services/api.ts), which is exactly the right handling for a ban - and
    a 403 would leave a suspended user sitting in a signed-in app where every
    screen failed one at a time.
    """

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        detail = user.suspension_detail()
        if detail is not None:
            # Strings only. DRF coerces every value in an exception payload to
            # `ErrorDetail`, which is a `str` subclass — so a boolean sent
            # through here arrives as `"False"`, which a client reads as true.
            # The login path avoids that by rendering its own Response (see
            # `users.views._SuspendedAccount`); an authentication class has no
            # such opportunity, so it simply does not send booleans.
            raise AuthenticationFailed(
                {
                    'detail': detail['detail'],
                    'code': detail['code'],
                    'reason': detail['reason'],
                    'until': detail['until'] or '',
                },
                code=detail['code'],
            )
        return user
