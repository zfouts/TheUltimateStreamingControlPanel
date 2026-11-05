"""The one definition of what a Twitch login and user ID look like (always used with fullmatch)."""
import re

LOGIN_RE = re.compile(r"[a-z0-9_]{1,25}")      # a login as Twitch sends it: lower case
USER_ID_RE = re.compile(r"[0-9]{1,20}")        # a numeric user ID
