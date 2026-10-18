"""Identifiers shared by the auth and storage layers.

Kept apart so storage can name the owner of its rows without importing the
authentication machinery, which in turn needs storage.
"""

# The first account. Everything stored before accounts existed belongs to it,
# and the setup page turns it into the owner's named account.
LOCAL_USER_ID = "local-user"
SESSION_COOKIE = "cubicle_session"
