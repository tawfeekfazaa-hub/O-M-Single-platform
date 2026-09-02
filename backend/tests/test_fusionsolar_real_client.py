"""Real-client protocol tests against an in-process fake server
(httpx.MockTransport) — transport, auth, session, and error-mapping
behaviour, all offline. No real credentials, no real plant data."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.adapters.base import (
    AdapterAuthError,
    AdapterError,
    AdapterProtocolError,
    AdapterRateLimitError,
    AdapterTransientError,
)
from app.adapters.fusionsolar.adapter import FusionSolarAdapter
from app.adapters.fusionsolar.client import XSRF_HEADER, RealFusionSolarClient
from app.adapters.fusionsolar.policy import FusionSolarRatePolicy

BASE_URL = "https://fake.fusionsolar.example/thirdData"
ORIGIN_HOST = "fake.fusionsolar.example"
USERNAME = "nb-user"
SYSTEM_CODE = "nb-system-code"
TOKEN = "token-abc"

STATIONS = [{"stationCode": "NE=1", "stationName": "P1", "capacity": 1.0, "stationAddr": "X"}]


def envelope(data: Any, *, success: bool = True, fail_code: int = 0, **extra: Any) -> dict:
    return {"success": success, "failCode": fail_code, "data": data, **extra}


class FakeFusionSolar:
    """Minimal Northbound API double with scriptable failures."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.hosts_seen: set[str] = set()
        self.fail_login = False
        self.rate_limit_all = False
        self.expire_session_times = 0  # how many calls answer failCode 305
        self.http_status: int | None = None
        self.retry_after: str | None = None
        self.raise_transport: Exception | None = None
        self.body_override: str | None = None
        self.token_in_cookie = False
        self.omit_token = False
        self.redirect_to: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts_seen.add(request.url.host)
        path = request.url.path.removeprefix("/thirdData")
        body = json.loads(request.content or b"{}")
        self.requests.append((path, body))

        if self.raise_transport is not None:
            raise self.raise_transport
        if self.redirect_to is not None:
            return httpx.Response(302, headers={"Location": self.redirect_to})
        if self.http_status is not None:
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return httpx.Response(self.http_status, headers=headers, json={})
        if self.body_override is not None:
            return httpx.Response(200, content=self.body_override)
        if self.rate_limit_all:
            return httpx.Response(200, json=envelope(None, success=False, fail_code=407))

        if path == "/login":
            if self.fail_login or body.get("systemCode") != SYSTEM_CODE:
                return httpx.Response(200, json=envelope(None, success=False, fail_code=20001))
            headers: dict[str, str] = {}
            if self.omit_token:
                pass
            elif self.token_in_cookie:
                headers["Set-Cookie"] = f"{XSRF_HEADER}={TOKEN}; Path=/"
            else:
                headers[XSRF_HEADER] = TOKEN
            return httpx.Response(200, json=envelope(None), headers=headers)

        if request.headers.get(XSRF_HEADER) != TOKEN or self.expire_session_times > 0:
            if self.expire_session_times > 0:
                self.expire_session_times -= 1
            return httpx.Response(200, json=envelope(None, success=False, fail_code=305))

        if path == "/getStationList":
            return httpx.Response(200, json=envelope(list(STATIONS)))
        if path == "/getStationRealKpi":
            codes = str(body.get("stationCodes", "")).split(",")
            rows = [
                {"stationCode": c, "dataItemMap": {"day_power": 5.0, "real_health_state": 3}}
                for c in codes
                if c
            ]
            return httpx.Response(
                200, json=envelope(rows, params={"currentTime": 1_780_000_000_000})
            )
        return httpx.Response(404)


def generous_policy() -> FusionSolarRatePolicy:
    policy = FusionSolarRatePolicy(
        login_max_calls=100,
        station_list_max_calls=100,
        station_list_window_seconds=86_400.0,
    )
    policy.set_kpi_plant_count(10_000)
    return policy


