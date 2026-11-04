import re
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit

_TAIL = re.compile(r"(?:/|\.(?:pdf|html?|aspx?|php))+$", re.IGNORECASE)
_DEFAULT_DOCUMENT = re.compile(r"/(index|default)\.(s?html?|aspx?|php|cfm|jsp)$", re.IGNORECASE)
_DEFAULT_PORT = {"http": ":80", "https": ":443"}
_HOST_PREFIX = re.compile(r"^(?:www\.|(?:m|mobile)\.(?=[^.]+\.))+")
_HOST_PORT = re.compile(r"^[\w.-]+:\d+(?:[/?#]|$)")
_SLASHES = re.compile(r"/{2,}")
_SESSION_PATH_PARAM = re.compile(r";jsessionid[^/;]*", re.IGNORECASE)
_TRACKING = re.compile(
    r"utm_\w*|gclid|gclsrc|dclid|gbraid|wbraid|srsltid|fbclid|msclkid|yclid|twclid|igshid"
    r"|mc_cid|mc_eid|_ga|_gl|_hsenc|_hsmi|mkt_tok|ref_src|ref_url"
    r"|phpsessid|aspsessionid\w*|jsessionid|cfid|cftoken",
    re.IGNORECASE,
)
_SESSION_TOKEN = re.compile(r"[0-9a-f]{16,}|[0-9a-z]{24,}", re.IGNORECASE)
_NAVIGATION: dict[str, re.Pattern[str]] = {
    "govt.westlaw.com": re.compile(
        r"viewtype|originationcontext|transitiontype|contextdata|bhcp|needtoinjectterms|ppcid"
        r"|t_(?:[st]\d+|querytext)",
        re.IGNORECASE,
    ),
}
_UNRESERVED = re.compile(
    r"%(4[1-9A-Fa-f]|5[0-9Aa]|6[1-9A-Fa-f]|7[0-9Aa]|3[0-9]|2[DdEe]|5[Ff]|7[Ee])"
)
_MARKS = re.compile(r"%(2[1789Aa])")
_PERCENT = re.compile(r"%[0-9a-f]{2}", re.IGNORECASE)
CASE_INSENSITIVE_HOSTS = frozenset(
    {
        "app.leg.wa.gov",
        "capitol.texas.gov",
        "cms.gov",
        "cslb.ca.gov",
        "csrc.nist.gov",
        "delcode.delaware.gov",
        "dir.ca.gov",
        "dnr.maryland.gov",
        "docs.sandiego.gov",
        "faa.gov",
        "flsenate.gov",
        "gc.nh.gov",
        "guthrie.house.gov",
        "ilga.gov",
        "irs.gov",
        "lawfilesext.leg.wa.gov",
        "le.utah.gov",
        "leg.state.fl.us",
        "leg.state.nv.us",
        "legis.state.pa.us",
        "legislation.nysenate.gov",
        "legislature.idaho.gov",
        "legislature.mi.gov",
        "malegislature.gov",
        "ncleg.gov",
        "nvlpubs.nist.gov",
        "oregon.gov",
        "portal.ct.gov",
        "privacy.ca.gov",
        "rules.sos.ri.gov",
        "statutes.capitol.texas.gov",
        "tcss.legis.texas.gov",
    }
)


def _kept(host: str, key: str, value: str) -> bool:
    if _TRACKING.fullmatch(key):
        return False
    navigation = _NAVIGATION.get(host)
    if navigation and navigation.fullmatch(key):
        return False
    return not (key.lower() == "sid" and _SESSION_TOKEN.fullmatch(value))


def _remove_dot_segments(path: str) -> str:
    segments: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if len(segments) > 1:
                segments.pop()
        elif segment != ".":
            segments.append(segment)
    return "/".join(segments)


def _query(host: str, query: str, fold: Callable[[str], str]) -> str:
    pairs = [
        (fold(key), fold(value).rstrip("."))
        for key, value in parse_qsl(query, keep_blank_values=True)
        if _kept(host, key, value)
    ]
    if any(value for _, value in pairs):
        pairs = [(key, value) for key, value in pairs if value]
    encoded = urlencode(sorted(pairs))
    return "?" + encoded if encoded else ""


def canonical(url: str) -> str:
    url = url.strip()
    parts = urlsplit("//" + url if _HOST_PORT.match(url) else url)
    netloc = parts.netloc.lower().removesuffix(_DEFAULT_PORT.get(parts.scheme.lower(), ""))
    path = parts.path
    if not netloc and path:
        head, _, rest = path.lstrip("/").partition("/")
        if "." in head:
            netloc = head.lower()
            path = "/" + rest if rest else ""
    host = _HOST_PREFIX.sub("", netloc)
    path = _UNRESERVED.sub(lambda m: chr(int(m.group(1), 16)), path)
    path = _SLASHES.sub("/", _SESSION_PATH_PARAM.sub("", path))
    path = _MARKS.sub(lambda m: chr(int(m.group(1), 16)), path)
    path = _remove_dot_segments(_PERCENT.sub(lambda m: m.group(0).upper(), path))
    path = _TAIL.sub("", _DEFAULT_DOCUMENT.sub("", path.rstrip("/")))
    fold = str.lower if host in CASE_INSENSITIVE_HOSTS else str
    return host + fold(path) + _query(host, parts.query, fold)
