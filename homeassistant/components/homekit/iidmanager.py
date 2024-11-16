"""Manage allocation of instance IDs.

HomeKit needs to allocate unique numbers to each accessory. These need to
be stable between reboots and upgrades.

This module generates and stores them in a HA storage.
"""

from __future__ import annotations

from uuid import UUID

from pyhap.util import uuid_to_hap_type

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .util import get_iid_storage_filename_for_entry_id

IID_MANAGER_STORAGE_VERSION = 2
IID_MANAGER_SAVE_DELAY = 2
ALLOCATIONS_KEY = "allocations"
IID_MIN = 1
IID_MAX = 18446744073709551615
ACCESSORY_INFORMATION_SERVICE = "3E"


class IIDStorage(Store):
    """Storage class for IIDManager."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict,
    ) -> dict:
        """Migrate to the new version."""
        if old_major_version == 1:
            return self._migrate_v1_to_v2(old_data)
        raise NotImplementedError

    def _migrate_v1_to_v2(self, old_data: dict) -> dict:
        """Convert v1 format to v2, storing IIDs per accessory."""
        old_allocations: dict[str, int] = old_data.pop(ALLOCATIONS_KEY, {})
        new_allocations: dict[str, dict[str, int]] = {}
        old_data[ALLOCATIONS_KEY] = new_allocations

        for allocation_key, iid in old_allocations.items():
            aid_str, new_allocation_key = allocation_key.split("_", 1)
            service_type, _, char_type, *_ = new_allocation_key.split("_")
            accessory_allocations = new_allocations.setdefault(aid_str, {})
            if service_type == ACCESSORY_INFORMATION_SERVICE and not char_type:
                accessory_allocations[new_allocation_key] = 1
            elif iid != 1:
                accessory_allocations[new_allocation_key] = iid

        return old_data


class AccessoryIIDStorage:
    """Provide stable allocation of IIDs for the lifetime of an accessory."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create a new IID store."""
        self.hass = hass
        self.entry_id = entry_id
        self.allocations: dict[str, dict[str, int]] = {}
        self.allocated_iids: dict[str, list[int]] = {}
        self.store: IIDStorage | None = None

    async def async_initialize(self) -> None:
        """Load the latest IID data."""
        iid_store = get_iid_storage_filename_for_entry_id(self.entry_id)
        self.store = IIDStorage(self.hass, IID_MANAGER_STORAGE_VERSION, iid_store)

        if not (raw_storage := await self.store.async_load()):
            return

        self.allocations = raw_storage.get(ALLOCATIONS_KEY, {})
        for aid_str, allocations in self.allocations.items():
            self.allocated_iids[aid_str] = sorted(allocations.values())

    def get_or_allocate_iid(
        self,
        aid: int,
        service_uuid: UUID,
        service_unique_id: str | None,
        char_uuid: UUID | None,
        char_unique_id: str | None,
    ) -> int:
        """Generate or retrieve a stable IID."""
        allocation_key = self._generate_allocation_key(
            service_uuid, service_unique_id, char_uuid, char_unique_id
        )
        aid_str = str(aid)

        if self._is_main_service_without_characteristic(service_uuid, char_uuid):
            return 1

        accessory_allocations = self.allocations.setdefault(aid_str, {})
        allocated_iids = self.allocated_iids.setdefault(aid_str, [1])

        if allocation_key in accessory_allocations:
            return accessory_allocations[allocation_key]

        allocated_iid = self._allocate_new_iid(allocated_iids)
        accessory_allocations[allocation_key] = allocated_iid
        allocated_iids.append(allocated_iid)

        self._async_schedule_save()
        return allocated_iid

    def _generate_allocation_key(
        self,
        service_uuid: UUID,
        service_unique_id: str | None,
        char_uuid: UUID | None,
        char_unique_id: str | None,
    ) -> str:
        """Generate a unique allocation key."""
        service_hap_type = uuid_to_hap_type(service_uuid)
        char_hap_type = uuid_to_hap_type(char_uuid) if char_uuid else ""
        return f"{service_hap_type}_{service_unique_id or ''}_{char_hap_type}_{char_unique_id or ''}"

    def _is_main_service_without_characteristic(
        self, service_uuid: UUID, char_uuid: UUID | None
    ) -> bool:
        """Check if it's the main service without a characteristic UUID."""
        return (
            uuid_to_hap_type(service_uuid) == ACCESSORY_INFORMATION_SERVICE
            and char_uuid is None
        )

    def _allocate_new_iid(self, allocated_iids: list[int]) -> int:
        """Allocate the next available IID."""
        return allocated_iids[-1] + 1 if allocated_iids else 2

    @callback
    def _async_schedule_save(self) -> None:
        """Schedule saving the IID allocations."""
        assert self.store is not None
        self.store.async_delay_save(self._data_to_save, IID_MANAGER_SAVE_DELAY)

    async def async_save(self) -> None:
        """Save the IID allocations."""
        assert self.store is not None
        return await self.store.async_save(self._data_to_save())

    @callback
    def _data_to_save(self) -> dict[str, dict[str, dict[str, int]]]:
        """Prepare data to save."""
        return {ALLOCATIONS_KEY: self.allocations}
