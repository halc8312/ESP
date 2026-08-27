"""Fail-closed safety checks shared by marketplace scrapers.

The search scrapers consume links controlled by third-party HTML.  This
module keeps those links on the expected marketplace, distinguishes a real
"zero results" page from selector drift, and turns block/error pages into
explicit failures instead of an ambiguous empty list.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from urllib.parse import urljoin, urlparse, urlunparse

logger = logging.getLogger(__name__)


class ScrapeFailure(RuntimeError):
    """Base class for an operational scrape failure.

    ``status_code`` keeps the observed HTTP status attached to the failure so
    callers can tell a definitive missing-resource response (404/410) apart
    from a transport or parsing problem.
    """

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ScrapeBlockedError(ScrapeFailure):
    """The marketplace returned an access-control or challenge page."""


class ScrapeHttpError(ScrapeFailure):
    """The marketplace returned an unsuccessful HTTP response."""


class ScrapeSelectorDriftError(ScrapeFailure):
    """A search page loaded but neither products nor a no-results marker existed."""


class UnsafeScrapeUrlError(ScrapeFailure):
    """A discovered or redirected URL escaped the per-site allowlist."""


_SITE_HOSTS = {
    "mercari": {
        "search": ("jp.mercari.com",),
        "detail": ("jp.mercari.com",),
    },
    "yahoo": {
        "search": ("shopping.yahoo.co.jp", "store.shopping.yahoo.co.jp"),
        "detail": ("store.shopping.yahoo.co.jp",),
    },
    "rakuma": {
        "search": ("fril.jp", "www.fril.jp"),
        "detail": ("item.fril.jp",),
    },
    "surugaya": {
        "search": ("suruga-ya.jp", "www.suruga-ya.jp"),
        "detail": ("suruga-ya.jp", "www.suruga-ya.jp"),
    },
    "offmall": {
        "search": ("netmall.hardoff.co.jp",),
        "detail": ("netmall.hardoff.co.jp",),
    },
    "yahuoku": {
        "search": ("auctions.yahoo.co.jp",),
        "detail": ("page.auctions.yahoo.co.jp", "auctions.yahoo.co.jp"),
    },
    "snkrdunk": {
        "search": ("snkrdunk.com", "www.snkrdunk.com"),
        "detail": ("snkrdunk.com", "www.snkrdunk.com"),
    },
    "recordcity": {
        "search": ("recordcity.jp", "www.recordcity.jp"),
        "detail": ("recordcity.jp", "www.recordcity.jp"),
    },
}


_HOST_SITE_MAP = {
    host: site
    for site, kinds in _SITE_HOSTS.items()
    for hosts in kinds.values()
    for host in hosts
}


def identify_marketplace_site(host: str) -> str | None:
    """Return the supported marketplace for an exact normalized host."""
    try:
        normalized = _normalize_host(host)
    except UnsafeScrapeUrlError:
        return None
    return _HOST_SITE_MAP.get(normalized)


def marketplace_hosts(site: str, *, kind: str | None = None) -> tuple[str, ...]:
    """Expose the exact host boundary shared by request classification/fetching."""
    site_hosts = _SITE_HOSTS.get(str(site or "").strip().lower(), {})
    if kind is not None:
        return tuple(site_hosts.get(kind, ()))
    return tuple(sorted({host for hosts in site_hosts.values() for host in hosts}))


_YAHOO_NON_ITEM_PAGE_IDS = frozenset(
    {
        "about",
        "category",
        "contact",
        "guide",
        "index",
        "info",
        "notice",
        "privacypolicy",
        "ranking",
        "review",
        "search",
        "topics",
    }
)
_YAHOO_STORE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.IGNORECASE)
_YAHOO_PAGE_ID_RE = re.compile(r"([a-z0-9][a-z0-9._-]{0,98})\.html", re.IGNORECASE)


def _is_yahoo_detail_path(host: str, path: str) -> bool:
    """Recognize the documented ``/{store}/{item-code}.html`` shape.

    Store roots, in-store searches (``search.html``), system pages, and
    deeper free-space/category routes must not be routed to the detail parser.
    Product and custom-category IDs share part of the same legacy namespace,
    so fetched pages must still expose actual product data.
    """
    if host != "store.shopping.yahoo.co.jp":
        return False
    parts = str(path or "").split("/")
    if len(parts) != 3 or parts[0] != "":
        return False
    store_id, filename = parts[1], parts[2]
    if _YAHOO_STORE_ID_RE.fullmatch(store_id) is None:
        return False
    match = _YAHOO_PAGE_ID_RE.fullmatch(filename)
    if match is None:
        return False
    return match.group(1).lower() not in _YAHOO_NON_ITEM_PAGE_IDS


def _is_detail_path(site: str, host: str, path: str) -> bool:
    normalized_path = path or "/"
    if site == "mercari":
        return normalized_path.startswith("/item/") or normalized_path.startswith("/shops/product/")
    if site == "yahoo":
        return _is_yahoo_detail_path(host, normalized_path)
    if site == "rakuma":
        return host == "item.fril.jp" and normalized_path != "/"
    if site == "surugaya":
        return "/product/detail/" in normalized_path
    if site == "offmall":
        return normalized_path.startswith("/product/")
    if site == "yahuoku":
        return "/auction/" in normalized_path
    if site == "snkrdunk":
        return normalized_path.startswith("/products/")
    if site == "recordcity":
        # /catalog/4936480, and the /ja/... form the site redirects to.
        return bool(re.fullmatch(r"/(?:[a-z]{2}/)?catalog/\d+/?", normalized_path))
    return False


def _normalize_host(raw_host: str) -> str:
    host = str(raw_host or "").strip().rstrip(".").lower()
    if not host:
        raise UnsafeScrapeUrlError("取得先URLにホスト名がありません。")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeScrapeUrlError("取得先URLのホスト名が不正です。") from exc
    labels = host.split(".")
    if (
        len(host) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-z0-9-]+", label) is None
            for label in labels
        )
    ):
        raise UnsafeScrapeUrlError("取得先URLのホスト名が不正です。")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise UnsafeScrapeUrlError("取得先URLにIPアドレスは使用できません。")


def _host_matches(host: str, allowed_host: str) -> bool:
    return host == allowed_host


def validate_marketplace_url(url: str, site: str, *, kind: str) -> str:
    """Validate and normalize a marketplace search or detail URL.

    Only HTTPS on port 443 is accepted.  Host checks use DNS label boundaries,
    so values such as ``snkrdunk.com.evil.example`` never match.
    """
    if kind not in {"search", "detail"}:
        raise ValueError(f"Unsupported URL kind: {kind}")

    raw = str(url or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise UnsafeScrapeUrlError("取得先URLの形式が不正です。") from exc

    if parsed.scheme.lower() != "https":
        raise UnsafeScrapeUrlError("取得先URLはHTTPSである必要があります。")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise UnsafeScrapeUrlError("取得先URLのホスト名が不正です。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeScrapeUrlError("取得先URLのポート番号が不正です。") from exc
    if port not in (None, 443):
        raise UnsafeScrapeUrlError("取得先URLは標準のHTTPSポートのみ使用できます。")

    host = _normalize_host(parsed.hostname)
    allowed_hosts = _SITE_HOSTS.get(site, {}).get(kind, ())
    if not allowed_hosts or not any(_host_matches(host, allowed) for allowed in allowed_hosts):
        raise UnsafeScrapeUrlError(f"{site}の許可ドメイン外のURLを検出しました。")
    if kind == "detail" and not _is_detail_path(site, host, parsed.path):
        raise UnsafeScrapeUrlError(f"{site}の商品ページではないURLを検出しました。")

    normalized_netloc = host
    if port == 443 and parsed.port is not None:
        normalized_netloc = f"{host}:443"
    return urlunparse(parsed._replace(scheme="https", netloc=normalized_netloc, fragment=""))


def page_text(page) -> str:
    """Best-effort text extraction for Scrapling, Playwright, and test doubles."""
    if page is None:
        return ""
    for attr_name in ("get_all_text", "get_text"):
        extractor = getattr(page, attr_name, None)
        if not callable(extractor):
            continue
        try:
            value = extractor()
        except Exception:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        if isinstance(value, str) and value.strip():
            return value

    for attr_name in ("body", "text", "content"):
        value = getattr(page, attr_name, None)
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        if isinstance(value, str) and value.strip():
            return value
    return ""


def response_status(fetch_result) -> int | None:
    """Read an integer HTTP status from supported response adapters."""
    status = getattr(fetch_result, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool):
        status = getattr(fetch_result, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None


def response_header(fetch_result, name: str) -> str:
    """Read a response header without depending on a specific HTTP client."""
    headers = getattr(fetch_result, "headers", None)
    if callable(headers):
        try:
            headers = headers()
        except Exception:
            headers = None
    if headers is None:
        return ""
    try:
        direct = headers.get(name)
        if direct is not None:
            return str(direct).strip()
        expected = name.lower()
        for key, value in headers.items():
            if str(key).lower() == expected:
                return str(value).strip()
    except Exception:
        return ""
    return ""


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_MARKETPLACE_REDIRECTS = 5


def _redirect_target(fetch_result, current_url: str, site: str, *, kind: str) -> str | None:
    status = response_status(fetch_result)
    if status not in _REDIRECT_STATUSES:
        return None
    location = response_header(fetch_result, "Location")
    if not location:
        raise ScrapeHttpError(f"{site}から転送先のないHTTP {status}応答が返されました。")
    return validate_marketplace_url(urljoin(current_url, location), site, kind=kind)


def fetch_with_safe_redirects(
    fetch_once,
    url: str,
    site: str,
    *,
    kind: str,
    max_redirects: int = MAX_MARKETPLACE_REDIRECTS,
    allowed_statuses: frozenset[int] = frozenset(),
):
    """Fetch synchronously while validating every redirect before following it."""
    current_url = validate_marketplace_url(url, site, kind=kind)
    for redirect_count in range(max(0, int(max_redirects)) + 1):
        response = fetch_once(current_url)
        next_url = _redirect_target(response, current_url, site, kind=kind)
        if next_url is None:
            validate_fetch_response(
                response,
                site,
                kind=kind,
                allowed_statuses=allowed_statuses,
            )
            return response
        if redirect_count >= max_redirects:
            raise ScrapeHttpError(f"{site}の取得で転送回数が上限を超えました。")
        current_url = next_url
    raise ScrapeHttpError(f"{site}の取得で転送回数が上限を超えました。")


async def fetch_with_safe_redirects_async(
    fetch_once,
    url: str,
    site: str,
    *,
    kind: str,
    max_redirects: int = MAX_MARKETPLACE_REDIRECTS,
    allowed_statuses: frozenset[int] = frozenset(),
):
    """Async counterpart of :func:`fetch_with_safe_redirects`."""
    current_url = validate_marketplace_url(url, site, kind=kind)
    for redirect_count in range(max(0, int(max_redirects)) + 1):
        response = await fetch_once(current_url)
        next_url = _redirect_target(response, current_url, site, kind=kind)
        if next_url is None:
            validate_fetch_response(
                response,
                site,
                kind=kind,
                allowed_statuses=allowed_statuses,
            )
            return response
        if redirect_count >= max_redirects:
            raise ScrapeHttpError(f"{site}の取得で転送回数が上限を超えました。")
        current_url = next_url
    raise ScrapeHttpError(f"{site}の取得で転送回数が上限を超えました。")


def is_marketplace_host_url(url: str, site: str) -> bool:
    """Report whether a URL stays on the marketplace, ignoring page kind.

    Used for sub-frame documents, where the path contract of the page being
    scraped (search vs detail) does not apply.
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    try:
        if parsed.port not in (None, 443):
            return False
    except ValueError:
        return False
    if not parsed.hostname:
        return False
    try:
        host = _normalize_host(parsed.hostname)
    except UnsafeScrapeUrlError:
        return False
    return host in marketplace_hosts(site)


