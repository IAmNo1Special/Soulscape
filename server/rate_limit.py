import threading
import time

from fastapi import Depends, HTTPException, Request

from .security import UserIdentity, get_api_key


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def check(self, key: str, limit: int, window_seconds: float) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            self._calls += 1
            hits = self._hits.get(key)
            if hits is None:
                hits = []
                self._hits[key] = hits
            cutoff = now - window_seconds
            while hits and hits[0] <= cutoff:
                hits.pop(0)
            if len(hits) >= limit:
                retry_after = max(0.0, hits[0] + window_seconds - now)
                if self._calls % 512 == 0:
                    self._prune(cutoff)
                return False, retry_after
            hits.append(now)
            if self._calls % 512 == 0:
                self._prune(cutoff)
            return True, 0.0

    def _prune(self, cutoff: float) -> None:
        empty = [
            key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff
        ]
        for key in empty:
            del self._hits[key]


limiter = SlidingWindowLimiter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limited(limit: int, window_seconds: int, scope: str):
    async def _guard(
        request: Request, identity: UserIdentity = Depends(get_api_key)
    ) -> None:
        if identity.is_operator:
            return
        key = f"{scope}:{identity.id}:{_client_ip(request)}"
        allowed, retry_after = limiter.check(key, limit, window_seconds)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

    return _guard


read_limit = rate_limited(120, 60, "reads")
social_write_limit = rate_limited(6, 60, "social_write")
market_write_limit = rate_limited(30, 60, "market_write")


def check_login_limit(username: str, ip: str) -> None:
    allowed, retry_after = limiter.check(f"login:{username}:{ip}", 10, 60)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
