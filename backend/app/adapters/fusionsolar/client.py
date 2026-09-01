"""FusionSolar Northbound API clients.

Both clients speak the *vendor* payload shapes; the adapter maps them to
our normalized model. The real client is the only place in the codebase
that performs vendor HTTP calls, and every call goes through the shared
rate limiter (~5 calls / 10 min / user, failCode 407 — docs/API-NOTES.md).
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.adapters.base import AdapterAuthError, AdapterError, AdapterRateLimitError
from app.core.ratelimit import RateLimitExceeded, RollingWindowRateLimiter

# FusionSolar failCodes we handle explicitly.
FAIL_CODE_NOT_LOGGED_IN = 305
FAIL_CODE_RATE_LIMITED = 407

XSRF_HEADER = "XSRF-TOKEN"


class FusionSolarClient(Protocol):
    """Vendor-shaped operations shared by the mock and real clients."""

    async def login(self) -> None: ...

    async def get_station_list(self) -> list[dict[str, Any]]: ...

    async def get_station_real_kpi(self, station_codes: list[str]) -> list[dict[str, Any]]: ...

    def is_logged_in(self) -> bool: ...

    async def close(self) -> None: ...


class RealFusionSolarClient:
    """Talks to the FusionSolar Northbound API over HTTPS.

    Session rules (docs/API-NOTES.md): single session per user, XSRF token
    from /login must accompany every call, re-login on failCode 305. Login
    calls consume rate budget too, so they also acquire the limiter.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        rate_limiter: RollingWindowRateLimiter,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._username = username
        self._password = password
        self._limiter = rate_limiter
        self._token: str | None = None
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    def is_logged_in(self) -> bool:
        return self._token is not None

    async def _post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        try:
            await self._limiter.acquire(wait=False)
        except RateLimitExceeded as exc:
            raise AdapterRateLimitError(
                "client-side FusionSolar rate budget exhausted",
                retry_after_seconds=exc.retry_after_seconds,
            ) from exc

        headers = {XSRF_HEADER: self._token} if self._token else {}
        try:
            response = await self._http.post(path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise AdapterError(f"FusionSolar request failed: {exc}") from exc

        if response.status_code != 200:
            raise AdapterError(f"FusionSolar HTTP {response.status_code} on {path}")
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise AdapterError(f"FusionSolar returned non-JSON body on {path}") from exc

        if path == "/login" and payload.get("success"):
            token = response.headers.get(XSRF_HEADER) or response.cookies.get(XSRF_HEADER)
            if not token:
                raise AdapterAuthError("FusionSolar login succeeded but returned no XSRF token")
            self._token = token
        return payload

    @staticmethod
    def _fail_code(payload: dict[str, Any]) -> int:
        try:
            return int(payload.get("failCode") or 0)
        except (TypeError, ValueError):
            return 0

    async def _call(self, path: str, json: dict[str, Any], *, retry_auth: bool = True) -> Any:
        """POST with envelope handling: 407 -> rate limit, 305 -> re-login once."""
        payload = await self._post(path, json)
        if payload.get("success"):
            return payload.get("data")

        fail_code = self._fail_code(payload)
        if fail_code == FAIL_CODE_RATE_LIMITED:
            raise AdapterRateLimitError("FusionSolar failCode 407 (access frequency too high)")
        if fail_code == FAIL_CODE_NOT_LOGGED_IN and retry_auth:
            self._token = None
            await self.login()
            return await self._call(path, json, retry_auth=False)
        raise AdapterError(f"FusionSolar call {path} failed (failCode={fail_code})")

    async def login(self) -> None:
        payload = await self._post(
            "/login",
            {"userName": self._username, "systemCode": self._password},
        )
        if not payload.get("success"):
            fail_code = self._fail_code(payload)
            if fail_code == FAIL_CODE_RATE_LIMITED:
                raise AdapterRateLimitError("FusionSolar rate-limited the login call")
            raise AdapterAuthError(f"FusionSolar login failed (failCode={fail_code})")

    async def get_station_list(self) -> list[dict[str, Any]]:
        if not self._token:
            await self.login()
        data = await self._call("/getStationList", {})
        return list(data or [])

    async def get_station_real_kpi(self, station_codes: list[str]) -> list[dict[str, Any]]:
        if not station_codes:
            return []
        if not self._token:
            await self.login()
        data = await self._call("/getStationRealKpi", {"stationCodes": ",".join(station_codes)})
        return list(data or [])

    async def close(self) -> None:
        await self._http.aclose()
