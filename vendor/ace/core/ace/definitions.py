"""Protocols implemented by workflow definitions and runtime services."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from ace.commands import WorkflowTransition
    from ace.json_types import JsonObject, JsonValue
    from ace.models import ActivityContext, WorkflowContext, WorkflowEvent


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new_id(self) -> str: ...


class WorkflowDefinition(Protocol):
    name: str
    version: str

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition: ...

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition: ...


ActivityCallable = Callable[["ActivityContext", "JsonObject"], "JsonValue"]