def _is_subframe_request(request) -> bool:
    """Detect a document request for a nested frame rather than the tab itself.

    Playwright exposes the owning frame on every request; the top-level frame
    is the one without a parent.  Adapters that do not expose ``frame`` are
    treated as top-level so the strict guard still applies to them.
    """
    frame = getattr(request, "frame", None)
    if frame is None:
        return False
    try:
        return getattr(frame, "parent_frame", None) is not None
    except Exception:
        return False


async def install_navigation_guard(context, site: str, *, kind: str) -> list[str]:
    """Abort disallowed browser document navigations before network dispatch.

    Subresources such as marketplace CDNs remain untouched.

    Only *top-level* navigations are scrape failures: those are the ones that
    can swap the page we are reading for an off-site or private-network
    document.  Marketplace pages also embed third-party ``<iframe>`` documents
    (tag managers, ad slots, captcha widgets) whose URLs are cross-domain by
    design.  Those are still aborted so the browser never fetches them, but
    they are page furniture, not a redirect escape, so they must not fail the
    scrape.  Returning them as failures made every Mercari/Rakuma search abort
    with "許可ドメイン外へのページ転送を遮断しました。".

    Returns the list of blocked *top-level* navigation URLs.
    """
    blocked_urls: list[str] = []

    async def _guard(route, request):
        is_navigation = False
        checker = getattr(request, "is_navigation_request", None)
        if callable(checker):
            try:
                is_navigation = bool(checker())
            except Exception:
                is_navigation = False
        resource_type = str(getattr(request, "resource_type", "") or "").lower()
        if is_navigation or resource_type == "document":
            request_url = str(getattr(request, "url", "") or "")
            if _is_subframe_request(request):
                if not is_marketplace_host_url(request_url, site):
                    logger.debug(
                        "Blocked third-party %s sub-frame document: %s",
                        site,
                        request_url[:200],
                    )
                    await route.abort("blockedbyclient")
                    return
            else:
                try:
                    validate_marketplace_url(request_url, site, kind=kind)
                except UnsafeScrapeUrlError:
                    blocked_urls.append(request_url)
                    await route.abort("blockedbyclient")
                    return
        await route.continue_()

    router = getattr(context, "route", None)
    if not callable(router):
        raise ScrapeHttpError(f"{site}のブラウザ通信ガードを設定できませんでした。")
    await router("**/*", _guard)
    return blocked_urls


