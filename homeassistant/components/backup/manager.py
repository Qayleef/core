"""Backup manager for the Backup integration."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import asdict, dataclass
import hashlib
import io
import json
from pathlib import Path
import tarfile
from tarfile import TarError
import time
from typing import Any, Protocol, cast

from securetar import SecureTarFile, atomic_contents_add

from homeassistant.backup_restore import RESTORE_BACKUP_FILE
from homeassistant.const import __version__ as HAVERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import integration_platform
from homeassistant.helpers.json import json_bytes
from homeassistant.util import dt as dt_util
from homeassistant.util.json import json_loads_object

from .const import DOMAIN, EXCLUDE_FROM_BACKUP, LOGGER

BUF_SIZE = 2**20 * 4  # 4MB


@dataclass(slots=True)
class Backup:
    """Backup class."""

    slug: str
    name: str
    date: str
    path: Path
    size: float

    def as_dict(self) -> dict:
        """Return a dict representation of this backup."""
        return {**asdict(self), "path": self.path.as_posix()}


class BackupPlatformProtocol(Protocol):
    """Define the format that backup platforms can have."""

    async def async_pre_backup(self, hass: HomeAssistant) -> None:
        """Perform operations before a backup starts."""

    async def async_post_backup(self, hass: HomeAssistant) -> None:
        """Perform operations after a backup finishes."""


class BaseBackupManager(abc.ABC):
    """Define the format that backup managers can have."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the backup manager."""
        self.hass = hass
        self.backing_up = False
        self.backups: dict[str, Backup] = {}
        self.loaded_platforms = False
        self.platforms: dict[str, BackupPlatformProtocol] = {}

    def _raise_backup_not_found(self, slug: str) -> None:
        """Raise a HomeAssistantError for a missing backup."""
        LOGGER.error("Backup not found for slug: %s", slug)
        raise HomeAssistantError(f"Backup {slug} not found")

    @callback
    def _add_platform(
        self,
        hass: HomeAssistant,
        integration_domain: str,
        platform: BackupPlatformProtocol,
    ) -> None:
        """Add a platform to the backup manager."""
        if not hasattr(platform, "async_pre_backup") or not hasattr(
            platform, "async_post_backup"
        ):
            LOGGER.warning(
                "%s does not implement required functions for the backup platform",
                integration_domain,
            )
            return
        self.platforms[integration_domain] = platform

    async def async_pre_backup_actions(self, **kwargs: Any) -> None:
        """Perform pre backup actions."""
        if not self.loaded_platforms:
            await self.load_platforms()

        pre_backup_results = await asyncio.gather(
            *(
                platform.async_pre_backup(self.hass)
                for platform in self.platforms.values()
            ),
            return_exceptions=True,
        )
        for result in pre_backup_results:
            if isinstance(result, Exception):  # comment for Assignment 2
                raise result

    async def async_post_backup_actions(self, **kwargs: Any) -> None:
        """Perform post backup actions."""
        if not self.loaded_platforms:
            await self.load_platforms()

        post_backup_results = await asyncio.gather(
            *(
                platform.async_post_backup(self.hass)
                for platform in self.platforms.values()
            ),
            return_exceptions=True,  # Ensure exceptions are captured
        )

        for result, platform in zip(
            post_backup_results, self.platforms.keys(), strict=False
        ):
            if isinstance(result, Exception):
                LOGGER.error(
                    "Error occurred in post_backup for %s: %s",
                    platform,
                    result,
                )
                raise result  # Propagate the exception

    async def load_platforms(self) -> None:
        """Load backup platforms."""
        await integration_platform.async_process_integration_platforms(
            self.hass, DOMAIN, self._add_platform, wait_for_platforms=True
        )
        LOGGER.debug("Loaded %s platforms", len(self.platforms))
        self.loaded_platforms = True

    @abc.abstractmethod
    async def async_restore_backup(self, slug: str, **kwargs: Any) -> None:
        """Restore a backup."""

    @abc.abstractmethod
    async def async_create_backup(self, **kwargs: Any) -> Backup:
        """Generate a backup."""

    @abc.abstractmethod
    async def async_get_backups(self, **kwargs: Any) -> dict[str, Backup]:
        """Get backups.

        Return a dictionary of Backup instances keyed by their slug.
        """

    @abc.abstractmethod
    async def async_get_backup(self, *, slug: str, **kwargs: Any) -> Backup | None:
        """Get a backup."""

    @abc.abstractmethod
    async def async_remove_backup(self, *, slug: str, **kwargs: Any) -> None:
        """Remove a backup."""


