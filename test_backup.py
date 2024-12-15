import asyncio

from homeassistant.components.backup.manager import BackupManager
from homeassistant.core import HomeAssistant


async def init_homeassistant():
    hass = HomeAssistant("/workspaces/core/config")
    manager = BackupManager(hass)
    return manager


async def create_full_backup(manager):
    backup = await manager.async_create_backup()
    return backup


loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Initialize HomeAssistant and BackupManager
manager = loop.run_until_complete(init_homeassistant())
full_backup = loop.run_until_complete(create_full_backup(manager))
print("Full Backup Created:", full_backup.as_dict())
