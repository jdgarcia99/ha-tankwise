"""Find out whether a state change was caused by a person.

A change counts as made by a person when its context (or one of its
ancestors) carries a user ID, or when it was made by one of the automations
the user listed as "acting on behalf of a person" (e.g. an automation that
handles a physical push button). Any other automation does not count.

Contexts only carry their parent's ID, so the tracker records the contexts
of automation and script runs as they start and walks the ``parent_id``
chain from the state change back through them.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Collection
from dataclasses import dataclass

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, Event, HomeAssistant, callback

# Event names of the automation and script integrations (kept as strings so
# TankWise does not depend on those integrations).
EVENT_AUTOMATION_TRIGGERED = "automation_triggered"
EVENT_SCRIPT_STARTED = "script_started"
MAX_TRACKED_RUNS = 500
MAX_CHAIN_DEPTH = 10


@dataclass(frozen=True, slots=True)
class _Run:
    entity_id: str | None
    parent_id: str | None
    user_id: str | None


class ActorTracker:
    """Remember recent automation/script runs to attribute state changes."""

    def __init__(self) -> None:
        """Initialise the tracker."""
        self._runs: OrderedDict[str, _Run] = OrderedDict()

    @callback
    def async_start(self, hass: HomeAssistant) -> Callable[[], None]:
        """Start listening to automation and script runs; return an unsubscriber."""
        unsubscribers = [
            hass.bus.async_listen(EVENT_AUTOMATION_TRIGGERED, self._async_record),
            hass.bus.async_listen(EVENT_SCRIPT_STARTED, self._async_record),
        ]

        @callback
        def unsubscribe() -> None:
            for unsub in unsubscribers:
                unsub()

        return unsubscribe

    @callback
    def _async_record(self, event: Event) -> None:
        context = event.context
        self._runs[context.id] = _Run(
            entity_id=event.data.get(ATTR_ENTITY_ID),
            parent_id=context.parent_id,
            user_id=context.user_id,
        )
        self._runs.move_to_end(context.id)
        while len(self._runs) > MAX_TRACKED_RUNS:
            self._runs.popitem(last=False)

    def is_person(self, context: Context, person_automations: Collection[str]) -> bool:
        """Return True if the change was made by a person (directly or not)."""
        if context.user_id is not None:
            return True
        context_id: str | None = context.id
        parent_id = context.parent_id
        for _ in range(MAX_CHAIN_DEPTH):
            run = self._runs.get(context_id) if context_id else None
            if run is not None:
                if run.user_id is not None or run.entity_id in person_automations:
                    return True
                context_id, parent_id = run.parent_id, None
                continue
            if parent_id is None:
                return False
            context_id, parent_id = parent_id, None
        return False
