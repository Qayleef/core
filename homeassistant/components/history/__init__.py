"""Provide pre-made queries on top of the recorder component."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime as dt, timedelta
from http import HTTPStatus
from typing import cast

from aiohttp import web
from aiohttp.web_request import MultiMapping
import voluptuous as vol

from homeassistant.components import frontend
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.util import session_scope
from homeassistant.const import CONF_EXCLUDE, CONF_INCLUDE
from homeassistant.core import HomeAssistant, valid_entity_id
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entityfilter import INCLUDE_EXCLUDE_BASE_FILTER_SCHEMA
from homeassistant.helpers.typing import ConfigType
import homeassistant.util.dt as dt_util

from . import websocket_api
from .const import DOMAIN
from .helpers import entities_may_have_state_changes_after, has_recorder_run_after

CONF_ORDER = "use_include_order"

_ONE_DAY = timedelta(days=1)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            cv.deprecated(CONF_INCLUDE),
            cv.deprecated(CONF_EXCLUDE),
            cv.deprecated(CONF_ORDER),
            INCLUDE_EXCLUDE_BASE_FILTER_SCHEMA.extend(
                {vol.Optional(CONF_ORDER, default=False): cv.boolean}
            ),
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the history component.

    This registers the HTTP endpoints and websocket APIs for
    fetching historical state data.
    """
    hass.http.register_view(HistoryPeriodView())
    hass.http.register_view(HistoryDiagnosticsView())  # Register diagnostics endpoint
    frontend.async_register_built_in_panel(hass, "history", "history", "hass:chart-box")
    websocket_api.async_setup(hass)
    return True


