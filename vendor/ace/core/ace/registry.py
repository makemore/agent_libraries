"""Explicit workflow and activity definition registries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ace.exceptions import DefinitionNotFound, DuplicateDefinition, InvalidDefinition

if TYPE_CHECKING:
    from ace.definitions import ActivityCallable, WorkflowDefinition


def _validate_identity(kind: str, name: str, version: str) -> None:
    if not name.strip():
        raise InvalidDefinition(f"{kind} name cannot be blank.")
    if not version.strip():
        raise InvalidDefinition(f"{kind} {name!r} version cannot be blank.")


class WorkflowRegistry:
    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str], WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        _validate_identity("Workflow", definition.name, definition.version)
        if not callable(definition.start) or not callable(definition.advance):
            raise InvalidDefinition(
                f"Workflow {definition.name!r} version {definition.version!r} "
                "must implement start() and advance()."
            )
        key = (definition.name, definition.version)
        if key in self._definitions:
            raise DuplicateDefinition(
                f"Workflow {definition.name!r} version {definition.version!r} is registered twice."
            )
        self._definitions[key] = definition
        return definition

    def resolve(self, name: str, version: str) -> WorkflowDefinition:
        try:
            return self._definitions[(name, version)]
        except KeyError as exc:
            raise DefinitionNotFound(
                f"Workflow {name!r} version {version!r} is not registered."
            ) from exc

    def identities(self) -> frozenset[tuple[str, str]]:
        """Return all registered (name, version) pairs."""
        return frozenset(self._definitions.keys())

    def has(self, name: str, version: str) -> bool:
        """Check if a specific (name, version) is registered."""
        return (name, version) in self._definitions


class ActivityRegistry:
    def __init__(self) -> None:
        self._activities: dict[tuple[str, str], ActivityCallable] = {}

    def register(
        self,
        name: str,
        function: ActivityCallable,
        *,
        version: str = "1",
    ) -> ActivityCallable:
        _validate_identity("Activity", name, version)
        if not callable(function):
            raise InvalidDefinition(f"Activity {name!r} must be callable.")
        key = (name, version)
        if key in self._activities:
            raise DuplicateDefinition(f"Activity {name!r} version {version!r} is registered twice.")
        self._activities[key] = function
        return function

    def resolve(self, name: str, version: str = "1") -> ActivityCallable:
        try:
            return self._activities[(name, version)]
        except KeyError as exc:
            raise DefinitionNotFound(
                f"Activity {name!r} version {version!r} is not registered."
            ) from exc

    def identities(self) -> frozenset[tuple[str, str]]:
        """Return all registered (name, version) pairs."""
        return frozenset(self._activities.keys())

    def has(self, name: str, version: str) -> bool:
        """Check if a specific (name, version) is registered."""
        return (name, version) in self._activities
