"""
mason_redact.py — one place that strips credentials out of free text before it is stored or logged.

Why this exists (N11, 2026-11): mason_health built its "<source> is DOWN" notification body from the text of a
`requests` exception, and that text carries the full request URL, ``...?symbol=SPY&apikey=<KEY>``. The notification
landed in mason.db, and the search indexer copied it into mason_search.db. Any code that turns an exception, a URL or an
HTTP header into stored text can do the same, so the fix is a shared redactor used at the source (mason_health) and at
the sink (mason_notifications.add_notification).

Public surface
    redact_text(value)   -> str      credentials replaced by "<redacted>"; text with none in it is returned unchanged
    secret_values()      -> list     the live values of the REGISTRY secrets (>= 8 chars), longest first. Never logged.
    REDACTED             -> "<redacted>"

What it removes (a request URL, a repr of the params / headers dict, an exception message, a pasted header)
    1. user:password@ in a URL (the whole userinfo part).
    2. Query / fragment parameters and assignments named like a credential, including percent-encoded names (api%5Fkey):
         strong names, anywhere:  apikey, api_key, api-key, x-api-key, access/auth/refresh/id_token, client_secret, secret,
                                  password, passwd, APCA-API-KEY-ID, APCA-API-SECRET-KEY. ``name=value`` is always
                                  redacted (the value may be quoted); ``name: value`` only when the value looks like a
                                  credential, so prose ("The secret: strong margins") is left alone.
         weak names, only after ? & ; or #:  key, token, sig, signature, auth
    3. Quoted dict / JSON entries: 'apikey': 'v', "Authorization": "Bearer v", 'APCA-API-SECRET-KEY': 'v' ...
    4. HTTP headers in plain text: Authorization / Proxy-Authorization (a scheme word plus a token, or a credential-shaped
       value), Cookie / Set-Cookie (name=value). Headline text such as "Emergency Use Authorization: granted" is left alone.
    5. Defence in depth: the live VALUE of every mason_secrets.REGISTRY credential (and its percent-encoded form), matched
       case-insensitively, wherever it appears, even in a shape the patterns above do not know.
Every pattern is linear on long input (bounded scheme / value lengths); a 500 KB unbroken run takes milliseconds.
If the redactor itself fails, or the REGISTRY values cannot be read (so step 5 could not run), it returns a fixed
placeholder instead of the original text (fail closed).
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote

REDACTED = "<redacted>"
_WITHHELD = "[text withheld: redaction error]"
_MIN_SECRET_LEN = 8          # a shorter value would mangle ordinary text and is not a real credential

# REGISTRY entries whose value is a credential. SEC_USER_AGENT (a contact string), ROBINHOOD_TOKEN_FILE (a path) and
# TWILIO_PHONE_NUMBER are not, and redacting them would only damage ordinary text.
_SECRET_NAME_RE = re.compile(r"(_KEY|_KEY_ID|_SECRET_KEY|_AUTH_TOKEN|_ACCOUNT_SID)$")

# Separator inside a credential name, including its percent-encoded forms (api%5Fkey, api%2Dkey).
_SEP = r"(?:_|\-|%5[Ff]|%2[Dd])"
_STRONG = (r"api" + _SEP + r"?key|x" + _SEP + r"api" + _SEP + r"key|access" + _SEP + r"token|auth" + _SEP + r"token|"
           r"refresh" + _SEP + r"token|id" + _SEP + r"token|client" + _SEP + r"secret|secret|password|passwd|"
           r"apca" + _SEP + r"api" + _SEP + r"key" + _SEP + r"id|apca" + _SEP + r"api" + _SEP + r"secret" + _SEP + r"key")
_WEAK = r"key|token|sig|signature|auth"
_HEADERS = (r"proxy" + _SEP + r"authorization|authorization|x" + _SEP + r"api" + _SEP + r"key|apca" + _SEP + r"api" + _SEP +
            r"key" + _SEP + r"id|apca" + _SEP + r"api" + _SEP + r"secret" + _SEP + r"key|x" + _SEP + r"auth" + _SEP +
            r"token|set" + _SEP + r"cookie|cookie")

# Every pattern below is linear on long unbroken input: scheme and value lengths are bounded, anchors stop a match
# from restarting inside a long run, and a quoted value cannot contain its own quote character. (N11 QA R1: the first
# version of the userinfo and quoted patterns were quadratic and ran on every notification.)

# 1. scheme://user:pass@host  (the userinfo may be a bare token)
_RE_USERINFO = re.compile(
    r"(?<![A-Za-z0-9+.\-])(?P<pre>[A-Za-z][A-Za-z0-9+.\-]{0,31}://)(?P<ui>[^\s/?#@\"'<>]{1,512})@")

# 3. 'name': 'value' / "name": "value" / 'name'='value'   (keeps the name and the quotes)
_RE_QUOTED = re.compile(
    r"(?P<k>(?P<q>['\"])(?:" + _STRONG + "|" + _HEADERS + r")(?P=q)\s{0,8}[:=]\s{0,8})"
    r"(?:'(?P<v1>[^']{0,512})'|\"(?P<v2>[^\"]{0,512})\")", re.I)

# 4. Headers in plain text. Authorization: a scheme word + token (Bearer abc ...), or an opaque token-shaped value
#    (see _opaque); Cookie / Set-Cookie: name=value. Ordinary headline text ("Emergency Use Authorization: granted",
#    "Cookie: stock rises") is left alone. X-Api-Key / APCA-* / X-Auth-Token are covered by the strong names (2a).
_SCHEME = r"(?:bearer|basic|token|digest|negotiate)\s{1,8}[^\s,;\"'<>]{1,2048}"
_RE_AUTH = re.compile(
    r"(?P<k>(?<![\w\-])(?:proxy" + _SEP + r")?authorization\s{0,8}[:=]\s{0,8})(?P<v>" + _SCHEME + r"|[^\s,;\"'<>]{1,2048})",
    re.I)
_RE_COOKIE = re.compile(
    r"(?P<k>(?<![\w\-])(?:set" + _SEP + r")?cookie\s{0,8}[:=]\s{0,8})(?P<v>[\w.\-]{1,128}=[^\s;,\"'<>]{1,2048})", re.I)

# 2a. strong names: name=value always; name: value only when the value looks like a credential (so prose such as
#     "The secret: strong margins" or "Password: reset required" is left alone).
_RE_STRONG_EQ = re.compile(
    r"(?P<k>(?<![\w\-])(?:" + _STRONG + r")\s{0,8}=\s{0,8}['\"]?)(?P<v>[^&\s#\"'<>]+)", re.I)
_RE_STRONG_COLON = re.compile(
    r"(?P<k>(?<![\w\-])(?:" + _STRONG + r")\s{0,8}:\s{0,8}['\"]?)(?P<v>[^&\s#\"'<>]+)", re.I)

# 2b. weak names, only as a query / fragment parameter (after ? & ; or #)
_RE_WEAK = re.compile(r"(?P<k>[?&;#](?:" + _WEAK + r")=)(?P<v>[^&\s#\"'<>]*)", re.I)


def _opaque(v: str) -> bool:
    """True when ``v`` is shaped like a credential, not like words: 8+ chars with a letter and a digit, or 24+ chars,
    and not a hyphenated phrase of ordinary words ('BLA-approval-for-Keytruda-sc-2026')."""
    v = v.rstrip(".,;)")
    if len(v) < 8:
        return False
    segs = [x for x in re.split(r"[-_.]", v) if x]
    if any(len(x) >= 16 and re.search(r"\d", x) and re.search(r"[A-Za-z]", x) for x in segs):
        return True
    if len([x for x in segs if x.isalpha() and len(x) >= 3]) >= 2:
        return False
    return len(v) >= 24 or bool(re.search(r"\d", v) and re.search(r"[A-Za-z]", v))


def _sub_userinfo(m):
    return m.group("pre") + REDACTED + "@" if m.group("ui") != REDACTED else m.group(0)


def _sub_quoted(m):
    v = m.group("v1") if m.group("v1") is not None else m.group("v2")
    if v == REDACTED:
        return m.group(0)
    q = "'" if m.group("v1") is not None else '"'
    return m.group("k") + q + REDACTED + q


def _sub_value(m):
    if not m.group("v"):
        return m.group(0)
    return m.group("k") + REDACTED


def _sub_opaque(m):
    v = m.group("v")
    if re.match(_SCHEME, v, re.I) or _opaque(v):
        return m.group("k") + REDACTED
    return m.group(0)


def _sub_colon(m):
    return m.group("k") + REDACTED if _opaque(m.group("v")) else m.group(0)


def secret_values(strict: bool = False) -> list:
    """The live values of the credential-type REGISTRY secrets (env first, then whatever mason_secrets resolves),
    >= 8 chars, longest first, plus their percent-encoded forms. Never logs a value. With strict=True a failure to read
    the secrets raises (redact_text uses that, so it can fail closed); otherwise it is swallowed and the list is short."""
    out = set()
    try:
        import mason_secrets as S
        for name, spec in S.REGISTRY.items():
            if not _SECRET_NAME_RE.search(name):
                continue
            cands = []
            try:
                cands.append(S.get(name))
            except Exception:
                if strict:
                    raise
            for e in spec.get("env", ()):
                cands.append(os.environ.get(e))
            for v in cands:
                if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN and v != REDACTED:
                    out.add(v)
                    q = quote(v, safe="")
                    if q != v:
                        out.add(q)
    except Exception:
        if strict:
            raise
    return sorted(out, key=lambda s: (-len(s), s))


def redact_text(value) -> str:
    """``value`` with credentials replaced by ``<redacted>``. A string with no credential in it comes back unchanged
    (same text). Non-strings are str()'d. Never raises. Fails closed: if the text cannot be processed, or the secrets
    cannot be read (so the value layer could not run), the text is withheld instead of returned."""
    try:
        s = value if isinstance(value, str) else str(value)
        low = None
        for v in secret_values(strict=True):
            if low is None:
                low = s.lower()
            if v.lower() in low:                              # case-insensitive: a key may be upper/lower-cased
                s = re.sub(re.escape(v), REDACTED, s, flags=re.I)
                low = s.lower()
        # Cheap substring tests first: most text (and all long text without these words) skips the regexes entirely.
        low = s.lower()
        if "://" in s:
            s = _RE_USERINFO.sub(_sub_userinfo, s)
        has_name = any(w in low for w in ("key", "token", "secret", "pass", "authorization", "cookie", "sig", "auth"))
        if has_name:
            if "'" in s or '"' in s:
                s = _RE_QUOTED.sub(_sub_quoted, s)
            if "authorization" in low:
                s = _RE_AUTH.sub(_sub_opaque, s)
            if "cookie" in low:
                s = _RE_COOKIE.sub(_sub_value, s)
            s = _RE_STRONG_EQ.sub(_sub_value, s)
            s = _RE_STRONG_COLON.sub(_sub_colon, s)
            if "?" in s or "&" in s or ";" in s or "#" in s:
                s = _RE_WEAK.sub(_sub_value, s)
        return s
    except Exception:
        return _WITHHELD
