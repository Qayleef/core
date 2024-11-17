"""Diagnostics support for HomeKit."""

from __future__ import annotations

from typing import Any

from pyhap.accessory_driver import AccessoryDriver
from pyhap.state import State

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .accessories import HomeAccessory, HomeBridge
from .models import HomeKitConfigEntry

TO_REDACT = {"access_token", "entity_picture"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HomeKitConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    homekit = entry.runtime_data.homekit

    # Prepare base diagnostic data
    data = {
        "status": homekit.status,
        "config-entry": {
            "title": entry.title,
            "version": entry.version,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
    }

    # Include IID storage if available
    if homekit.iid_storage:
        data["iid_storage"] = homekit.iid_storage.allocations

    # If the driver is not available or failed, return the current data
    if not homekit.driver:
        return data

    driver: AccessoryDriver = homekit.driver

    # Add diagnostics for the accessory or bridge
    if driver.accessory:
        if isinstance(driver.accessory, HomeBridge):
            data["bridge"] = _get_bridge_diagnostics(hass, driver.accessory)
        else:
            data["accessory"] = _get_accessory_diagnostics(hass, driver.accessory)

    # Update data with driver accessory details
    data.update(driver.get_accessories())

    # Add client properties and configuration details
    state: State = driver.state
    data["client_properties"] = {
        str(client): props for client, props in state.client_properties.items()
    }
    data["config_version"] = state.config_version
    data["pairing_id"] = state.mac

    return data


def _get_bridge_diagnostics(hass: HomeAssistant, bridge: HomeBridge) -> dict[int, Any]:
    """Return diagnostics for a bridge."""
    return {
        aid: _get_accessory_diagnostics(hass, accessory)
        for aid, accessory in bridge.accessories.items()
    }


def _get_accessory_diagnostics(
    hass: HomeAssistant, accessory: HomeAccessory
) -> dict[str, Any]:
    """Return diagnostics for an accessory."""
    # Get entity state if available
    entity_state = hass.states.get(accessory.entity_id) if accessory.entity_id else None

    # Prepare base diagnostic data
    data = {
        "aid": accessory.aid,
        "config": accessory.config,
        "category": accessory.category,
        "name": accessory.display_name,
        "entity_id": accessory.entity_id,
    }

    # Include entity state if available
    if entity_state:
        data["entity_state"] = async_redact_data(entity_state, TO_REDACT)

    return data
