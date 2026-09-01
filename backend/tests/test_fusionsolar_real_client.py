"""Real-client protocol tests against an in-process fake server (httpx
MockTransport) — envelope handling, session, and rate-limit behaviour are
exercised without any network or credentials."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.adapters.base import AdapterAuthError, AdapterRateLimitError
from app.adapters.fusionsolar.client import XSRF_HEADER, RealFusionSolarClient
from app.core.ratelimit import RollingWindowRateLimiter

BASE_URL = "https://fake.fusionsolar.example/thirdData"
USERNAME = "nb-user"
PASSWORD = "nb-pass"
TOKEN = "token-abc"

STATIONS = [{"stationCode": "NE=1", "stationName": "P1", "capacity": 1.0, "stationAddr": "X"}]


class FakeFusionSolar:
    """Minimal Northbound API double with scriptable failures."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.fail_login = False
        self.rate_limit_all = False
        self.expire_session_once = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/thirdData")
        body = json.loads(request.content or b"{}")
        self.requests.append((path, body))

        if self.rate_limit_all:
            return httpx.Response(200, json={"success": False, "failCode": 407})

        if path == "/login":
            if self.fail_login or body.get("systemCode") != PASSWORD:
                return httpx.Response(200, json={"success": False, "failCode": 20001})
            return httpx.Response(
                200, json={"success": True, "failCode": 0}, headers={XSRF_HEADER: TOKEN}
            )

        if request.headers.get(XSRF_HEADER) != TOKEN or self.expire_session_once:
            self.expire_session_once = False
            return httpx.Response(200, json={"success": False, "failCode": 305})

        if path == "/getStationList":
            return httpx.Response(200, json={"success": True, "failCode": 0, "data": STATIONS})
        if path == "/getStationRealKpi":
            codes = str(body.get("stationCodes", "")).split(",")
            data = [
                {"stationCode": c, "dataItemMap": {"day_power": 5.0, "real_health_state": 3}}
                for c in codes
                if c
            ]
            return httpx.Response(200, json={"success": True, "failCode": 0, "data": data})
        return httpx.Response(404)


def make_client(fake: FakeFusionSolar, max_calls: int = 10) -> RealFusionSolarClient:
    return RealFusionSolarClient(
        base_url=BASE_URL,
        username=USERNAME,
        password=PASSWORD,
        rate_limiter=RollingWindowRateLimiter(max_calls, 600.0),
        transport=httpx.MockTransport(fake.handler),
    )


async def test_login_then_authenticated_call():
    fake = FakeFusionSolar()
    client = make_client(fake)
    stations = await client.get_station_list()
    assert stations == STATIONS
    assert [p for p, _ in fake.requests] == ["/login", "/getStationList"]
    assert client.is_logged_in()
    await client.close()


async def test_station_kpi_batches_codes_comma_separated():
    fake = FakeFusionSolar()
    client = make_client(fake)
    rows = await client.get_station_real_kpi(["NE=1", "NE=2"])
    assert {r["stationCode"] for r in rows} == {"NE=1", "NE=2"}
    _, body = fake.requests[-1]
    assert body == {"stationCodes": "NE=1,NE=2"}
    await client.close()


async def test_server_407_raises_rate_limit_error():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.get_station_list()  # establish session first
    fake.rate_limit_all = True
    with pytest.raises(AdapterRateLimitError):
        await client.get_station_list()
    await client.close()


async def test_session_expiry_triggers_single_relogin():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.get_station_list()
    fake.expire_session_once = True
    stations = await client.get_station_list()
    assert stations == STATIONS
    # 305 -> re-login -> retried call.
    assert [p for p, _ in fake.requests] == [
        "/login",
        "/getStationList",
        "/getStationList",
        "/login",
        "/getStationList",
    ]
    await client.close()


async def test_client_side_budget_counts_login_calls():
    fake = FakeFusionSolar()
    client = make_client(fake, max_calls=1)
    with pytest.raises(AdapterRateLimitError) as excinfo:
        await client.get_station_list()  # login consumes the only slot
    assert excinfo.value.retry_after_seconds is not None
    # Only the login reached the "server".
    assert [p for p, _ in fake.requests] == ["/login"]
    await client.close()


async def test_bad_credentials_raise_auth_error():
    fake = FakeFusionSolar()
    fake.fail_login = True
    client = make_client(fake)
    with pytest.raises(AdapterAuthError):
        await client.login()
    await client.close()
