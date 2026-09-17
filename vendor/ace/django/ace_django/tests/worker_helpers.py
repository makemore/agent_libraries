"""Spawn-safe activity definitions used by ACE worker tests."""

import os
import time

import django

django.setup()

from ace import ActivityContext, ActivityRegistry  # noqa: E402
from ace.json_types import JsonObject, JsonValue  # noqa: E402

from ace_django.worker import WorkerRuntime  # noqa: E402


def echo_activity(context: ActivityContext, input: JsonObject) -> JsonValue:
    context.heartbeat({"phase": "echo"})
    return input


def slow_activity(context: ActivityContext, input: JsonObject) -> JsonValue:
    time.sleep(0.3)
    return {"completed": True, **input}


def build_runtime() -> WorkerRuntime:
    activities = ActivityRegistry()
    activities.register("tests.echo", echo_activity)
    activities.register("tests.slow", slow_activity)
    return WorkerRuntime(activities)


def exit_immediately(*args) -> None:
    os._exit(7)


def hang_until_killed(*args) -> None:
    while True:
        time.sleep(1)