def make_client(fake: FakeFusionSolar, policy: FusionSolarRatePolicy | None = None):
    return RealFusionSolarClient(
        base_url=BASE_URL,
        username=USERNAME,
        system_code=SYSTEM_CODE,
        policy=policy or generous_policy(),
        transport=httpx.MockTransport(fake.handler),
    )


# --------------------------------------------------------------------- #
# authentication & token handling                                       #
# --------------------------------------------------------------------- #


async def test_login_token_from_response_header():
    fake = FakeFusionSolar()
    client = make_client(fake)
    result = await client.list_stations()
    assert [s["stationCode"] for s in result.stations] == ["NE=1"]
    assert [p for p, _ in fake.requests] == ["/login", "/getStationList"]
    assert client.is_logged_in()
    await client.close()


async def test_login_token_from_cookie():
    fake = FakeFusionSolar()
    fake.token_in_cookie = True
    client = make_client(fake)
    result = await client.list_stations()
    assert len(result.stations) == 1
    await client.close()


async def test_missing_token_is_auth_error():
    fake = FakeFusionSolar()
    fake.omit_token = True
    client = make_client(fake)
    with pytest.raises(AdapterAuthError):
        await client.login()
    await client.close()


async def test_bad_credentials_raise_auth_error():
    fake = FakeFusionSolar()
    fake.fail_login = True
    client = make_client(fake)
    with pytest.raises(AdapterAuthError):
        await client.login()
    await client.close()


async def test_all_requests_stay_on_configured_origin():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    await client.get_station_real_kpi(["NE=1"])
    assert fake.hosts_seen == {ORIGIN_HOST}
    await client.close()


async def test_redirects_are_not_followed():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.redirect_to = "https://evil.example/steal"
    before = len(fake.requests)
    with pytest.raises(AdapterError):
        await client.get_station_real_kpi(["NE=1"])
    # Exactly one request was made; the redirect target was never called.
    assert len(fake.requests) == before + 1
    assert fake.hosts_seen == {ORIGIN_HOST}
    await client.close()


async def test_session_expiry_triggers_single_relogin():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.expire_session_times = 1
    result = await client.list_stations()
    assert len(result.stations) == 1
    # 305 -> exactly one re-login -> one retry.
    assert [p for p, _ in fake.requests].count("/login") == 2


async def test_repeated_305_does_not_loop():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.expire_session_times = 99
    before_logins = [p for p, _ in fake.requests].count("/login")
    with pytest.raises(AdapterAuthError):
        await client.list_stations()
    after_logins = [p for p, _ in fake.requests].count("/login")
    assert after_logins - before_logins == 1  # one controlled re-login, no loop
    await client.close()


async def test_concurrent_logins_are_single_flight():
    import asyncio

    fake = FakeFusionSolar()
    client = make_client(fake)
    await asyncio.gather(client.login(), client.login(), client.login())
    assert [p for p, _ in fake.requests].count("/login") == 1
    await client.close()


async def test_session_expiry_retry_reuses_the_reserved_budget_slot():
    # With <=100 plants the KPI budget is exactly ONE call per window; the
    # promised post-re-login retry must reuse the slot the rejected attempt
    # already paid for instead of failing on an exhausted budget.
    fake = FakeFusionSolar()
    policy = FusionSolarRatePolicy(login_max_calls=100, station_list_max_calls=100)
    policy.set_kpi_plant_count(1)  # ceil(1/100) -> 1 call per window
    client = make_client(fake, policy)
    await client.login()
    fake.expire_session_times = 1
    result = await client.get_station_real_kpi(["NE=1"])
    assert [r["stationCode"] for r in result.rows] == ["NE=1"]
    assert [p for p, _ in fake.requests].count("/login") == 2  # one re-login
    await client.close()