def raise_for_blocked_navigation(blocked_urls: list[str], site: str) -> None:
    if not blocked_urls:
        return
    if is_marketplace_host_url(blocked_urls[0], site):
        raise UnsafeScrapeUrlError(f"{site}の対象外ページへのページ転送を遮断しました。")
    raise UnsafeScrapeUrlError(f"{site}の許可ドメイン外へのページ転送を遮断しました。")


_BLOCK_MARKERS = (
    "just a moment",
    "attention required",
    "access denied",
    "cf-chl",
    "cf-browser-verification",
    "challenge-platform",
    "challenges.cloudflare.com",
    "robot check",
    "verify you are human",
    "ロボットではないことを確認",
    "アクセスが制限されています",
    "不正なアクセスを検知",
)

_NO_RESULTS_MARKERS = {
    "mercari": (
        "検索結果がありません",
        "該当する商品がありません",
        "該当の商品がありません",
        "出品された商品がありません",
        "商品が見つかりませんでした",
    ),
    "yahoo": (
        "検索結果がありません",
        "該当する商品はありません",
        "該当の商品はありません",
        "商品が見つかりませんでした",
    ),
    "rakuma": (
        "該当する商品はありません",
        "該当の商品はありません",
        "検索結果がありません",
        "商品が見つかりませんでした",
    ),
    "surugaya": (
        "該当する商品がありません",
        "該当の商品がありません",
        "検索結果がありません",
        "商品は見つかりませんでした",
    ),
    "offmall": (
        "該当する商品がありません",
        "該当の商品がありません",
        "商品が見つかりませんでした",
        "検索結果がありません",
    ),
    "yahuoku": (
        "該当する商品はありませんでした",
        "該当の商品はありませんでした",
        "検索結果がありません",
        "商品が見つかりませんでした",
    ),
    "snkrdunk": (
        "検索結果がありません",
        "該当する商品がありません",
        "該当の商品がありません",
        "商品が見つかりませんでした",
        "no products found",
    ),
    "recordcity": (
        "該当する商品がありません",
        "該当の商品がありません",
        "検索結果がありません",
        "商品が見つかりませんでした",
        "0件",
    ),
}


