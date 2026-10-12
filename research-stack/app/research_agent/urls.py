from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid",
    "ref", "ref_src", "ref_url", "spm", "_hsenc", "_hsmi", "cmpid", "ito",
}
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "ga_")
DEFAULT_PORTS = {"http": "80", "https": "443"}
ARXIV_RE = re.compile(r"^/(abs|pdf)/(?P<id>[0-9]{4}\.[0-9]{4,5}|[a-z\-]+(\.[A-Z]{2})?/[0-9]{7})(v[0-9]+)?(\.pdf)?$")


def normalize_url(url: str) -> str:
    """Canonical form used for dedup and as the cache/vector key.

    - lowercases scheme and host, drops "www.", default ports and fragments
    - removes tracking parameters and sorts the rest
    - removes trailing slash (except root)
    - maps arXiv /pdf/ and versioned links to https://arxiv.org/abs/<id>
    - maps doi.org variants to https://doi.org/<lowercase doi>
    """
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parts.port}" if parts.port and str(parts.port) != DEFAULT_PORTS.get(scheme) else ""
    path = re.sub(r"/{2,}", "/", parts.path or "/")

    if host in {"arxiv.org", "export.arxiv.org"}:
        m = ARXIV_RE.match(path)
        if m:
            return f"https://arxiv.org/abs/{m.group('id')}"
    if host in {"doi.org", "dx.doi.org"}:
        return f"https://doi.org/{path.lstrip('/').lower()}"

    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    ]
    query.sort()
    return urlunsplit((scheme, host + port, path, urlencode(query), ""))


def hostname(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def content_hash(text: str) -> str:
    """Exact-duplicate fingerprint on whitespace/case-normalised text."""
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def simhash(text: str, bits: int = 64) -> int:
    """64-bit SimHash over word 3-shingles, for near-duplicate detection (syndicated news, mirrors)."""
    words = re.findall(r"\w+", text.lower())
    shingles = [" ".join(words[i : i + 3]) for i in range(max(1, len(words) - 2))]
    vector = [0] * bits
    for sh in shingles:
        h = int.from_bytes(hashlib.blake2b(sh.encode("utf-8"), digest_size=8).digest(), "big")
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    return sum(1 << i for i in range(bits) if vector[i] > 0)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class Deduper:
    """Tracks canonical URLs, exact hashes and simhashes seen during one research run."""

    def __init__(self, near_dup_threshold: int = 3) -> None:
        self.urls: set[str] = set()
        self.hashes: set[str] = set()
        self.simhashes: list[int] = []
        self.threshold = near_dup_threshold

    def seen_url(self, canonical_url: str) -> bool:
        return canonical_url in self.urls

    def add_url(self, canonical_url: str) -> None:
        self.urls.add(canonical_url)

    def is_duplicate_text(self, text: str) -> bool:
        h = content_hash(text)
        if h in self.hashes:
            return True
        sh = simhash(text)
        if any(hamming(sh, other) <= self.threshold for other in self.simhashes):
            return True
        self.hashes.add(h)
        self.simhashes.append(sh)
        return False