async def test_adapter_scales_kpi_budget_from_full_plant_count():
    # The policy's KPI budget starts at the 1-call constructor default; the
    # adapter must announce the FULL requested count before its first batch
    # or every multi-batch tenant would be rejected client-side.
    fake = FakeFusionSolar()
    policy = FusionSolarRatePolicy(login_max_calls=100, station_list_max_calls=100)
    client = make_client(fake, policy)
    adapter = FusionSolarAdapter(client, allow_synthetic_fields=False)
    codes = [f"NE={i}" for i in range(150)]
    readings = await adapter.fetch_plant_kpis(codes)
    assert len(readings) == 150
    assert client.call_counts().station_real_kpi == 2  # two sequential batches
    await client.close()


# --------------------------------------------------------------------- #
# rate-limit and transient/protocol error mapping                       #
# --------------------------------------------------------------------- #


async def test_server_fail_code_407_maps_to_rate_limit_with_window_hint():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.rate_limit_all = True
    with pytest.raises(AdapterRateLimitError) as excinfo:
        await client.get_station_real_kpi(["NE=1"])
    assert excinfo.value.retry_after_seconds is not None
    assert excinfo.value.retry_after_seconds >= 300.0  # KPI window lower bound
    # No re-login was attempted in response to 407.
    assert [p for p, _ in fake.requests].count("/login") == 1
    await client.close()


async def test_http_429_with_numeric_retry_after():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.http_status = 429
    fake.retry_after = "120"
    with pytest.raises(AdapterRateLimitError) as excinfo:
        await client.get_station_real_kpi(["NE=1"])
    assert excinfo.value.retry_after_seconds == pytest.approx(120.0)
    await client.close()


async def test_http_429_with_malformed_retry_after_uses_budget_hint():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.http_status = 429
    fake.retry_after = "not-a-delay"
    with pytest.raises(AdapterRateLimitError) as excinfo:
        await client.get_station_real_kpi(["NE=1"])
    assert excinfo.value.retry_after_seconds >= 300.0
    await client.close()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_retryable_5xx_is_transient(status: int):
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.http_status = status
    with pytest.raises(AdapterTransientError):
        await client.get_station_real_kpi(["NE=1"])
    await client.close()


@pytest.mark.parametrize(
    "exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("c")]
)
async def test_timeouts_and_connection_failures_are_transient(exc: Exception):
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.raise_transport = exc
    with pytest.raises(AdapterTransientError):
        await client.get_station_real_kpi(["NE=1"])
    await client.close()


async def test_non_json_body_is_protocol_error():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.body_override = "<html>maintenance</html>"
    with pytest.raises(AdapterProtocolError):
        await client.get_station_real_kpi(["NE=1"])
    await client.close()


async def test_non_object_envelope_is_protocol_error():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.body_override = json.dumps([1, 2, 3])
    with pytest.raises(AdapterProtocolError):
        await client.get_station_real_kpi(["NE=1"])
    await client.close()


# --------------------------------------------------------------------- #
# real-time KPI call shape                                              #
# --------------------------------------------------------------------- #


async def test_kpi_batch_is_comma_separated_and_capped():
    fake = FakeFusionSolar()
    client = make_client(fake)
    result = await client.get_station_real_kpi(["NE=1", "NE=2"])
    assert {r["stationCode"] for r in result.rows} == {"NE=1", "NE=2"}
    _, body = fake.requests[-1]
    assert body == {"stationCodes": "NE=1,NE=2"}
    with pytest.raises(AdapterError):
        await client.get_station_real_kpi([f"NE={i}" for i in range(101)])
    await client.close()


async def test_kpi_vendor_server_time_is_captured():
    fake = FakeFusionSolar()
    client = make_client(fake)
    result = await client.get_station_real_kpi(["NE=1"])
    assert result.vendor_current_time_ms == 1_780_000_000_000
    await client.close()


async def test_kpi_malformed_data_is_protocol_error():
    fake = FakeFusionSolar()
    client = make_client(fake)
    await client.list_stations()
    fake.body_override = json.dumps(envelope({"not": "a list"}))
    with pytest.raises(AdapterProtocolError):
        await client.get_station_real_kpi(["NE=1"])
    await client.close()
