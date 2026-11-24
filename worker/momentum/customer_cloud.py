"""GET-only Supabase authentication and owner-bound settings transport.

User identity comes from the project's Auth server, never decoded JWT claims.
Keys are supplied server-side. No token, raw response or upstream error is logged.
"""
import base64
from datetime import datetime, timezone
import json
import re
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener

from .alpaca import NoRedirect
from .customer_settings import SettingsBindingError, validate_saved_settings

MAX_RESPONSE = 65536
TIMEOUT = 3.0
_JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")
_PROJECT = re.compile(r"https://[a-z0-9][a-z0-9-]{0,62}\.supabase\.co\Z")
_COLUMNS = "owner_id,version,request_id,configuration,updated_at"


class CustomerCloudError(RuntimeError):
    def __init__(self, code="customer_service_unavailable", status=503):
        self.code, self.status = code, status
        super().__init__(code)


def canonical_uuid(value):
    try:
        if not isinstance(value, str) or len(value) != 36 or str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid_identity") from None


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("invalid_json_number")


def strict_json(body):
    return json.loads(body.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)


def _key_kind(key, kind):
    if not isinstance(key, str) or not 1 <= len(key) <= 4096:
        return False
    prefix = "sb_publishable_" if kind == "anon" else "sb_secret_"
    if re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{1,1024}", key):
        return True
    if not _JWT.fullmatch(key):
        return False
    try:
        part = key.split(".")[1]
        claims = strict_json(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        # This only recognizes a configured API-key type. Auth verifies users.
        return isinstance(claims, dict) and claims.get("role") == kind
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return False


class SupabaseCustomerCloud:
    """One configured project and owner; no browser-selected endpoints/accounts."""
    def __init__(self, *, project_url, publishable_key, server_key, owner_id,
                 transport=None, clock=None, monotonic=None):
        if not isinstance(project_url, str) or not _PROJECT.fullmatch(project_url):
            raise ValueError("invalid_customer_project_url")
        if not _key_kind(publishable_key, "anon") or not _key_kind(server_key, "service_role"):
            raise ValueError("invalid_customer_api_key_types")
        self.owner_id = canonical_uuid(owner_id)
        self._url, self._public, self._secret = project_url, publishable_key, server_key
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self._transport = transport or self._http_get

    @staticmethod
    def _http_get(url, headers):
        # No ambient proxy or redirects can redirect credentials to another host.
        request = Request(url, headers=headers, method="GET")
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=TIMEOUT) as response:
                return response.status, dict(response.headers), response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            # Do not read or propagate an upstream error body.
            status = exc.code
            exc.close()
            return status, {}, b""

    def _get(self, path, headers, *, authenticating=False):
        started = self.monotonic()
        try:
            status, response_headers, body = self._transport(self._url + path,
                {**headers, "Accept": "application/json", "Accept-Encoding": "identity"})
            if not 0 <= self.monotonic() - started <= TIMEOUT:
                raise ValueError
            if authenticating and status in {401, 403}:
                raise CustomerCloudError("unauthorized", 401)
            if type(status) is not int or status != 200:
                raise ValueError
            if not isinstance(body, bytes) or len(body) > MAX_RESPONSE:
                raise ValueError
            hdr = {str(k).lower(): v for k, v in response_headers.items()}
            if hdr.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                raise ValueError
            if hdr.get("content-encoding", "identity").lower() != "identity":
                raise ValueError
            if "content-length" in hdr and (not hdr["content-length"].isdigit()
                    or int(hdr["content-length"]) != len(body)):
                raise ValueError
            return strict_json(body)
        except CustomerCloudError:
            raise
        except Exception:
            raise CustomerCloudError() from None

    def authenticate(self, authorization):
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise CustomerCloudError("unauthorized", 401)
        token = authorization[7:]
        if not 1 <= len(token) <= 8192 or not _JWT.fullmatch(token):
            raise CustomerCloudError("unauthorized", 401)
        user = self._get("/auth/v1/user",
            {"apikey": self._public, "Authorization": "Bearer " + token}, authenticating=True)
        try:
            if (not isinstance(user, dict) or user.get("aud") != "authenticated"
                    or user.get("role") != "authenticated" or user.get("is_anonymous") is not False):
                raise ValueError
            return canonical_uuid(user.get("id"))
        except (ValueError, TypeError):
            raise CustomerCloudError("unauthorized", 401) from None

    def settings_for(self, owner_id):
        if owner_id != self.owner_id:
            raise CustomerCloudError("settings_owner_mismatch", 403)
        headers = {"apikey": self._secret}
        if _JWT.fullmatch(self._secret):
            headers["Authorization"] = "Bearer " + self._secret
        query = urlencode({"owner_id": "eq." + self.owner_id, "select": _COLUMNS, "limit": "2"})
        rows = self._get("/rest/v1/autopilot_settings?" + query, headers)
        try:
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError
            validate_saved_settings(rows[0], owner_id=self.owner_id, now=self.clock())
            return rows[0]
        except SettingsBindingError:
            # A successful DB response proving a changed/invalid row is NOT an
            # outage. The worker must durably pause, even if an old row returns.
            raise
        except Exception:
            raise SettingsBindingError("invalid_saved_settings_row") from None
