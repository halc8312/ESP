"""Shared, site-scoped admission for marketplace network operations.

Redis is mandatory in production. Leases are owned, renewable and expiring;
each navigation/retry also consumes a bounded job budget and start interval.
No credentials, target paths, or customer identifiers are stored in keys.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import os
import threading
import time
from urllib.parse import urlparse
import uuid

from security_config import is_production_runtime
from services.scrape_safety import ScrapeBlockedError, ScrapeFailure, identify_marketplace_site


class AccessStoreUnavailable(ScrapeFailure):
    pass


class AccessBudgetExceeded(ScrapeFailure):
    pass


class AccessLeaseLost(ScrapeFailure):
    pass


_SITES = frozenset({"mercari", "rakuma", "snkrdunk", "recordcity", "surugaya", "yahoo", "yahuoku", "offmall"})
_held: ContextVar[dict] = ContextVar("marketplace_access_leases", default={})
_budget: ContextVar["RequestBudget | None"] = ContextVar("marketplace_request_budget", default=None)


def resolve_site(value: str) -> str:
    value = str(value or "").lower()
    if value in _SITES:
        return value
    site = identify_marketplace_site(urlparse(value).hostname or "")
    if site not in _SITES:
        raise AccessBudgetExceeded("取得先サイトを確認できません。")
    return site


def _setting(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
        if not minimum <= value <= maximum:
            return default
        return value
    except (TypeError, ValueError):
        return default


@dataclass
class RequestBudget:
    max_requests: int = 120
    max_seconds: float = 900
    started_at: float = field(default_factory=time.monotonic)
    requests: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, consume=False):
        with self.lock:
            if time.monotonic() - self.started_at >= self.max_seconds:
                raise AccessBudgetExceeded("取得時間の上限に達しました。取得済みの商品を確認してください。")
            if consume:
                if self.requests >= self.max_requests:
                    raise AccessBudgetExceeded("取得リクエスト数の上限に達しました。取得済みの商品を確認してください。")
                self.requests += 1


@contextmanager
def request_budget(max_requests=120, max_seconds=900):
    # Reuse the mutable object across asyncio/to_thread/worker runtime tasks.
    existing = _budget.get()
    token = _budget.set(existing or RequestBudget(max_requests=max_requests, max_seconds=max_seconds))
    try:
        yield _budget.get()
    finally:
        _budget.reset(token)


def _check_active(consume=False):
    budget = _budget.get()
    if budget:
        budget.check(consume=consume)
    from services.scrape_job_runtime import assert_current_job_active
    assert_current_job_active()


def check_request_budget():
    budget = _budget.get()
    if budget:
        budget.check()


class MemoryAccessStore:
    """Development/test only; the algorithm matches the shared Redis store."""
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.states = {}

    def acquire(self, site, owner, *, interval, concurrency, lease_seconds, consume, parent=None):
        with self.lock:
            now = self.clock()
            state = self.states.setdefault(site, {"leases": {}, "next": 0.0, "pause": 0.0})
            leases = state["leases"]
            for token, deadline in list(leases.items()):
                if deadline <= now:
                    del leases[token]
            if parent and parent not in leases:
                raise AccessLeaseLost("取得処理のロックが失効しました。")
            if state["pause"] > now:
                return state["pause"] - now, "site_cooldown"
            if consume and state["next"] > now:
                return state["next"] - now, "site_interval"
            if not parent and len(leases) >= concurrency:
                return min(1.0, min(leases.values()) - now), "site_busy"
            if not parent:
                leases[owner] = now + lease_seconds
            if consume:
                state["next"] = now + interval
            return 0.0, ""

    def release(self, site, owner):
        with self.lock:
            self.states.get(site, {}).get("leases", {}).pop(owner, None)

    def renew(self, site, owner, lease_seconds):
        with self.lock:
            now = self.clock()
            leases = self.states.get(site, {}).get("leases", {})
            if leases.get(owner, 0) <= now:
                return False
            leases[owner] = now + lease_seconds
            return True

    def pause(self, site, seconds):
        with self.lock:
            state = self.states.setdefault(site, {"leases": {}, "next": 0.0, "pause": 0.0})
            state["pause"] = max(state["pause"], self.clock() + seconds)


_ACQUIRE_LUA = """
local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local parent=ARGV[6]
if parent~='' and not redis.call('ZSCORE', KEYS[1], parent) then return {-1,3} end
local pause=tonumber(redis.call('GET', KEYS[3]) or 0)
if pause>now then return {math.ceil((pause-now)*1000),2} end
local consume=ARGV[5]=='1'
local next=tonumber(redis.call('GET', KEYS[2]) or 0)
if consume and next>now then return {math.ceil((next-now)*1000),1} end
if parent=='' and redis.call('ZCARD', KEYS[1])>=tonumber(ARGV[3]) then return {1000,3} end
if parent=='' then
 redis.call('ZADD', KEYS[1], now+tonumber(ARGV[4]), ARGV[1])
 redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[4])*2))
