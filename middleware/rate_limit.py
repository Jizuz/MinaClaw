"""
流量检测与限流
- 滑动窗口 QPS（全局 & 按 Key）
- 令牌桶
- 并发计数
- 熔断器
- 每日 Token 配额（由 token_meter 提供实际用量）
"""
import time
import asyncio
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Deque, Tuple

from utils.logger import get_logger, log_extra

log = get_logger("traffic")


class SlidingWindow:
    def __init__(self, window_sec: int = 60):
        self.window = window_sec
        self.events: Deque[float] = deque()

    def add(self, now: float | None = None):
        now = now or time.time()
        self.events.append(now)
        self._evict(now)

    def count(self, now: float | None = None) -> int:
        now = now or time.time()
        self._evict(now)
        return len(self.events)

    def _evict(self, now: float):
        cutoff = now - self.window
        while self.events and self.events[0] < cutoff:
            self.events.popleft()


class TokenBucket:
    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> bool:
        async with self._lock:
            now = time.time()
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


class CircuitBreaker:
    def __init__(self, threshold: int = 10, cool_down: int = 30):
        self.threshold = threshold
        self.cool_down = cool_down
        self.fails = 0
        self.state = "closed"
        self.opened_at = 0.0
        self._lock = asyncio.Lock()

    async def allow(self) -> Tuple[bool, str]:
        async with self._lock:
            now = time.time()
            if self.state == "closed":
                return True, "closed"
            if self.state == "open":
                if now - self.opened_at >= self.cool_down:
                    self.state = "half_open"
                    return True, "half_open_probe"
                return False, "open"
            return True, "half_open"

    async def on_success(self):
        async with self._lock:
            if self.state == "half_open":
                self.state = "closed"
                log.info("circuit closed after successful probe")
            self.fails = 0

    async def on_failure(self):
        async with self._lock:
            self.fails += 1
            if self.state == "half_open":
                self.state = "open"
                self.opened_at = time.time()
                log.warning("circuit opened (half_open probe failed)")
            elif self.fails >= self.threshold and self.state == "closed":
                self.state = "open"
                self.opened_at = time.time()
                log.warning("circuit opened", extra=log_extra(fails=self.fails))


@dataclass
class TrafficConfig:
    global_qps: int = 50
    per_key_qps: int = 10
    global_burst: int = 100
    per_key_burst: int = 20
    max_concurrent: int = 20
    breaker_threshold: int = 10
    breaker_cool_down: int = 30


@dataclass
class TrafficStats:
    total_requests: int = 0
    rejected_requests: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.time)


class TrafficController:
    def __init__(self, config: TrafficConfig | None = None):
        self.cfg = config or TrafficConfig()

        self.global_window = SlidingWindow(60)
        self.global_bucket = TokenBucket(self.cfg.global_qps, self.cfg.global_burst)
        self.breaker = CircuitBreaker(self.cfg.breaker_threshold, self.cfg.breaker_cool_down)

        self.key_windows: Dict[str, SlidingWindow] = defaultdict(SlidingWindow)
        self.key_buckets: Dict[str, TokenBucket] = {}
        self.concurrent = 0
        self._concurrent_lock = asyncio.Lock()
        self.stats = TrafficStats()

    async def check(self, api_key: str) -> Tuple[bool, str]:
        self.stats.total_requests += 1

        ok, state = await self.breaker.allow()
        if not ok:
            self.stats.rejected_requests += 1
            log.warning("reject circuit_open",
                        extra=log_extra(state=state))
            return False, f"circuit_open({state})"

        if self.global_window.count() >= self.cfg.global_qps:
            self.stats.rejected_requests += 1
            log.warning("reject global_qps_exceeded")
            return False, "global_qps_exceeded"

        kw = self.key_windows[api_key]
        if kw.count() >= self.cfg.per_key_qps:
            self.stats.rejected_requests += 1
            log.warning("reject key_qps_exceeded")
            return False, "key_qps_exceeded"

        if not await self.global_bucket.acquire():
            self.stats.rejected_requests += 1
            log.warning("reject global_rate_limited")
            return False, "global_rate_limited"

        kb = self.key_buckets.get(api_key)
        if kb is None:
            kb = TokenBucket(self.cfg.per_key_qps, self.cfg.per_key_burst)
            self.key_buckets[api_key] = kb
        if not await kb.acquire():
            self.stats.rejected_requests += 1
            log.warning("reject key_rate_limited")
            return False, "key_rate_limited"

        async with self._concurrent_lock:
            if self.concurrent >= self.cfg.max_concurrent:
                self.stats.rejected_requests += 1
                log.warning("reject concurrency_exceeded",
                            extra=log_extra(concurrent=self.concurrent))
                return False, "concurrency_exceeded"
            self.concurrent += 1

        now = time.time()
        self.global_window.add(now)
        kw.add(now)
        return True, "ok"

    async def release(self, success: bool):
        async with self._concurrent_lock:
            self.concurrent = max(0, self.concurrent - 1)
        if success:
            await self.breaker.on_success()
        else:
            self.stats.errors += 1
            await self.breaker.on_failure()

    def snapshot(self) -> dict:
        return {
            "qps_60s": self.global_window.count(),
            "concurrent": self.concurrent,
            "breaker_state": self.breaker.state,
            "breaker_fails": self.breaker.fails,
            "total_requests": self.stats.total_requests,
            "rejected_requests": self.stats.rejected_requests,
            "reject_rate": round(self.stats.rejected_requests / max(1, self.stats.total_requests), 3),
            "errors": self.stats.errors,
            "uptime_sec": round(time.time() - self.stats.started_at, 1),
            "keys_active": len(self.key_windows),
        }


traffic = TrafficController()