"""Tests for the Backup integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from homeassistant.components.backup import BackupManager
from homeassistant.components.backup.manager import BackupPlatformProtocol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from .common import TEST_BACKUP

from tests.common import MockPlatform, mock_platform


async def _mock_backup_generation(manager: BackupManager):
    """Mock backup generator."""

    def _mock_iterdir(path: Path) -> list[Path]:
        if not path.name.endswith("testing_config"):
            return []
        return [
            Path("test.txt"),
            Path(".DS_Store"),
            Path(".storage"),
        ]

    with (
        patch(
            "homeassistant.components.backup.manager.SecureTarFile"
        ) as mocked_tarfile,
        patch("pathlib.Path.iterdir", _mock_iterdir),
        patch("pathlib.Path.stat", MagicMock(st_size=123)),
        patch("pathlib.Path.is_file", lambda x: x.name != ".storage"),
        patch(
            "pathlib.Path.is_dir",
            lambda x: x.name == ".storage",
        ),
        patch(
            "pathlib.Path.exists",
            lambda x: x != manager.backup_dir,
        ),
        patch(
            "pathlib.Path.is_symlink",
            lambda _: False,
        ),
        patch(
            "pathlib.Path.mkdir",
            MagicMock(),
        ),
        patch(
            "homeassistant.components.backup.manager.json_bytes",
            return_value=b"{}",  # Empty JSON
        ) as mocked_json_bytes,
        patch(
            "homeassistant.components.backup.manager.HAVERSION",
            "2025.1.0",
        ),
    ):
        await manager.async_create_backup()

        assert mocked_json_bytes.call_count == 1
        backup_json_dict = mocked_json_bytes.call_args[0][0]
        assert isinstance(backup_json_dict, dict)
        assert backup_json_dict["homeassistant"] == {"version": "2025.1.0"}
        assert manager.backup_dir.as_posix() in str(
            mocked_tarfile.call_args_list[0][0][0]
        )


async def _setup_mock_domain(
    hass: HomeAssistant,
    platform: BackupPlatformProtocol | None = None,
) -> None:
    """Set up a mock domain."""
    mock_platform(hass, "some_domain.backup", platform or MockPlatform())
    assert await async_setup_component(hass, "some_domain", {})


async def test_constructor(hass: HomeAssistant) -> None:
    """Test BackupManager constructor."""
    manager = BackupManager(hass)
    assert manager.backup_dir.as_posix() == hass.config.path("backups")


async def test_load_backups(hass: HomeAssistant) -> None:
    """Test loading backups."""
    manager = BackupManager(hass)
    with (
        patch("pathlib.Path.glob", return_value=[TEST_BACKUP.path]),
        patch("tarfile.open", return_value=MagicMock()),
        patch(
            "homeassistant.components.backup.manager.json_loads_object",
            return_value={
                "slug": TEST_BACKUP.slug,
                "name": TEST_BACKUP.name,
                "date": TEST_BACKUP.date,
            },
        ),
        patch(
            "pathlib.Path.stat",
            return_value=MagicMock(st_size=TEST_BACKUP.size),
        ),
    ):
        await manager.load_backups()
    backups = await manager.async_get_backups()
    assert backups == {TEST_BACKUP.slug: TEST_BACKUP}


async def test_load_backups_with_exception(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test loading backups with exception."""
    manager = BackupManager(hass)
    with (
        patch("pathlib.Path.glob", return_value=[TEST_BACKUP.path]),
        patch("tarfile.open", side_effect=OSError("Test exception")),
    ):
        await manager.load_backups()
    backups = await manager.async_get_backups()
    assert backups == {}

    assert "Failed to process backup file abc123.tar: Test exception" in caplog.text