def _looks_blocked(text: str) -> bool:
    normalized = str(text or "").lower()
    return any(marker in normalized for marker in _BLOCK_MARKERS)


def has_no_results_evidence(text: str, site: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if any(marker.lower() in normalized for marker in _NO_RESULTS_MARKERS.get(site, ())):
        return True
    return bool(
        re.search(
            r"(?:検索結果|該当商品|該当する商品|商品数)\s*(?:[:：=はが]\s*)?0\s*件",
            normalized,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:search\s+)?results?\s*(?:[:：=]\s*)?0\b",
            normalized,
            flags=re.IGNORECASE,
        )
        or re.search(r"\b0\s+(?:search\s+)?results?\b", normalized, flags=re.IGNORECASE)
    )


def validate_fetch_response(
    fetch_result,
    site: str,
    *,
    kind: str,
    text: str | None = None,
    allowed_statuses: frozenset[int] = frozenset(),
) -> None:
    """Validate HTTP status, final URL, and challenge markers when exposed."""
    if fetch_result is None:
        raise ScrapeHttpError(f"{site}から応答を取得できませんでした。")

    status = response_status(fetch_result)
    if status is not None and 300 <= status < 400:
        raise ScrapeHttpError(
            f"{site}から未処理の転送応答が返されました（HTTP {status}）。", status_code=status
        )
    if status is not None and status >= 400 and status not in allowed_statuses:
        if status in {401, 403, 429, 503}:
            raise ScrapeBlockedError(
                f"{site}へのアクセスが制限されました（HTTP {status}）。", status_code=status
            )
        raise ScrapeHttpError(
            f"{site}の取得に失敗しました（HTTP {status}）。", status_code=status
        )

    final_url = getattr(fetch_result, "url", None)
    if isinstance(final_url, str) and final_url.strip():
        validate_marketplace_url(final_url, site, kind=kind)

    observed_text = page_text(fetch_result) if text is None else text
    if _looks_blocked(observed_text):
        raise ScrapeBlockedError(f"{site}のアクセス確認画面を検出しました。")


