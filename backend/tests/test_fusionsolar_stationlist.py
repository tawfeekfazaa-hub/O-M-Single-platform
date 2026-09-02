"""Station-list contract tests on /thirdData/getStationList: both
documented response variants, full pagination, guards, de-duplication.
All offline via httpx.MockTransport; no other endpoint is ever tried."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.adapters.base import AdapterProtocolError
from app.adapters.fusionsolar.client import XSRF_HEADER, RealFusionSolarClient
from app.adapters.fusionsolar.policy import FusionSolarRatePolicy

BASE_URL = "https://fake.fusionsolar.example/thirdData"
TOKEN = "token-abc"


def station(i: int, **overrides: Any) -> dict[str, Any]:
    row = {
        "stationCode": f"NE={i}",
        "stationName": f"Plant {i}",
        "capacity": 1.0,
        "stationAddr": "addr",
    }
    row.update(overrides)
    return row


class StationListServer:
    """Serves login plus a scriptable /getStationList."""

    def __init__(self, pages: list[list[dict[str, Any]]] | None, direct: list | None = None):
        self.pages = pages
        self.direct = direct
        self.page_count_override: Any = "auto"
        self.omit_page_count = False
        self.data_override: Any = None
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/thirdData")
        body = json.loads(request.content or b"{}")
        if path == "/login":
            return httpx.Response(
                200, json={"success": True, "failCode": 0}, headers={XSRF_HEADER: TOKEN}
            )
        assert path == "/getStationList", f"unexpected endpoint called: {path}"
        self.requests.append(body)

        if self.data_override is not None:
            data = self.data_override
        elif self.direct is not None:
            data = self.direct
        else:
            page_no = int(body.get("pageNo", 1))
            pages = self.pages or [[]]
            rows = pages[page_no - 1] if 1 <= page_no <= len(pages) else []
            data = {
                "list": rows,
                "pageNo": page_no,
                "pageSize": int(body.get("pageSize", 100)),
                "total": sum(len(p) for p in pages),
            }
            if not self.omit_page_count:
                data["pageCount"] = (
                    len(pages) if self.page_count_override == "auto" else self.page_count_override
                )
        return httpx.Response(200, json={"success": True, "failCode": 0, "data": data})


def make_client(server: StationListServer, max_pages: int = 50) -> RealFusionSolarClient:
    policy = FusionSolarRatePolicy(
        login_max_calls=100, station_list_max_calls=1000, station_list_window_seconds=86_400.0
    )
    return RealFusionSolarClient(
        base_url=BASE_URL,
        username="nb-user",
        system_code="nb-system-code",
        policy=policy,
        transport=httpx.MockTransport(server.handler),
        max_station_list_pages=max_pages,
    )


async def test_request_sends_page_no_from_one_and_page_size_100():
    server = StationListServer(pages=[[station(1)]])
    client = make_client(server)
    await client.list_stations()
    assert server.requests[0] == {"pageNo": 1, "pageSize": 100}
    await client.close()


async def test_legacy_direct_list_variant():
    server = StationListServer(pages=None, direct=[station(1), station(2)])
    client = make_client(server)
    result = await client.list_stations()
    assert result.variant == "direct_list"
    assert result.pages_retrieved == 1
    assert [s["stationCode"] for s in result.stations] == ["NE=1", "NE=2"]
    assert len(server.requests) == 1  # a direct list is complete: no second call
    await client.close()


async def test_zero_plants_direct_list():
    server = StationListServer(pages=None, direct=[])
    client = make_client(server)
    result = await client.list_stations()
    assert result.stations == [] and result.variant == "direct_list"
    await client.close()


async def test_zero_plants_paginated():
    server = StationListServer(pages=[[]])
    client = make_client(server)
    result = await client.list_stations()
    assert result.stations == [] and result.variant == "paginated"
    await client.close()


async def test_single_paginated_page():
    server = StationListServer(pages=[[station(i) for i in range(5)]])
    client = make_client(server)
    result = await client.list_stations()
    assert result.variant == "paginated" and result.pages_retrieved == 1
    assert len(result.stations) == 5
    await client.close()


async def test_exactly_100_on_one_page():
    server = StationListServer(pages=[[station(i) for i in range(100)]])
    client = make_client(server)
    result = await client.list_stations()
    assert len(result.stations) == 100 and result.pages_retrieved == 1
    await client.close()


async def test_multiple_pages_are_all_retrieved():
    server = StationListServer(
        pages=[
            [station(i) for i in range(100)],
            [station(i) for i in range(100, 200)],
            [station(i) for i in range(200, 250)],
        ]
    )
    client = make_client(server)
    result = await client.list_stations()
    assert result.pages_retrieved == 3
    assert len(result.stations) == 250
    assert [b["pageNo"] for b in server.requests] == [1, 2, 3]
    await client.close()


async def test_identical_duplicates_are_deduplicated_deterministically():
    dup = station(1)
    server = StationListServer(pages=[[dup, station(2)], [dict(dup), station(3)]])
    client = make_client(server)
    result = await client.list_stations()
    assert [s["stationCode"] for s in result.stations] == ["NE=1", "NE=2", "NE=3"]
    assert result.duplicates_removed == 1
    await client.close()


async def test_conflicting_duplicates_are_rejected():
    server = StationListServer(pages=[[station(1)], [station(1, stationName="Different Name")]])
    client = make_client(server)
    with pytest.raises(AdapterProtocolError):
        await client.list_stations()
    await client.close()


async def test_repeated_identical_page_is_detected():
    page = [station(1), station(2)]
    server = StationListServer(pages=[page, [dict(r) for r in page], [station(3)]])
    client = make_client(server)
    with pytest.raises(AdapterProtocolError):
        await client.list_stations()
    await client.close()


async def test_missing_page_count_metadata_is_flagged_not_silent():
    server = StationListServer(pages=[[station(1)]])
    server.omit_page_count = True
    client = make_client(server)
    result = await client.list_stations()
    assert result.metadata_missing is True
    assert len(result.stations) == 1
    await client.close()


@pytest.mark.parametrize("bad_count", [-1, 10_000, "many"])
async def test_impossible_page_count_is_protocol_error(bad_count: Any):
    server = StationListServer(pages=[[station(1)]])
    server.page_count_override = bad_count
    client = make_client(server)
    with pytest.raises(AdapterProtocolError):
        await client.list_stations()
    await client.close()


async def test_empty_page_before_page_count_is_protocol_error():
    server = StationListServer(pages=[[station(1)], []])
    server.page_count_override = 3
    client = make_client(server)
    with pytest.raises(AdapterProtocolError):
        await client.list_stations()
    await client.close()


async def test_finite_max_page_guard():
    # Server claims a within-guard pageCount but keeps yielding fresh rows;
    # client must stop at pageCount — and a huge claimed count trips the guard.
    server = StationListServer(pages=[[station(i)] for i in range(4)])
    client = make_client(server, max_pages=3)
    server.page_count_override = 4  # > max_pages guard
    with pytest.raises(AdapterProtocolError):
        await client.list_stations()
    await client.close()


async def test_malformed_rows_are_not_silently_skipped():
    server = StationListServer(pages=None, direct=[station(1), {"stationName": "no code"}])
    client = make_client(server)
    with pytest.raises(AdapterProtocolError):
        await client.list_stations()
    await client.close()


async def test_data_neither_list_nor_object_is_protocol_error():
    server = StationListServer(pages=[[station(1)]])
    server.data_override = "strange"
    client = make_client(server)
    with pytest.raises(AdapterProtocolError):
        await client.list_stations()
    await client.close()