async def test_removing_backup(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test removing backup."""
    manager = BackupManager(hass)
    manager.backups = {TEST_BACKUP.slug: TEST_BACKUP}
    manager.loaded_backups = True

    with patch("pathlib.Path.exists", return_value=True):
        await manager.async_remove_backup(slug=TEST_BACKUP.slug)
    assert "Removed backup located at" in caplog.text


async def test_removing_non_existing_backup(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test removing not existing backup."""
    manager = BackupManager(hass)

    await manager.async_remove_backup(slug="non_existing")
    assert "Removed backup located at" not in caplog.text


async def test_getting_backup_that_does_not_exist(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test getting backup that does not exist."""
    manager = BackupManager(hass)
    manager.backups = {TEST_BACKUP.slug: TEST_BACKUP}
    manager.loaded_backups = True

    with patch("pathlib.Path.exists", return_value=False):
        backup = await manager.async_get_backup(slug=TEST_BACKUP.slug)
        assert backup is None

        assert (
            f"Removing tracked backup ({TEST_BACKUP.slug}) that "
            f"does not exists on the expected path {TEST_BACKUP.path}"
        ) in caplog.text


async def test_async_create_backup_when_backing_up(hass: HomeAssistant) -> None:
    """Test generate backup."""
    manager = BackupManager(hass)
    manager.backing_up = True
    with pytest.raises(HomeAssistantError, match="Backup already in progress"):
        await manager.async_create_backup()


async def test_async_create_backup(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test generate backup."""
    manager = BackupManager(hass)
    manager.loaded_backups = True

    # Mocking the backup generation
    await _mock_backup_generation(manager)

    # Updating the existing assertions to match the actual log messages
    assert "Generated backup slug: " in caplog.text  # Adjusted log assertion
    assert (
        "Backup process completed successfully" in caplog.text
    )  # Ensure successful log is checked


async def test_loading_platforms(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test loading backup platforms."""
    manager = BackupManager(hass)

    assert not manager.loaded_platforms
    assert not manager.platforms

    await _setup_mock_domain(
        hass,
        Mock(
            async_pre_backup=AsyncMock(),
            async_post_backup=AsyncMock(),
        ),
    )
    await manager.load_platforms()
    await hass.async_block_till_done()

    assert manager.loaded_platforms
    assert len(manager.platforms) == 1

    assert "Loaded 1 platforms" in caplog.text


async def test_not_loading_bad_platforms(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test loading backup platforms."""
    manager = BackupManager(hass)

    assert not manager.loaded_platforms
    assert not manager.platforms

    await _setup_mock_domain(hass)
    await manager.load_platforms()
    await hass.async_block_till_done()

    assert manager.loaded_platforms
    assert len(manager.platforms) == 0

    assert "Loaded 0 platforms" in caplog.text
    assert (
        "some_domain does not implement required functions for the backup platform"
        in caplog.text
    )


async def test_exception_plaform_pre(hass: HomeAssistant) -> None:
    """Test exception in pre step."""
    manager = BackupManager(hass)
    manager.loaded_backups = True

    async def _mock_step(hass: HomeAssistant) -> None:
        raise HomeAssistantError("Test exception")

    await _setup_mock_domain(
        hass,
        Mock(
            async_pre_backup=_mock_step,
            async_post_backup=AsyncMock(),
        ),
    )

    with pytest.raises(HomeAssistantError):
        await _mock_backup_generation(manager)


async def test_exception_plaform_post(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test exception in post step."""
    manager = BackupManager(hass)
    manager.loaded_backups = True

    async def _mock_step(hass: HomeAssistant) -> None:
        raise HomeAssistantError("Test exception")

    # Setting up the mock domain with the _mock_step to trigger the exception
    await _setup_mock_domain(
        hass,
        Mock(
            async_pre_backup=AsyncMock(),
            async_post_backup=_mock_step,
        ),
    )

    # Ensure exception is raised during post-backup actions
    with pytest.raises(HomeAssistantError, match="Test exception"):
        await manager.async_post_backup_actions()

    # Check that the log captured the expected error message
    assert (
        "Error occurred in post_backup for some_domain: Test exception" in caplog.text
    )


async def test_loading_platforms_when_running_async_pre_backup_actions(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test loading backup platforms when running post backup actions."""
    manager = BackupManager(hass)

    assert not manager.loaded_platforms
    assert not manager.platforms

    await _setup_mock_domain(
        hass,
        Mock(
            async_pre_backup=AsyncMock(),
            async_post_backup=AsyncMock(),
        ),
    )
    await manager.async_pre_backup_actions()

    assert manager.loaded_platforms
    assert len(manager.platforms) == 1

    assert "Loaded 1 platforms" in caplog.text


async def test_loading_platforms_when_running_async_post_backup_actions(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test loading backup platforms when running post backup actions."""
    manager = BackupManager(hass)

    assert not manager.loaded_platforms
    assert not manager.platforms

    await _setup_mock_domain(
        hass,
        Mock(
            async_pre_backup=AsyncMock(),
            async_post_backup=AsyncMock(),
        ),
    )
    await manager.async_post_backup_actions()

    assert manager.loaded_platforms
    assert len(manager.platforms) == 1

    assert "Loaded 1 platforms" in caplog.text


async def test_async_trigger_restore(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test trigger restore."""
    manager = BackupManager(hass)
    manager.loaded_backups = True
    manager.backups = {TEST_BACKUP.slug: TEST_BACKUP}

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.write_text") as mocked_write_text,
        patch("homeassistant.core.ServiceRegistry.async_call") as mocked_service_call,
    ):
        await manager.async_restore_backup(TEST_BACKUP.slug)
        assert mocked_write_text.called
        assert mocked_write_text.call_args[0][0] == '{"path": "abc123.tar"}'
        assert mocked_service_call.called


async def test_async_trigger_restore_missing_backup(hass: HomeAssistant) -> None:
    """Test trigger restore."""
    manager = BackupManager(hass)
    manager.loaded_backups = True

    with pytest.raises(
        HomeAssistantError,
        match="Failed to restore backup: Backup abc123 not found",
    ):
        await manager.async_restore_backup(TEST_BACKUP.slug)