class HistoryPeriodView(HomeAssistantView):
    """Handle requests for historical state data over a specific time period."""

    url = "/api/history/period"
    name = "api:history:view-period"
    extra_urls = ["/api/history/period/{datetime}"]

    async def get(
        self, request: web.Request, datetime: str | None = None
    ) -> web.Response:
        """Fetch historical state data for the requested time period and entities.

        Args:
            request (web.Request): The HTTP request containing query parameters.
            datetime (str | None): The start time as a string, if provided in the URL.

        Returns:
            web.Response: JSON response containing the historical state data or an error message.

        """
        # Retrieve the Home Assistant instance from the request
        hass = request.app[KEY_HASS]

        # Parse the start time from the URL if provided
        datetime_ = self._parse_datetime(datetime)
        if datetime is not None and datetime_ is None:
            return self.json_message("Invalid datetime", HTTPStatus.BAD_REQUEST)

        # Retrieve and validate the `filter_entity_id` parameter
        entity_ids = self._parse_entity_ids(request.query)
        if not entity_ids:
            return self.json_message(
                "filter_entity_id is missing", HTTPStatus.BAD_REQUEST
            )

        # Validate that all entity IDs are valid and present in Home Assistant states
        invalid_entity = self._validate_entity_ids(hass, entity_ids)
        if invalid_entity:
            return self.json_message(
                f"Invalid filter_entity_id: {invalid_entity}",
                HTTPStatus.BAD_REQUEST,
            )

        now = dt_util.utcnow()

        # Determine the start and end times based on the query parameters
        start_time, end_time = self._get_time_range(request.query, datetime_, now)
        if start_time > now:
            return self.json([])

        # Determine additional query flags
        include_start_time_state = "skip_initial_state" not in request.query
        significant_changes_only = (
            request.query.get("significant_changes_only", "1") != "0"
        )
        minimal_response = "minimal_response" in request.query
        no_attributes = "no_attributes" in request.query

        # Verify if the history retrieval is possible for the given time range and entities
        if not self._can_retrieve_history(
            hass,
            start_time,
            end_time,
            entity_ids,
            include_start_time_state,
            no_attributes,
        ):
            return self.json([])

        # Fetch and return significant states
        return await self._fetch_significant_states(
            hass,
            start_time,
            end_time,
            entity_ids,
            include_start_time_state,
            significant_changes_only,
            minimal_response,
            no_attributes,
        )

    def _parse_datetime(self, datetime: str | None) -> dt | None:
        """Parse datetime from a string.

        Args:
            datetime (str | None): The datetime string to parse.

        Returns:
            dt | None: The parsed datetime object, or None if invalid.

        """
        return dt_util.parse_datetime(datetime) if datetime else None

    def _parse_entity_ids(self, query: MultiMapping[str]) -> list[str] | None:
        """Parse and validate entity IDs from the query string.

        Args:
            query (web.MultiDict): The query parameters.

        Returns:
            list[str] | None: A list of entity IDs or None if missing.

        """
        entity_ids_str: str | None = query.get("filter_entity_id")
        if not entity_ids_str:
            return None
        return entity_ids_str.strip().lower().split(",")

    def _validate_entity_ids(
        self, hass: HomeAssistant, entity_ids: list[str]
    ) -> str | None:
        """Validate that all entity IDs are valid and present in Home Assistant states.

        Args:
            hass (HomeAssistant): Home Assistant instance.
            entity_ids (list[str]): List of entity IDs to validate.

        Returns:
            str | None: The first invalid entity ID, or None if all are valid.

        """
        for entity_id in entity_ids:
            if not hass.states.get(entity_id) and not valid_entity_id(entity_id):
                return entity_id
        return None

    def _get_time_range(
        self, query: MultiMapping[str], datetime_: dt | None, now: dt
    ) -> tuple[dt, dt]:
        """Determine the start and end times based on the query parameters.

        Args:
            query (web.MultiDict): The query parameters.
            datetime_ (dt | None): The parsed start time, if provided.
            now (dt): The current time.

        Returns:
            tuple[dt, dt]: The start and end times as datetime objects.

        Raises:
            web.HTTPBadRequest: If the `end_time` is invalid.

        """
        start_time = dt_util.as_utc(datetime_) if datetime_ else now - _ONE_DAY

        end_time_str: str | None = query.get("end_time")
        if end_time_str:
            end_time = dt_util.parse_datetime(end_time_str)
            if end_time is None:
                # Return JSON-formatted error message
                raise web.HTTPBadRequest(
                    content_type="application/json",
                    text='{"message": "Invalid end_time"}',
                )
            end_time = dt_util.as_utc(end_time)
        else:
            end_time = start_time + _ONE_DAY

        return start_time, end_time

    def _can_retrieve_history(
        self,
        hass: HomeAssistant,
        start_time: dt,
        end_time: dt,
        entity_ids: list[str],
        include_start_time_state: bool,
        no_attributes: bool,
    ) -> bool:
        """Check if history retrieval is possible for the given time range and entities.

        Args:
            hass (HomeAssistant): Home Assistant instance.
            start_time (dt): The start time for the query.
            end_time (dt): The end time for the query.
            entity_ids (list[str]): List of entity IDs to check.
            include_start_time_state (bool): Include the initial state if True.
            no_attributes (bool): Exclude attributes from the response if True.

        Returns:
            bool: True if history retrieval is possible, False otherwise.

        """
        if end_time and not has_recorder_run_after(hass, end_time):
            return False
        if not include_start_time_state and entity_ids:
            return entities_may_have_state_changes_after(
                hass, entity_ids, start_time, no_attributes
            )
        return True

    async def _fetch_significant_states(
        self,
        hass: HomeAssistant,
        start_time: dt,
        end_time: dt,
        entity_ids: list[str],
        include_start_time_state: bool,
        significant_changes_only: bool,
        minimal_response: bool,
        no_attributes: bool,
    ) -> web.Response:
        """Fetch and return significant state changes.

        Args:
            hass (HomeAssistant): Home Assistant instance.
            start_time (dt): The start time for the query.
            end_time (dt): The end time for the query.
            entity_ids (list[str]): List of entity IDs to fetch history for.
            include_start_time_state (bool): Include the initial state if True.
            significant_changes_only (bool): Include only significant changes if True.
            minimal_response (bool): Return minimal data if True.
            no_attributes (bool): Exclude attributes from the response if True.

        Returns:
            web.Response: JSON response with the state changes.

        """
        return cast(
            web.Response,
            await get_instance(hass).async_add_executor_job(
                self._sorted_significant_states_json,
                hass,
                start_time,
                end_time,
                entity_ids,
                include_start_time_state,
                significant_changes_only,
                minimal_response,
                no_attributes,
            ),
        )

    def _sorted_significant_states_json(
        self,
        hass: HomeAssistant,
        start_time: dt,
        end_time: dt,
        entity_ids: list[str],
        include_start_time_state: bool,
        significant_changes_only: bool,
        minimal_response: bool,
        no_attributes: bool,
    ) -> web.Response:
        """Fetch and return significant state changes as a JSON response.

        Args:
            hass (HomeAssistant): Home Assistant instance.
            start_time (dt): The start time for the query.
            end_time (dt): The end time for the query.
            entity_ids (list[str]): List of entity IDs to fetch history for.
            include_start_time_state (bool): Include the initial state if True.
            significant_changes_only (bool): Include only significant changes if True.
            minimal_response (bool): Return minimal data if True.
            no_attributes (bool): Exclude attributes from the response if True.

        Returns:
            web.Response: JSON response with the state changes.

        """
        with session_scope(hass=hass, read_only=True) as session:
            return self.json(
                list(
                    history.get_significant_states_with_session(
                        hass,
                        session,
                        start_time,
                        end_time,
                        entity_ids,
                        None,
                        include_start_time_state,
                        significant_changes_only,
                        minimal_response,
                        no_attributes,
                    ).values()
                )
            )


