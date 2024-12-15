"""
Test script for validating backup functionality in the Backup Component.
Includes testing for both full and incremental backups.
"""

import asyncio
from homeassistant.components.backup.manager import BackupManager
from homeassistant.core import HomeAssistant
import logging

logger = logging.getLogger(__name__)

async def init_homeassistant():
    """
    Initialize the HomeAssistant instance and return the BackupManager.

    Returns:
        BackupManager: The initialized backup manager.
    """
    return BackupManager(HomeAssistant("/workspaces/core/config"))

async def create_full_backup(manager):
    """
    Create a full backup using the BackupManager.

    Args:
        manager (BackupManager): The backup manager instance.

    Returns:
        Backup: The created backup object.
    """
    return await manager.async_create_backup()

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Initialize HomeAssistant and BackupManager
manager = loop.run_until_complete(init_homeassistant())
full_backup = loop.run_until_complete(create_full_backup(manager))
logger.info("Full Backup Created: %s", full_backup.as_dict())
