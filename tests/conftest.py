from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
NOW = "2026-09-13T10:00:00+00:00"


@pytest.fixture
def cases_dir(tmp_path, monkeypatch):
    d = tmp_path / "cases"
    monkeypatch.setenv("SCREENING_CASES_DIR", str(d))
    return d


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def mock_client(handler, base_url: str) -> httpx.Client:
    """httpx client whose transport is `handler(request) -> httpx.Response`."""
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)


def json_response(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)