class BackupManager(BaseBackupManager):
    """Backup manager for the Backup integration."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the backup manager."""
        super().__init__(hass=hass)
        self.backup_dir = Path(hass.config.path("backups"))
        self.loaded_backups = False

    async def async_get_backups(self, **kwargs: Any) -> dict[str, Backup]:
        """Get backups."""
        if not self.loaded_backups:
            await self.load_backups()
        return self.backups

    async def async_restore_backup(self, slug: str, **kwargs: Any) -> None:
        """Restore a backup."""
        try:
            LOGGER.info("Starting restore process for backup slug: %s", slug)

            # Fetch the backup
            backup = await self.async_get_backup(slug=slug)

            # Explicitly check for None
            if backup is None:
                self._raise_backup_not_found(slug)

            # At this point, mypy knows backup is not None
            assert (
                backup is not None
            )  # For additional type safety (helps static checkers)

            # Ensure the backup path exists
            if not backup.path.exists():
                self._raise_backup_not_found(slug)

            # Write the restore metadata
            restore_file = Path(self.hass.config.path(RESTORE_BACKUP_FILE))
            restore_file.write_text(
                json.dumps({"path": backup.path.as_posix()}),
                encoding="utf-8",
            )

            # Trigger Home Assistant restart
            await self.hass.services.async_call("homeassistant", "restart", {})
            LOGGER.info(
                "Restore process completed successfully for backup slug: %s", slug
            )

        except Exception as exc:
            LOGGER.error(
                "Restore process failed for backup slug: %s. Error: %s: %s",
                slug,
                type(exc).__name__,
                str(exc),
            )
            raise HomeAssistantError(f"Failed to restore backup: {exc}") from exc

    async def load_backups(self) -> None:
        """Load data of stored backup files."""
        backups = await self.hass.async_add_executor_job(self._read_backups)
        LOGGER.debug("Loaded %s backups", len(backups))
        self.backups = backups
        self.loaded_backups = True

    def _read_backups(self) -> dict[str, Backup]:
        """Read backups from disk with enhanced error handling."""
        backups: dict[str, Backup] = {}
        for backup_path in self.backup_dir.glob("*.tar"):
            try:
                LOGGER.debug("Processing backup file: %s", backup_path)
                with tarfile.open(backup_path, "r:", bufsize=BUF_SIZE) as backup_file:
                    if data_file := backup_file.extractfile("./backup.json"):
                        data = json_loads_object(data_file.read())
                        backup = Backup(
                            slug=cast(str, data["slug"]),
                            name=cast(str, data["name"]),
                            date=cast(str, data["date"]),
                            path=backup_path,
                            size=round(backup_path.stat().st_size / 1_048_576, 2),
                        )
                        backups[backup.slug] = backup
            except (OSError, TarError) as err:
                LOGGER.error("Failed to process backup file %s: %s", backup_path, err)
            except json.JSONDecodeError as err:
                LOGGER.error("Failed to parse JSON in %s: %s", backup_path, err)
            except KeyError as err:
                LOGGER.error(
                    "Missing metadata key in backup file %s: %s", backup_path, err
                )

        LOGGER.info("Read %d valid backups from disk", len(backups))
        return backups

    async def async_get_backup(self, *, slug: str, **kwargs: Any) -> Backup | None:
        """Return a backup."""
        if not self.loaded_backups:
            await self.load_backups()

        backup = self.backups.get(slug)
        if backup is None or not backup.path.exists():
            LOGGER.debug(
                "Removing tracked backup (%s) that does not exist on the expected path %s",
                slug,
                backup.path if backup else "Unknown",
            )
            self.backups.pop(slug, None)
            return None

        return backup

    async def async_remove_backup(self, *, slug: str, **kwargs: Any) -> None:
        """Remove a backup."""
        if (backup := await self.async_get_backup(slug=slug)) is None:
            return

        await self.hass.async_add_executor_job(backup.path.unlink, True)
        LOGGER.debug("Removed backup located at %s", backup.path)
        self.backups.pop(slug)

    async def async_create_backup(self, **kwargs: Any) -> Backup:
        """Generate a backup with enhanced error handling and logging."""
        if self.backing_up:
            LOGGER.error("Backup process already in progress, aborting request")
            raise HomeAssistantError("Backup already in progress")

        try:
            self.backing_up = True
            LOGGER.info("Starting the backup process")

            await self.async_pre_backup_actions()

            date_str = dt_util.now().isoformat()
            LOGGER.debug("Backup timestamp: %s", date_str)

            backup_name = f"Backup {HAVERSION}"
            slug = _generate_slug(date_str, backup_name)
            LOGGER.debug("Generated backup slug: %s", slug)

            backup_data = {
                "slug": slug,
                "name": backup_name,
                "date": date_str,
                "folders": ["homeassistant"],
                "homeassistant": {"version": HAVERSION},
                "compressed": True,
            }

            tar_file_path = Path(self.backup_dir, f"{backup_data['slug']}.tar")
            LOGGER.debug("Backup file path: %s", tar_file_path)

            size_in_bytes = await self.hass.async_add_executor_job(
                self._mkdir_and_generate_backup_contents,
                tar_file_path,
                backup_data,
            )

            backup = Backup(
                slug=slug,
                name=backup_name,
                date=date_str,
                path=tar_file_path,
                size=round(size_in_bytes / 1_048_576, 2),  # Convert bytes to MB
            )

            self.backups[slug] = backup
            if backup is not None:
                LOGGER.info(
                    "Backup process completed successfully: %s", backup.as_dict()
                )

        except Exception as exc:
            LOGGER.error(
                "Backup process failed. Error: %s: %s",
                type(exc).__name__,
                str(exc),
            )
            raise HomeAssistantError("Backup process encountered an error.") from exc

        else:  # Move the return statement to an else block
            return backup

        finally:
            self.backing_up = False
            LOGGER.info("Backup process state reset")

    def _mkdir_and_generate_backup_contents(
        self,
        tar_file_path: Path,
        backup_data: dict[str, Any],
    ) -> int:
        """Generate backup contents and return the size."""
        if not self.backup_dir.exists():
            LOGGER.debug("Creating backup directory")
            self.backup_dir.mkdir()

        outer_secure_tarfile = SecureTarFile(
            tar_file_path, "w", gzip=False, bufsize=BUF_SIZE
        )
        with outer_secure_tarfile as outer_secure_tarfile_tarfile:
            raw_bytes = json_bytes(backup_data)
            fileobj = io.BytesIO(raw_bytes)
            tar_info = tarfile.TarInfo(name="./backup.json")
            tar_info.size = len(raw_bytes)
            tar_info.mtime = int(time.time())
            outer_secure_tarfile_tarfile.addfile(tar_info, fileobj=fileobj)
            with outer_secure_tarfile.create_inner_tar(
                "./homeassistant.tar.gz", gzip=True
            ) as core_tar:
                atomic_contents_add(
                    tar_file=core_tar,
                    origin_path=Path(self.hass.config.path()),
                    excludes=EXCLUDE_FROM_BACKUP,
                    arcname="data",
                )

        return tar_file_path.stat().st_size


def _generate_slug(date: str, name: str) -> str:
    """Generate a backup slug."""
    return hashlib.sha1(f"{date} - {name}".lower().encode()).hexdigest()[:8]
