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
from pathlib import Path
import re
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
_process_generation: ContextVar[str | None] = ContextVar("marketplace_process_generation", default=None)
_quarantined_generations: set[str] = set()
_quarantine_lock = threading.Lock()
_local_generation = None


def _process_start_ticks(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError) as exc:
        raise AccessStoreUnavailable("旧プロセスの状態を確認できません。隔離状態を維持します。") from exc


def _local_process_generation() -> str:
    global _local_generation
    pid = os.getpid()
    with _quarantine_lock:
        if _local_generation is None or not _local_generation.startswith((f"local-{pid}-", f"portable-{pid}-")):
            ticks = _process_start_ticks(pid) if Path("/proc").is_dir() else None
            _local_generation = (f"local-{pid}-{ticks}-{uuid.uuid4().hex}" if ticks is not None
                                 else f"portable-{pid}-{uuid.uuid4().hex}")
        return _local_generation


def has_marketplace_generation_quarantine(generation: str) -> bool:
    """Observe uncertainty even if a site adapter converted its exception."""
    with _quarantine_lock:
        return generation in _quarantined_generations


@contextmanager
def marketplace_process_generation(generation: str):
    """Identify a supervisor-owned process tree, never a mutable global PID."""
    generation = str(generation)
    if not generation or ":" in generation:
        raise ValueError("A nonempty generation without ':' is required")
    token = _process_generation.set(generation)
    try:
        yield
    finally:
        _process_generation.reset(token)


def _new_owner() -> str:
    generation = _process_generation.get() or _local_process_generation()
    return f"{generation}:{uuid.uuid4().hex}"


def inspect_marketplace_quarantines(*, sites=None) -> dict:
    """Private read-only operator API; do not expose ownership in public pages."""
    store = get_access_store()
    result = {}
    for site in _checked_quarantine_sites(sites):
        entries = []
        for owner in store.inspect_quarantines(site):
            generation = owner.partition(":")[0] if ":" in owner else None
            local = re.fullmatch(r"local-(\d+)-(\d+)-[a-f0-9]{32}", generation or "")
            entries.append({
                "owner": owner,
                "generation": generation,
                "owner_process": {"pid": int(local[1]), "start_ticks": int(local[2])} if local else None,
            })
        result[site] = entries
    return result


@dataclass(frozen=True)
class QuarantineStopProof:
    generation: str
    owner_identity: tuple[int, int]
    resource_identities: tuple[tuple[int, int], ...]
    evidence_reference: str


def clear_marketplace_owner_quarantine(site, owner, *, proof: QuarantineStopProof):
    """Explicit operator recovery after approved, verified resource termination.

    The caller must supply the complete recorded tree and its evidence reference.
    We independently reject any still-live recorded identity. Anonymous legacy
    owners cannot be safely mapped here and remain quarantined.
    """
    generation, separator, _ = str(owner).partition(":")
    local = re.fullmatch(r"local-(\d+)-(\d+)-[a-f0-9]{32}", generation)
    if (not separator or not local or not isinstance(proof, QuarantineStopProof)
            or proof.generation != generation or not isinstance(proof.evidence_reference, str)
            or not proof.evidence_reference.strip()
            or proof.owner_identity != (int(local[1]), int(local[2]))
            or proof.owner_identity not in proof.resource_identities):
        raise ValueError("Complete owner/resource termination proof is required")
    for pid, ticks in proof.resource_identities:
        if type(pid) is not int or type(ticks) is not int:
            raise ValueError("Process identities require integer PID/start ticks")
        if pid <= 0 or ticks <= 0 or _process_start_ticks(pid) == ticks:
            raise AccessLeaseLost("旧取得プロセスの終了を確認できません。隔離状態を維持します。")
    return get_access_store().clear_owner_quarantine(resolve_site(site), str(owner))


def _checked_quarantine_sites(sites):
    return tuple(sorted(_SITES if sites is None else {resolve_site(site) for site in sites}))