class HistoryDiagnosticsView(HomeAssistantView):
    """Enhanced Diagnostics view for the history component."""

    url = "/api/history/diagnostics"
    name = "api:history:diagnostics"

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET requests for diagnostics."""
        hass: HomeAssistant = request.app["hass"]

        # Define a time range for diagnostics
        now = dt_util.utcnow()
        past_hour = now - timedelta(hours=1)

        # Get all entity IDs known to Home Assistant
        entity_ids = list(hass.states.async_entity_ids())

        # Initialize trackers for state changes and categories
        state_change_tracker: defaultdict[str, int] = defaultdict(int)
        entity_state_changes = {}
        entity_categories: defaultdict[str, int] = defaultdict(int)
        total_changes = 0

        # Define routine updates or meaningless states to ignore
        IGNORE_STATES = {"unknown", "0"}

        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            if state and state.last_changed and state.last_changed > past_hour:
                if state.state in IGNORE_STATES:
                    continue  # Skip entities with ignored states

                # Count the state change
                state_change_tracker[entity_id] += 1
                entity_state_changes[entity_id] = {
                    "changes": state_change_tracker[entity_id],
                    "last_changed": state.last_changed.isoformat(),
                    "state_value": state.state,
                }
                total_changes += 1

                # Categorize changes by domain
                domain = entity_id.split(".")[0]
                entity_categories[domain] += 1

        # Identify the top 5 entities with the most frequent state changes
        frequent_entities = sorted(
            entity_state_changes.items(),
            key=lambda x: int(x[1]["changes"])
            if isinstance(x[1]["changes"], int)
            else 0,
            reverse=True,
        )[:5]

        # Compile diagnostics
        diagnostics = {
            "current_time": now.isoformat(),
            "entities_total": len(entity_ids),
            "total_state_changes_in_last_hour": total_changes,
            "entities_with_changes": len(entity_state_changes),
            "top_changing_entities": [
                {
                    "entity_id": entity,
                    "changes": data["changes"],
                    "last_changed": data["last_changed"],
                    "state_value": data["state_value"],
                }
                for entity, data in frequent_entities
            ],
            "categorized_changes": dict(entity_categories),
        }

        return web.json_response(diagnostics)
