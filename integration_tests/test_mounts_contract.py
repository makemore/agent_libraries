"""The same existing APIs can be mounted under a different host prefix."""

import pytest

from .fixtures import create_thread

pytestmark = pytest.mark.django_db


def test_alternate_mounts_preserve_auth_serializers_and_pagination(world, api_for, bridge):
    create_thread(world.owner, world.project, world.agent)
    default = api_for(world.owner)
    alternate = api_for(
        world.owner, runtime_mount="/alternate/runtime/", studio_mount="/alternate/studio/api/",
    )
    for resource in ("projects", "threads"):
        query = {"limit": 1} if resource == "threads" else None
        expected = default.list("studio", resource, query=query, all_pages=True)
        start = len(bridge.requests)
        assert alternate.list("studio", resource, query=query, all_pages=True) == expected
        assert all(path.startswith("/alternate/studio/api/workspace/")
                   for _, path, _ in bridge.requests[start:])
    for group, resource, identity in (
        ("runtime", "agents", world.agent["slug"]),
        ("studio", "projects", world.project["id"]),
        ("studio", "threads", world.thread["id"]),
    ):
        expected = default.get(group, resource, identity)
        start = len(bridge.requests)
        assert alternate.get(group, resource, identity) == expected
        prefix = "/alternate/runtime/" if group == "runtime" else "/alternate/studio/api/workspace/"
        assert all(path.startswith(prefix) for _, path, _ in bridge.requests[start:])
    assert alternate.list("runtime", "agents") == default.list("runtime", "agents")
    assert alternate.list("runtime", "runs") == default.list("runtime", "runs")


def test_status_only_observes_configured_reads_without_dispatch_or_database_writes(
    world, api_for, bridge,
):
    api = api_for(world.owner, identity_path="/host/identity/")
    start = len(bridge.requests)
    result = api.status()
    assert len(bridge.requests) - start == 3
    assert [row["availability"] for row in result["observations"]] == ["observed_available"] * 3
    assert "not capability or authorization grants" in result["scope"]
    world.owner.revoke_token()
    start = len(bridge.requests)
    unavailable = api.status()
    assert len(bridge.requests) - start == 3
    assert [row["availability"] for row in unavailable["observations"]] == ["observed_unavailable"] * 3
    assert [row["status"] for row in unavailable["observations"]] == [401, 403, 401]