def clear_marketplace_generation_quarantines(generation: str, *, sites=None, deadline=None) -> int:
    """Only call after the supervisor has verified this entire tree is gone.

    Clearing generation A does not release or change a later owner's lease or
    quarantine. An unverifiable tree must remain blocked, even after lease TTL.
    """
    if not generation or ":" in str(generation):
        raise ValueError("Invalid process generation")
    store = get_access_store()
    cleared = 0
    for site in _checked_quarantine_sites(sites):
        if deadline is not None and time.monotonic() >= deadline:
            raise AccessStoreUnavailable("隔離解除の期限に達しました。未解除の隔離状態を維持します。")
        cleared += store.clear_generation_quarantine(site, str(generation), deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            raise AccessStoreUnavailable("隔離解除の期限内完了を確認できません。取得を再開しません。")
    with _quarantine_lock:
        _quarantined_generations.discard(str(generation))
    return cleared


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
    from services.operation_deadline import check_deadline
    check_deadline()
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
            if state.get("quarantines"):
                raise AccessLeaseLost("停止を確認できない旧取得処理があるため、取得を停止しています。")
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

    def quarantine(self, site, owner):
        with self.lock:
            state = self.states.setdefault(site, {"leases": {}, "next": 0.0, "pause": 0.0})
            state.setdefault("quarantines", set()).add(owner)

    def inspect_quarantines(self, site):
        with self.lock:
            return sorted(self.states.get(site, {}).get("quarantines", set()))

    def clear_owner_quarantine(self, site, owner):
        with self.lock:
            state = self.states.get(site, {})
            quarantines = state.get("quarantines", set())
            removed = int(owner in quarantines)
            quarantines.discard(owner)
            state.get("leases", {}).pop(owner, None)
            return removed

    def clear_generation_quarantine(self, site, generation, *, deadline=None):
        with self.lock:
            quarantines = self.states.get(site, {}).get("quarantines", set())
            owned = {owner for owner in quarantines if owner.startswith(generation + ":")}
            quarantines.difference_update(owned)
            # The old process can no longer use its lease. Clear only its exact
            # generation tokens; never touch successor tokens.
            leases = self.states.get(site, {}).get("leases", {})
            for owner in list(leases):
                if owner.startswith(generation + ":"):
                    del leases[owner]
            return len(owned)


_ACQUIRE_LUA = """
local t=redis.call('TIME'); local now=tonumber(t[1])+tonumber(t[2])/1000000
if redis.call('SCARD',KEYS[4])>0 then return {-1,4} end
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
_CLEAR_GENERATION_LUA = """
local prefix=ARGV[1]..':'
local removed=0
for _,owner in ipairs(redis.call('SMEMBERS',KEYS[1])) do
 if string.sub(owner,1,string.len(prefix))==prefix then
  removed=removed+redis.call('SREM',KEYS[1],owner)
 end
end
for _,owner in ipairs(redis.call('ZRANGE',KEYS[2],0,-1)) do
 if string.sub(owner,1,string.len(prefix))==prefix then redis.call('ZREM',KEYS[2],owner) end
end
return removed
"""
_CLEAR_OWNER_LUA = """
local removed=redis.call('SREM',KEYS[1],ARGV[1])
redis.call('ZREM',KEYS[2],ARGV[1])
return removed
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
        return [prefix + ":leases", prefix + ":next", prefix + ":pause", prefix + ":quarantines"]

    def acquire(self, site, owner, *, interval, concurrency, lease_seconds, consume, parent=None):
        try:
            wait_ms, reason = self.client.eval(_ACQUIRE_LUA, 4, *self._keys(site), owner,
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

    def quarantine(self, site, owner):
        try:
            # No expiry: 180s lease expiration is not proof that Chromium died.
            self.client.sadd(self._keys(site)[3], owner)
        except Exception:
            raise AccessStoreUnavailable("旧取得処理の停止を確認できず、隔離状態の保存にも失敗しました。") from None

    def inspect_quarantines(self, site):
        try:
            return sorted(owner.decode() if isinstance(owner, bytes) else str(owner)
                          for owner in self.client.smembers(self._keys(site)[3]))
        except Exception:
            raise AccessStoreUnavailable("取得処理の隔離状態を確認できません。") from None

    def clear_owner_quarantine(self, site, owner):
        try:
            keys = self._keys(site)
            return int(self.client.eval(_CLEAR_OWNER_LUA, 2, keys[3], keys[0], owner))
        except Exception:
            raise AccessStoreUnavailable("所有者指定の隔離解除を確認できません。") from None

    def clear_generation_quarantine(self, site, generation, *, deadline=None):
        dedicated = None
        try:
            keys = self._keys(site)
            client = self.client
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AccessStoreUnavailable("隔離解除の期限に達しました。")
                import redis
                options = dict(self.client.connection_pool.connection_kwargs)
                # Never mutate the shared pool: other requests may be using it.
                options["socket_timeout"] = min(2.0, remaining / 2)
                options["socket_connect_timeout"] = min(2.0, remaining / 2)
                options["retry_on_timeout"] = False
                options.pop("retry", None)
                pool = redis.ConnectionPool(connection_class=self.client.connection_pool.connection_class, **options)
                dedicated = client = redis.Redis(connection_pool=pool)
            return int(client.eval(_CLEAR_GENERATION_LUA, 2, keys[3], keys[0], generation))
        except Exception:
            raise AccessStoreUnavailable("停止済み取得プロセスの隔離解除を確認できません。") from None
        finally:
            if dedicated is not None:
                dedicated.close()
                dedicated.connection_pool.disconnect()


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
    quarantine_persisted: bool = False

    def abandon(self):
        """Compatibility alias: uncertainty must outlive the renewable lease."""
        self.quarantine()

    def quarantine(self):
        """Persist a site fence before giving up ownership of an uncertain RPC."""
        self.abandoned = True
        self.lost.set()
        if ":" in self.owner:
            with _quarantine_lock:
                _quarantined_generations.add(self.owner.split(":", 1)[0])
        self.store.quarantine(self.site, self.owner)
        self.quarantine_persisted = True
        # Keep renewing until close, so a failed quarantine write is not
        # silently followed by voluntary lease expiration.

    def start(self):
        def renew():
            while not self.stop.wait(30):
                if not self.store.renew(self.site, self.owner, self.lease_seconds):
                    self.lost.set()
                    return
        self.renewal = threading.Thread(target=renew, name="marketplace-access-renew", daemon=True)
        self.renewal.start()

    def close(self):
        if self.abandoned and not self.quarantine_persisted:
            # A Redis outage cannot be made safe by voluntarily dropping the
            # lease. The supervisor must stop this process generation; retain
            # any usable renewal until that hard stop rather than expiry alone.
            return
        self.stop.set()
        if self.renewal:
            self.renewal.join(timeout=3)
        if not self.abandoned:
            self.store.release(self.site, self.owner)


def current_access_lease(site_or_url):
    return _held.get().get(resolve_site(site_or_url))


def _prepare(site_or_url, parent):
    site = resolve_site(site_or_url)
    # A copied ContextVar must not let concurrent HTTP child tasks share a
    # browser reservation. Only the navigation guard passes an explicit parent.
    if parent and (parent.site != site or parent.lost.is_set() or parent.stop.is_set()):
        raise AccessLeaseLost("取得処理のロックが失効しました。")
    # Waiting or a refused admission is not a marketplace request. Consume
    # the shared job budget only after the owned lease has been granted.
    _check_active()
    store = parent.store if parent else get_access_store()
    interval = _setting(f"{site.upper()}_ACCESS_INTERVAL_SECONDS", 2, 0, 60)
    if is_production_runtime():
        interval = max(1, interval)
    concurrency = int(_setting(f"{site.upper()}_ACCESS_CONCURRENCY", 1, 1, 2))
    return site, parent, store, interval, concurrency


def _waiting(reason, seconds):
    from services.scrape_job_runtime import report_current_job_wait
    report_current_job_wait(reason, seconds)


def _with_retry_delay(error, seconds):
    # Structured metadata lets background ledgers respect a shared pause
    # without parsing a translated error message. Constructors stay unchanged.
    error.retry_after_seconds = max(0, float(seconds))
    return error


@contextmanager
def marketplace_access(site_or_url, timeout_seconds=120, consume_request=True, parent_lease=None):
    site, parent, store, interval, concurrency = _prepare(site_or_url, parent_lease)
    lease = parent or AccessLease(site, _new_owner(), store)
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
                raise _with_retry_delay(ScrapeBlockedError(f"アクセスが制限されています。約{int(wait + 0.999)}秒後に再実行してください。"), wait)
            if time.monotonic() + min(wait, 0.25) > deadline:
                raise ScrapeBlockedError("サイトの共有アクセス制限により待機中です。後で再実行してください。")
            time.sleep(min(wait, 0.25))
        if not parent:
            lease.start()
            token = _held.set({**_held.get(), site: lease})
        _waiting("", 0)
        _check_active(consume=consume_request)
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
    site, parent, store, interval, concurrency = _prepare(site_or_url, parent_lease)
    lease = parent or AccessLease(site, _new_owner(), store)
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
                raise _with_retry_delay(ScrapeBlockedError(f"アクセスが制限されています。約{int(wait + 0.999)}秒後に再実行してください。"), wait)
            if time.monotonic() + min(wait, 0.25) > deadline:
                raise ScrapeBlockedError("サイトの共有アクセス制限により待機中です。後で再実行してください。")
            await asyncio.sleep(min(wait, 0.25))
        if not parent:
            lease.start()
            token = _held.set({**_held.get(), site: lease})
        _waiting("", 0)
        _check_active(consume=consume_request)
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
        raise _with_retry_delay(ScrapeBlockedError("サイトの取得制限 (HTTP 429) により待機します。", status_code=429), delay)
    waf_action = str((headers or {}).get("x-amzn-waf-action") or "").lower()
    if not isinstance(response_or_status, int):
        waf_action = response_header(response_or_status, "x-amzn-waf-action").lower()
    text = str(body or "").lower()
    waf_markers = ("awswafcaptcha", "window.gokuprops", "awswafintegration", "token.awswaf", "captcha.awswaf")
    if status in {401, 403} or waf_action in {"challenge", "captcha"} or _looks_blocked(text) or any(marker in text for marker in waf_markers):
        get_access_store().pause(site, 600)
        raise _with_retry_delay(ScrapeBlockedError(f"サイトのアクセス拒否 (HTTP {status}) を確認したため、取得を停止しました。", status_code=status), 600)
    if status is not None and status >= 500:
        get_access_store().pause(site, 30)
        raise _with_retry_delay(ScrapeHttpError(f"取得先の一時障害 (HTTP {status}) により待機します。", status_code=status), 30)