def require_search_outcome(site: str, *, candidate_count: int, text: str) -> None:
    """Require candidates or explicit evidence that a zero-result page is valid."""
    if _looks_blocked(text):
        raise ScrapeBlockedError(f"{site}のアクセス確認画面を検出しました。")
    if candidate_count > 0 or has_no_results_evidence(text, site):
        return
    raise ScrapeSelectorDriftError(
        f"{site}の検索ページから商品を確認できませんでした。"
        "サイト側の表示変更または通信障害の可能性があります。"
    )


def require_usable_details(site: str, *, candidate_count: int, item_count: int) -> None:
    """Fail when candidates existed but every detail extraction failed."""
    if candidate_count > 0 and item_count == 0:
        raise ScrapeSelectorDriftError(
            f"{site}の商品候補は見つかりましたが、商品詳細を1件も取得できませんでした。"
        )


def is_usable_detail_result(item) -> bool:
    if not isinstance(item, dict):
        return False
    status = str(item.get("status") or "").strip().lower()
    return bool(str(item.get("title") or "").strip()) and status not in {
        "blocked",
        "challenge",
        "error",
    }


def raise_for_unsafe_detail_result(site: str, result) -> None:
    """Propagate unsafe/block failures even when another detail succeeded.

    Ordinary transport/HTTP/parser failures remain eligible for partial
    success; :func:`require_usable_details` turns them into a failed search
    only when every discovered detail failed.
    """
    if isinstance(result, (UnsafeScrapeUrlError, ScrapeBlockedError)):
        raise result
    if isinstance(result, Exception):
        return
    if not isinstance(result, dict):
        return
    status = str(result.get("status") or "").strip().lower()
    if status in {"blocked", "challenge"}:
        raise ScrapeBlockedError(f"{site}の商品ページでアクセス制限を検出しました。")
