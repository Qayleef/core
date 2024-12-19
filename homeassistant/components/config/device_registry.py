"""HTTP views to interact with the device registry."""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol
import logging


from homeassistant import loader
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import require_admin
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry, DeviceEntryDisabler

logger = logging.getLogger(__name__)

@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        "type": "config/device_registry/remove_config_entry",
        "config_entry_id": str,
        "device_id": str,
    }
)
@websocket_api.async_response
async def websocket_remove_config_entry_from_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Remove config entry from a device with enhanced security and logging."""
    registry = dr.async_get(hass)
    config_entry_id = msg["config_entry_id"]
    device_id = msg["device_id"]
    user = connection.user

    # Log the attempt for security auditing
    logger.info(
        f"User {user.id} attempting to remove config entry {config_entry_id} from device {device_id}"
    )

    def send_error_response(message):
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, message)

    if (config_entry := hass.config_entries.async_get_entry(config_entry_id)) is None:
        send_error_response("Config entry not found or inaccessible")
        return

    if not config_entry.supports_remove_device:
        send_error_response("Config entry does not support device removal")
        return

    if (device_entry := registry.async_get(device_id)) is None:
        send_error_response("Device not found or inaccessible")
        return

    if config_entry_id not in device_entry.config_entries:
        send_error_response("Config entry not in device")
        return

    try:
        integration = await loader.async_get_integration(hass, config_entry.domain)
        component = await integration.async_get_component()
    except (ImportError, loader.IntegrationNotFound) as exc:
        send_error_response("Integration not found")
        logger.error(f"Integration {config_entry.domain} not found: {exc}")
        return

    if not await component.async_remove_config_entry_device(
        hass, config_entry, device_entry
    ):
        send_error_response("Failed to remove device entry, rejected by integration")
        return

    # Update registry if the integration hasn't removed the device entry already.
    if registry.async_get(device_id):
        entry = registry.async_update_device(
            device_id, remove_config_entry_id=config_entry_id
        )
        entry_as_dict = entry.dict_repr if entry else None
    else:
        entry_as_dict = None

    connection.send_message(websocket_api.result_message(msg["id"], entry_as_dict))




@callback
def async_setup(hass: HomeAssistant) -> bool:
    """Enable the Device Registry views."""

    websocket_api.async_register_command(hass, websocket_list_devices)
    websocket_api.async_register_command(hass, websocket_update_device)
    websocket_api.async_register_command(
        hass, websocket_remove_config_entry_from_device
    )
    return True


@callback
@websocket_api.websocket_command(
    {
        vol.Required("type"): "config/device_registry/list",
    }
)
def websocket_list_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle list devices command."""
    registry = dr.async_get(hass)
    # Build start of response message
    msg_json_prefix = (
        f'{{"id":{msg["id"]},"type": "{websocket_api.TYPE_RESULT}",'
        f'"success":true,"result": ['
    ).encode()
    # Concatenate cached entity registry item JSON serializations
    inner = b",".join(
        [
            entry.json_repr
            for entry in registry.devices.values()
            if entry.json_repr is not None
        ]
    )
    msg_json = b"".join((msg_json_prefix, inner, b"]}"))
    connection.send_message(msg_json)


@require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "config/device_registry/update",
        vol.Optional("area_id"): vol.Any(str, None),
        vol.Required("device_id"): str,
        # We only allow setting disabled_by user via API.
        # No Enum support like this in voluptuous, use .value
        vol.Optional("disabled_by"): vol.Any(DeviceEntryDisabler.USER.value, None),
        vol.Optional("labels"): [str],
        vol.Optional("name_by_user"): vol.Any(str, None),
    }
)
@callback
def websocket_update_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Handle update device websocket command."""
    registry = dr.async_get(hass)

    msg.pop("type")
    msg_id = msg.pop("id")

    if msg.get("disabled_by") is not None:
        msg["disabled_by"] = DeviceEntryDisabler(msg["disabled_by"])

    if "labels" in msg:
        # Convert labels to a set
        msg["labels"] = set(msg["labels"])

    entry = cast(DeviceEntry, registry.async_update_device(**msg))

    connection.send_message(websocket_api.result_message(msg_id, entry.dict_repr))


