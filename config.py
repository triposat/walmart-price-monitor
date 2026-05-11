# config.py: loads proxies from environment variable, validates products

import os
import re
from urllib.parse import quote
from pydantic import BaseModel, field_validator


class ProxyConfig(BaseModel):
    host: str
    port: str
    user: str
    password: str

    @property
    def url(self):
        # quote() escapes special characters such as @, :, /, # in the
        # username and password so they do not break the URL structure.
        return f"http://{quote(self.user, safe='')}:{quote(self.password, safe='')}@{self.host}:{self.port}"


class ProductConfig(BaseModel):
    item_id: str
    name: str

    @field_validator("item_id")
    @classmethod
    def validate_item_id(cls, v):
        # Walmart item IDs are numeric strings, typically 6 to 12 digits.
        # The range below stays loose to allow legacy and future IDs.
        if not re.fullmatch(r"\d{5,15}", v):
            raise ValueError("item_id must be a numeric string of 5 to 15 digits")
        return v


def _load_proxies_from_env():
    """Parse proxies from the PROXIES env var.

    Format: one proxy per line, each line as host:port:user:password.
    The password may contain colons; the split is limited to 4 fields.

    An empty PROXIES value is allowed and returns an empty list. The
    scraper then runs in direct mode (no proxy on the request), which
    relies on curl_cffi's TLS impersonation alone. Direct mode is fine
    for short-term testing but the same IP making sustained requests
    will eventually flag at Akamai; configure real ISP proxies for
    anything beyond a few hours of runtime.
    """
    raw = os.environ.get("PROXIES", "").strip()
    if not raw:
        return []

    proxies = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # split into 4 parts so a colon inside the password is preserved.
        parts = line.split(":", 3)
        if len(parts) != 4:
            raise ValueError(f"Bad proxy line (expected host:port:user:pass): {line}")
        host, port, user, password = parts
        proxies.append(ProxyConfig(host=host, port=port, user=user, password=password))

    return proxies


PROXIES = _load_proxies_from_env()
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