end
if consume then redis.call('SET', KEYS[2], now+tonumber(ARGV[2]), 'EX', math.ceil(tonumber(ARGV[2])+60)) end
return {0,0}
"""
_RENEW_LUA = """
local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
local expires=tonumber(redis.call('ZSCORE',KEYS[1],ARGV[1]) or 0)
if expires<=now then return 0 end
redis.call('ZADD',KEYS[1],now+tonumber(ARGV[2]),ARGV[1])
redis.call('EXPIRE',KEYS[1],math.ceil(tonumber(ARGV[2])*2)); return 1
"""
_PAUSE_LUA = """
local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
local deadline=math.max(tonumber(redis.call('GET',KEYS[1]) or 0),now+tonumber(ARGV[1]))
redis.call('SET',KEYS[1],deadline,'EX',math.ceil(deadline-now)+60); return 1
"""


class RedisAccessStore:
    def __init__(self, url, client=None):
        if client is None:
            import redis
            client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        self.client = client

    def _keys(self, site):
        # Hash tag keeps the site-scoped transaction usable with Redis Cluster.
        prefix = f"esp:marketplace:{{{site}}}"
        return [prefix + ":leases", prefix + ":next", prefix + ":pause"]

    def acquire(self, site, owner, *, interval, concurrency, lease_seconds, consume, parent=None):
        try:
            wait_ms, reason = self.client.eval(_ACQUIRE_LUA, 3, *self._keys(site), owner,
                interval, concurrency, lease_seconds, int(consume), parent or "")
            if int(wait_ms) < 0:
                raise AccessLeaseLost("取得処理のロックが失効しました。")
            return int(wait_ms)/1000, {0: "", 1: "site_interval", 2: "site_cooldown", 3: "site_busy"}[int(reason)]
        except AccessLeaseLost:
            raise
        except Exception:
            raise AccessStoreUnavailable("共有アクセス制限を確認できないため、取得を停止しました。") from None

    def release(self, site, owner):
        try:
            self.client.zrem(self._keys(site)[0], owner)
        except Exception:
            raise AccessStoreUnavailable("取得ロックの解放を確認できません。期限切れまで取得を停止します。") from None

    def renew(self, site, owner, lease_seconds):
        try:
            return bool(self.client.eval(_RENEW_LUA, 1, self._keys(site)[0], owner, lease_seconds))
        except Exception:
            return False

    def pause(self, site, seconds):
        try:
            self.client.eval(_PAUSE_LUA, 1, self._keys(site)[2], seconds)
        except Exception:
            raise AccessStoreUnavailable("共有待機時間を保存できないため、取得を停止しました。") from None


_store = None
_signature = None
_store_lock = threading.Lock()


def reset_marketplace_access_for_tests():
    global _store, _signature
    with _store_lock:
        _store = _signature = None


def get_access_store():
    global _store, _signature
    url = os.environ.get("REDIS_URL") or os.environ.get("VALKEY_URL")
    signature = (url, is_production_runtime())
    with _store_lock:
        if _store is None or signature != _signature:
            if url:
                _store = RedisAccessStore(url)
            elif signature[1]:
                raise AccessStoreUnavailable("本番の取得には共有Redis/Valkeyが必要です。")
            else:
                _store = MemoryAccessStore()
            _signature = signature
        return _store


@dataclass
class AccessLease:
    site: str
    owner: str
    store: object
    lease_seconds: float = 180
    lost: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    renewal: threading.Thread | None = None
    abandoned: bool = False

    def abandon(self):
        """Leave an uncertain in-flight operation fenced until lease expiry."""
        self.abandoned = True
        self.stop.set()

    def start(self):
        def renew():
            while not self.stop.wait(30):
                if not self.store.renew(self.site, self.owner, self.lease_seconds):
                    self.lost.set()
                    return
        self.renewal = threading.Thread(target=renew, name="marketplace-access-renew", daemon=True)
        self.renewal.start()

    def close(self):
        self.stop.set()
        if self.renewal:
            self.renewal.join(timeout=3)
        if not self.abandoned:
            self.store.release(self.site, self.owner)


def current_access_lease(site_or_url):
    return _held.get().get(resolve_site(site_or_url))


def _prepare(site_or_url, consume, parent):
    site = resolve_site(site_or_url)
    # A copied ContextVar must not let concurrent HTTP child tasks share a
    # browser reservation. Only the navigation guard passes an explicit parent.
    if parent and (parent.site != site or parent.lost.is_set() or parent.stop.is_set()):
        raise AccessLeaseLost("取得処理のロックが失効しました。")
    _check_active(consume=consume)
    store = parent.store if parent else get_access_store()
    interval = _setting(f"{site.upper()}_ACCESS_INTERVAL_SECONDS", 2, 0, 60)
    if is_production_runtime():
        interval = max(1, interval)
    concurrency = int(_setting(f"{site.upper()}_ACCESS_CONCURRENCY", 1, 1, 2))
    return site, parent, store, interval, concurrency


def _waiting(reason, seconds):
    from services.scrape_job_runtime import report_current_job_wait
    report_current_job_wait(reason, seconds)


@contextmanager
def marketplace_access(site_or_url, timeout_seconds=120, consume_request=True, parent_lease=None):
    site, parent, store, interval, concurrency = _prepare(site_or_url, consume_request, parent_lease)
    lease = parent or AccessLease(site, uuid.uuid4().hex, store)
    deadline = time.monotonic() + min(120, max(0, timeout_seconds))
    token = None
    acquired = False
    try:
        while True:
            _check_active()
            wait, reason = store.acquire(site, lease.owner, interval=interval, concurrency=concurrency,
                lease_seconds=lease.lease_seconds, consume=consume_request, parent=parent.owner if parent else None)
            if wait <= 0:
                acquired = True
                break
            _waiting(reason, wait)
            if reason == "site_cooldown":
                raise ScrapeBlockedError(f"アクセスが制限されています。約{int(wait + 0.999)}秒後に再実行してください。")
            if time.monotonic() + min(wait, 0.25) > deadline:
                raise ScrapeBlockedError("サイトの共有アクセス制限により待機中です。後で再実行してください。")
            time.sleep(min(wait, 0.25))
        if not parent:
            lease.start()
            token = _held.set({**_held.get(), site: lease})
        _waiting("", 0)
        yield lease
        _check_active()
        if lease.lost.is_set():
            raise AccessLeaseLost("取得処理のロックが失効しました。")
    finally:
        if token is not None:
            _held.reset(token)
        if acquired and not parent:
            lease.close()


@asynccontextmanager
async def async_marketplace_access(site_or_url, timeout_seconds=120, consume_request=True, parent_lease=None):
    site, parent, store, interval, concurrency = _prepare(site_or_url, consume_request, parent_lease)
    lease = parent or AccessLease(site, uuid.uuid4().hex, store)
    deadline = time.monotonic() + min(120, max(0, timeout_seconds))
    token = None
    acquired = False
    try:
        while True:
            _check_active()
            # Admission must complete before cancellation cleanup. Shielding an
            # acquire without awaiting it would leak an untracked owned lease.
            kwargs = dict(interval=interval, concurrency=concurrency,
                lease_seconds=lease.lease_seconds, consume=consume_request,
                parent=parent.owner if parent else None)
            if isinstance(store, MemoryAccessStore):
                wait, reason = store.acquire(site, lease.owner, **kwargs)
            else:
                task = asyncio.create_task(asyncio.to_thread(store.acquire, site, lease.owner, **kwargs))
                try:
                    # The timeout is a polling wakeup, not an admission timeout.
                    # It also works on runtimes that delay executor wakeups.
                    while not task.done():
                        await asyncio.wait({task}, timeout=0.1)
                    wait, reason = task.result()
                except asyncio.CancelledError:
                    while not task.done():
                        try:
                            await asyncio.wait({task}, timeout=0.1)
                        except asyncio.CancelledError:
                            continue
                    wait, _ = task.result()
                    if wait <= 0 and not parent:
                        store.release(site, lease.owner)
                    raise
            if wait <= 0:
                acquired = True
                break
            _waiting(reason, wait)
            if reason == "site_cooldown":
                raise ScrapeBlockedError(f"アクセスが制限されています。約{int(wait + 0.999)}秒後に再実行してください。")
            if time.monotonic() + min(wait, 0.25) > deadline:
                raise ScrapeBlockedError("サイトの共有アクセス制限により待機中です。後で再実行してください。")
            await asyncio.sleep(min(wait, 0.25))
        if not parent:
            lease.start()
            token = _held.set({**_held.get(), site: lease})
        _waiting("", 0)
        yield lease
        _check_active()
        if lease.lost.is_set():
            raise AccessLeaseLost("取得処理のロックが失効しました。")
    finally:
        if token is not None:
            _held.reset(token)
        if acquired and not parent:
            # Bounded close also avoids leaving an executor cleanup behind when
            # a short-lived browser event loop is shutting down.
            lease.close()


def observe_access_response(site_or_url, response_or_status, headers=None, body=""):
    from services.scrape_safety import _looks_blocked, page_text, response_header, response_status, ScrapeHttpError
    site = resolve_site(site_or_url)
    if isinstance(response_or_status, int):
        status = response_or_status
        retry_after = str((headers or {}).get("Retry-After") or (headers or {}).get("retry-after") or "")
    else:
        status = response_status(response_or_status)
        retry_after = response_header(response_or_status, "Retry-After")
        body = body or page_text(response_or_status)
    if status == 429:
        try:
            delay = float(retry_after)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 60
        delay = min(3600, max(60, delay))
        get_access_store().pause(site, delay)
        raise ScrapeBlockedError("サイトの取得制限 (HTTP 429) により待機します。", status_code=429)
    waf_action = str((headers or {}).get("x-amzn-waf-action") or "").lower()
    if not isinstance(response_or_status, int):
        waf_action = response_header(response_or_status, "x-amzn-waf-action").lower()
    text = str(body or "").lower()
    waf_markers = ("awswafcaptcha", "window.gokuprops", "awswafintegration", "token.awswaf", "captcha.awswaf")
    if status in {401, 403} or waf_action in {"challenge", "captcha"} or _looks_blocked(text) or any(marker in text for marker in waf_markers):
        get_access_store().pause(site, 600)
        raise ScrapeBlockedError(f"サイトのアクセス拒否 (HTTP {status}) を確認したため、取得を停止しました。", status_code=status)
    if status is not None and status >= 500:
        get_access_store().pause(site, 30)
        raise ScrapeHttpError(f"取得先の一時障害 (HTTP {status}) により待機します。", status_code=status)
