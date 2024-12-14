"""Provide pre-made queries on top of the recorder component."""

from __future__ import annotations

from datetime import datetime as dt, timedelta
from http import HTTPStatus
from typing import cast

from aiohttp import web
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
        query = request.query

        # Parse the start time from the URL if provided
        datetime_ = None
        if datetime and (datetime_ := dt_util.parse_datetime(datetime)) is None:
            return self.json_message("Invalid datetime", HTTPStatus.BAD_REQUEST)

        # Retrieve and validate the `filter_entity_id` parameter
        entity_ids_str = query.get("filter_entity_id")
        if not entity_ids_str or not (
            entity_ids := entity_ids_str.strip().lower().split(",")
        ):
            return self.json_message(
                "filter_entity_id is missing", HTTPStatus.BAD_REQUEST
            )

        hass = request.app[KEY_HASS]

        # Validate that all entity IDs are valid and present in Home Assistant states
        for entity_id in entity_ids:
            if not hass.states.get(entity_id) and not valid_entity_id(entity_id):
                return self.json_message(
                    "Invalid filter_entity_id", HTTPStatus.BAD_REQUEST
                )

        now = dt_util.utcnow()

        # Determine the start time: parsed datetime or one day before now
        if datetime_:
            start_time = dt_util.as_utc(datetime_)
        else:
            start_time = now - _ONE_DAY

        # If the start time is in the future, return an empty response
        if start_time > now:
            return self.json([])

        # Parse and validate the `end_time` parameter
        end_time_str = query.get("end_time")
        if end_time_str:
            if end_time := dt_util.parse_datetime(end_time_str):
                end_time = dt_util.as_utc(end_time)
            else:
                return self.json_message("Invalid end_time", HTTPStatus.BAD_REQUEST)
        else:
            end_time = start_time + _ONE_DAY

        # Determine additional query flags
        include_start_time_state = "skip_initial_state" not in query
        significant_changes_only = query.get("significant_changes_only", "1") != "0"
        minimal_response = "minimal_response" in query
        no_attributes = "no_attributes" in query

        # Verify if the history retrieval is possible for the given time range and entities
        if (end_time and not has_recorder_run_after(hass, end_time)) or (
            not include_start_time_state
            and entity_ids
            and not entities_may_have_state_changes_after(
                hass, entity_ids, start_time, no_attributes
            )
        ):
            return self.json([])

        # Fetch and return significant states
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
