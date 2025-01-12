# type: ignore  # noqa: PGH003
"""Event parser and human readable log generator."""

from __future__ import annotations

from collections.abc import Callable
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import frontend
from homeassistant.components.recorder import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.filters import (
    extract_include_exclude_filter_conf,
    merge_include_exclude_filters,
    sqlalchemy_filter_from_include_exclude_conf,
)
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_NAME,
    EVENT_LOGBOOK_ENTRY,
)
from homeassistant.core import Context, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entityfilter import (
    INCLUDE_EXCLUDE_BASE_FILTER_SCHEMA,
    convert_include_exclude_filter,
)
from homeassistant.helpers.integration_platform import (
    async_process_integration_platforms,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import bind_hass
from homeassistant.util.event_type import EventType

from . import rest_api, websocket_api
from .const import (  # noqa: F401
    ATTR_MESSAGE,
    DOMAIN,
    LOGBOOK_ENTRY_CONTEXT_ID,
    LOGBOOK_ENTRY_DOMAIN,
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_ICON,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
    LOGBOOK_ENTRY_SOURCE,
)
from .models import LazyEventPartialState, LogbookConfig

LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: INCLUDE_EXCLUDE_BASE_FILTER_SCHEMA}, extra=vol.ALLOW_EXTRA
)


LOG_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NAME): cv.string,
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_DOMAIN): cv.slug,
        vol.Optional(ATTR_ENTITY_ID): cv.entity_id,
    }
)


@callback
def logbook_health_check(hass: HomeAssistant) -> None:
    """Perform a health check on the logbook integration."""
    LOGGER.info("Executing logbook_health_check")
    logbook_config = hass.data.get(DOMAIN)
    if not logbook_config:
        LOGGER.error("Logbook configuration is missing")
        return

    filters = getattr(logbook_config, "filters", None)
    external_events = getattr(logbook_config, "external_events", None)

    if filters is None or external_events is None:
        LOGGER.warning("Logbook filters or external events are not configured properly")
    else:
        LOGGER.info(
            "Logbook is healthy: filters and external events are properly configured"
        )


@bind_hass
def log_entry(
    hass: HomeAssistant,
    name: str,
    message: str,
    domain: str | None = None,
    entity_id: str | None = None,
    context: Context | None = None,
) -> None:
    """Add an entry to the logbook."""
    hass.add_job(async_log_entry, hass, name, message, domain, entity_id, context)


@callback
@bind_hass
def async_log_entry(
    hass: HomeAssistant,
    name: str,
    message: str,
    domain: str | None = None,
    entity_id: str | None = None,
    context: Context | None = None,
) -> None:
    """Add an entry to the logbook."""
    LOGGER.info(
        "Processing log entry: name=%s, message=%s, domain=%s, entity_id=%s",
        name,
        message,
        domain,
        entity_id,
    )

    entities_filter = getattr(hass.data.get(DOMAIN), "entities_filter", None)
    # Check if the entity is excluded by the filter
    if entity_id and entities_filter and not entities_filter(entity_id):
        LOGGER.info("Skipping excluded entity: %s", entity_id)
        return

    # Check if the domain is excluded by the filter
    if domain and entities_filter and not entities_filter(f"{domain}._"):
        LOGGER.info("Skipping excluded domain: %s", domain)
        return

    if not name or not message:
        LOGGER.warning("Invalid log entry: name or message is missing")
        return

    data = {LOGBOOK_ENTRY_NAME: name, LOGBOOK_ENTRY_MESSAGE: message}
    if domain is not None:
        data[LOGBOOK_ENTRY_DOMAIN] = domain
    if entity_id is not None:
        data[LOGBOOK_ENTRY_ENTITY_ID] = entity_id
    hass.bus.async_fire(EVENT_LOGBOOK_ENTRY, data, context=context)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Logbook setup."""

    @callback
    def log_message(service: ServiceCall) -> None:
        """Handle sending notification message service calls."""
        message = service.data[ATTR_MESSAGE]
        name = service.data[ATTR_NAME]
        domain = service.data.get(ATTR_DOMAIN)
        entity_id = service.data.get(ATTR_ENTITY_ID)

        if entity_id is None and domain is None:
            # If there is no entity_id or
            # domain, the event will get filtered
            # away so we use the "logbook" domain
            domain = DOMAIN

        async_log_entry(hass, name, message, domain, entity_id, service.context)

    frontend.async_register_built_in_panel(
        hass, "logbook", "logbook", "hass:format-list-bulleted-type"
    )

    # Apply default recorder and logbook filters if not provided
    recorder_conf = config.get(RECORDER_DOMAIN, {})
    logbook_conf = config.get(DOMAIN, {})

    if not recorder_conf:
        LOGGER.info("No recorder configuration found. Using default filters")
        recorder_conf = {
            "include": {"domains": ["light", "switch"]},
            "exclude": {"entities": ["light.bedroom"]},
        }

    if not logbook_conf:
        LOGGER.info("No logbook configuration found. Using default filters")
        logbook_conf = {
            "include": {
                "domains": [
                    "automation",
                    "light",
                    "sensor",
                    "switch",
                ]
            },
            "exclude": {"entities": ["sensor.outdoor_temperature"]},
        }

    # Extract and merge filters
    recorder_filter = extract_include_exclude_filter_conf(recorder_conf)
    logbook_filter = extract_include_exclude_filter_conf(logbook_conf)
    LOGGER.info("Extracted logbook filter: %s", logbook_filter)

    start_time = time.time()
    merged_filter = merge_include_exclude_filters(recorder_filter, logbook_filter)
    LOGGER.info("Merged filter (recorder + logbook): %s", merged_filter)
    LOGGER.info("Filter merging took %.2f seconds", time.time() - start_time)

    possible_merged_entities_filter = convert_include_exclude_filter(merged_filter)
    if not possible_merged_entities_filter.empty_filter:
        LOGGER.info("Merged entities filter is valid. Applying filter")
        filters = sqlalchemy_filter_from_include_exclude_conf(merged_filter)
        entities_filter = possible_merged_entities_filter.get_filter()
    else:
        LOGGER.warning(
            "Merged entities filter is empty. No entities will be filtered"
            "Check the recorder and logbook include/exclude configuration"
        )
        filters = None
        entities_filter = None

    external_events: dict[
        EventType[Any] | str,
        tuple[str, Callable[[LazyEventPartialState], dict[str, Any]]],
    ] = {}
    hass.data[DOMAIN] = LogbookConfig(external_events, filters, entities_filter)
    websocket_api.async_setup(hass)
    rest_api.async_setup(hass, config, filters, entities_filter)
    hass.services.async_register(DOMAIN, "log", log_message, schema=LOG_MESSAGE_SCHEMA)

    # Call health check after filters are set up
    logbook_health_check(hass)

    await async_process_integration_platforms(hass, DOMAIN, _process_logbook_platform)

    return True


@callback
def _process_logbook_platform(hass: HomeAssistant, domain: str, platform: Any) -> None:
    """Process a logbook platform."""
    logbook_config: LogbookConfig = hass.data[DOMAIN]
    external_events = logbook_config.external_events

    LOGGER.info("Processing platform: %s", domain)

    @callback
    def _async_describe_event(
        domain: str,
        event_name: str,
        describe_callback: Callable[[LazyEventPartialState], dict[str, Any]],
    ) -> None:
        """Teach logbook how to describe a new event."""
        LOGGER.info("Registering event: %s for domain: %s", event_name, domain)
        external_events[event_name] = (domain, describe_callback)

    platform.async_describe_events(hass, _async_describe_event)
