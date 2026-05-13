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
        # URL-escape user/pass so '@' or ':' inside credentials do not break parsing.
        return f"http://{quote(self.user, safe='')}:{quote(self.password, safe='')}@{self.host}:{self.port}"


class ProductConfig(BaseModel):
    item_id: str
    name: str

    @field_validator("item_id")
    @classmethod
    def validate_item_id(cls, v):
        # Walmart item IDs are numeric, typically 6 to 12 digits. Regex stays
        # loose at 5 to 15 to cover outliers.
        if not re.fullmatch(r"\d{5,15}", v):
            raise ValueError("item_id must be a numeric string of 5 to 15 digits")
        return v


def _load_proxies_from_env():
    """Parse proxies from the PROXIES env var.

    Format: one proxy per line, each line as host:port:user:password. Password
    may contain colons (the split is bounded to 4 fields). Empty value returns
    an empty list, which puts the scraper in direct mode via curl_cffi alone.
    """
    raw = os.environ.get("PROXIES", "").strip()
    if not raw:
        return []

    proxies = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # maxsplit=3 keeps any ':' inside the password intact.
        parts = line.split(":", 3)
        if len(parts) != 4:
            raise ValueError(f"Bad proxy line (expected host:port:user:pass): {line}")
        host, port, user, password = parts
        proxies.append(ProxyConfig(host=host, port=port, user=user, password=password))

    return proxies


PROXIES = _load_proxies_from_env()
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
