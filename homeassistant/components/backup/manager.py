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
            if isinstance(result, Exception):
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
            return_exceptions=True,
        )
        for result in post_backup_results:
            if isinstance(result, Exception):
                raise result

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


def _file_has_changed(current_file: Path, last_backup_file: Path) -> bool:
    """Compare file checksums to detect changes."""
    if not last_backup_file.exists():
        return True  # If file does not exist in the last backup, it is new.

    current_hash = hashlib.sha256(current_file.read_bytes()).hexdigest()
    backup_hash = hashlib.sha256(last_backup_file.read_bytes()).hexdigest()
    return current_hash != backup_hash


class BackupManager(BaseBackupManager):
    """Backup manager for the Backup integration."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the backup manager."""
        super().__init__(hass=hass)
        self.backup_dir = Path(hass.config.path("backups"))
        self.loaded_backups = False

    def _get_last_full_backup(self) -> Backup | None:
        """Return the most recent full backup."""
        for backup in sorted(self.backups.values(), key=lambda b: b.date, reverse=True):
            if backup.name.startswith("Full Backup"):
                return backup
        return None

    async def load_backups(self) -> None:
        """Load data of stored backup files."""
        backups = await self.hass.async_add_executor_job(self._read_backups)
        LOGGER.debug("Loaded %s backups", len(backups))
        self.backups = backups
        self.loaded_backups = True

    def _read_backups(self) -> dict[str, Backup]:
        """Read backups from disk."""
        backups: dict[str, Backup] = {}
        for backup_path in self.backup_dir.glob("*.tar"):
            try:
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
            except (OSError, TarError, json.JSONDecodeError, KeyError) as err:
                LOGGER.warning("Unable to read backup %s: %s", backup_path, err)
        return backups

    async def async_get_backups(self, **kwargs: Any) -> dict[str, Backup]:
        """Return backups."""
        if not self.loaded_backups:
            await self.load_backups()

        return self.backups

    async def async_get_backup(self, *, slug: str, **kwargs: Any) -> Backup | None:
        """Return a backup."""
        if not self.loaded_backups:
            await self.load_backups()

        if not (backup := self.backups.get(slug)):
            return None

        if not backup.path.exists():
            LOGGER.debug(
                (
                    "Removing tracked backup (%s) that does not exists on the expected"
                    " path %s"
                ),
                backup.slug,
                backup.path,
            )
            self.backups.pop(slug)
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
        """Generate a backup."""
        if self.backing_up:
            raise HomeAssistantError("Backup already in progress")

        try:
            self.backing_up = True
            await self.async_pre_backup_actions()

            date_str = dt_util.now().isoformat()
            last_full_backup = self._get_last_full_backup()

            if not last_full_backup:
                backup_name = f"Full Backup {HAVERSION}"
                LOGGER.info("No full backup found. Creating a full backup")
            else:
                backup_name = f"Incremental Backup {HAVERSION}"
                LOGGER.info(
                    "Creating incremental backup based on %s", last_full_backup.name
                )

            slug = _generate_slug(date_str, backup_name)

            backup_data = {
                "slug": slug,
                "name": backup_name,
                "date": date_str,
                "type": "partial",
                "folders": ["homeassistant"],
                "homeassistant": {"version": HAVERSION},
                "compressed": True,
            }
            tar_file_path = Path(self.backup_dir, f"{backup_data['slug']}.tar")
            # Determine if a full backup already exists
            last_full_backup = None
            for backup in self.backups.values():
                if backup.name.startswith("Core") and backup.path.exists():
                    last_full_backup = backup
                    break

            # Create full or incremental backup
            if not last_full_backup:
                # Create a full backup if no prior full backup exists
                size_in_bytes = await self.hass.async_add_executor_job(
                    self._mkdir_and_generate_backup_contents,
                    tar_file_path,
                    backup_data,
                )
            else:
                # Create incremental backup
                size_in_bytes = await self.hass.async_add_executor_job(
                    self._create_incremental_backup_contents,
                    tar_file_path,
                    last_full_backup.path,
                )

            backup = Backup(
                slug=slug,
                name=backup_name,
                date=date_str,
                path=tar_file_path,
                size=round(size_in_bytes / 1_048_576, 2),
            )
            if self.loaded_backups:
                self.backups[slug] = backup
            LOGGER.debug("Generated new backup with slug %s", slug)
            return backup
        finally:
            self.backing_up = False
            await self.async_post_backup_actions()

    def _create_incremental_backup_contents(
        self, tar_file_path: Path, last_full_backup_path: Path
    ) -> int:
        """Generate an incremental backup by comparing with the last full backup."""
        LOGGER.info("Generating incremental backup at %s", tar_file_path)

        backup_data = {
            "slug": tar_file_path.stem,
            "name": f"Incremental Backup {tar_file_path.stem}",
            "date": dt_util.now().isoformat(),
            "backup_type": "incremental",
        }

        # Track files from the last full backup
        last_backup_files = list(Path(last_full_backup_path).parent.glob("**/*"))

        with tarfile.open(tar_file_path, "w:gz") as tar:
            # Add backup.json metadata first
            raw_bytes = json_bytes(backup_data)
            fileobj = io.BytesIO(raw_bytes)
            tar_info = tarfile.TarInfo(name="backup.json")
            tar_info.size = len(raw_bytes)
            tar_info.mtime = int(time.time())
            tar.addfile(tar_info, fileobj)

            # Add only changed or new files
            for file in Path(self.hass.config.path()).glob("**/*"):
                if not file.is_file():
                    continue

                corresponding_file = next(
                    (b for b in last_backup_files if b.name == file.name), None
                )
                if corresponding_file is None or _file_has_changed(
                    file, corresponding_file
                ):
                    LOGGER.debug(
                        "Adding updated/new file to incremental backup: %s", file
                    )
                    tar.add(file, arcname=file.relative_to(self.hass.config.path()))
                else:
                    LOGGER.debug("Skipping unchanged file: %s", file)

        return tar_file_path.stat().st_size

    def _mkdir_and_generate_backup_contents(
        self,
        tar_file_path: Path,
        backup_data: dict[str, Any],
    ) -> int:
        """Generate full backup contents and return the size."""
        if not self.backup_dir.exists():
            LOGGER.debug("Creating backup directory")
            self.backup_dir.mkdir()

        # Add backup_type metadata
        backup_data["backup_type"] = "full"

        outer_secure_tarfile = SecureTarFile(
            tar_file_path, "w", gzip=False, bufsize=BUF_SIZE
        )
        with outer_secure_tarfile as outer_secure_tarfile_tarfile:
            # Write the backup.json metadata
            raw_bytes = json_bytes(backup_data)
            fileobj = io.BytesIO(raw_bytes)
            tar_info = tarfile.TarInfo(name="./backup.json")
            tar_info.size = len(raw_bytes)
            tar_info.mtime = int(time.time())
            outer_secure_tarfile_tarfile.addfile(tar_info, fileobj=fileobj)

            # Add the Home Assistant data directory to the backup
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

    async def async_restore_backup(self, slug: str, **kwargs: Any) -> None:
        """Restore a backup.

        This will write the restore information to .HA_RESTORE which
        will be handled during startup by the restore_backup module.
        """
        if (backup := await self.async_get_backup(slug=slug)) is None:
            raise HomeAssistantError(f"Backup {slug} not found")

        def _write_restore_file() -> None:
            """Write the restore file."""
            Path(self.hass.config.path(RESTORE_BACKUP_FILE)).write_text(
                json.dumps({"path": backup.path.as_posix()}),
                encoding="utf-8",
            )

        await self.hass.async_add_executor_job(_write_restore_file)
        await self.hass.services.async_call("homeassistant", "restart", {})


def _generate_slug(date: str, name: str) -> str:
    """Generate a backup slug."""
    return hashlib.sha1(f"{date} - {name}".lower().encode()).hexdigest()[:8]
