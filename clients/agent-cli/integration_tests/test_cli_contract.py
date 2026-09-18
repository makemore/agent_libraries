"""Command parser through the real REST/auth/ACL stack (socket bridge only)."""

import io
import json

import pytest
from agentctl.cli import main
from rest_framework.pagination import PageNumberPagination

from .fixtures import create_agent
from .urls import RuntimeAgents

pytestmark = pytest.mark.django_db


def test_command_output_and_revocation_against_actual_studio_api(world, bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_CONFIG_DIR", str(tmp_path.resolve() / "profiles"))
    out, err = io.StringIO(), io.StringIO()
    assert main([
        "profiles", "add", "test", "--origin", "http://127.0.0.1", "--allow-http-loopback",
        "--studio-mount", "/alternate/studio/api/",
    ], stdout=out, stderr=err, environ={}) == 0
    assert not bridge.requests

    def command():
        output, error = io.StringIO(), io.StringIO()
        code = main(["--profile", "test", "studio", "threads", "list", "--json"],
                    stdout=output, stderr=error, environ={"AGENTCTL_TOKEN": world.owner._token})
        return code, output.getvalue(), error.getvalue()

    code, output, error = command()
    assert code == 0 and not error
    data = json.loads(output)
    assert data["schema_version"] == 1
    assert [row["id"] for row in data["data"]["items"]] == [world.thread["id"]]
    assert world.owner._token not in output
    world.owner.revoke_token()
    code, output, error = command()
    assert code == 3 and not output
    assert json.loads(error)["error"]["status"] == 403
    assert world.owner._token not in error
    assert len(bridge.requests) == 2


def test_existing_runtime_with_real_drf_page_number_paginator(world, api_for, monkeypatch, bridge):
    # Named paginated-host scenario: base runtime may be unpaginated; only this
    # test's host class adds DRF's supported paginator. No product policy change.
    class SmallPages(PageNumberPagination):
        page_size = 1

    monkeypatch.setattr(RuntimeAgents, "pagination_class", SmallPages)
    create_agent(world.owner, "second-contract-agent")
    result = api_for(world.owner).list("runtime", "agents", all_pages=True)
    assert result["count"] == 2 and result["pages"] == 2
    assert len(result["items"]) == 2 and not result["has_more"]
    assert bridge.requests[-1][2] == "page=2"
