"""Migration of config entries, through Home Assistant's entry manager.

An entry stored with a newer version than the flow supports (restored backup,
or an older release installed again) must load instead of ending in
`migration_error`. Home Assistant itself refuses a newer *major* version before
it calls `async_migrate_entry` (config_entries.py, ConfigEntry.async_migrate),
so only a newer minor version reaches the hook through the entry manager; the
newer major version is checked by calling the hook directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquarea import async_migrate_entry
from custom_components.aquarea.config_flow import AquareaConfigFlow
from custom_components.aquarea.const import DOMAIN

from .conftest import DEVICE_ID


async def test_current_version_untouched(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An entry at the supported version is left as it is."""
    mock_config_entry.add_to_hass(hass)
    data = dict(mock_config_entry.data)

    assert await async_migrate_entry(hass, mock_config_entry)
    assert mock_config_entry.version == AquareaConfigFlow.VERSION
    assert dict(mock_config_entry.data) == data


async def test_newer_major_version_is_set_back(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A newer major version keeps its data and gets the supported version."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, version=AquareaConfigFlow.VERSION + 1
    )
    data = dict(mock_config_entry.data)

    assert await async_migrate_entry(hass, mock_config_entry)
    assert mock_config_entry.version == AquareaConfigFlow.VERSION
    assert mock_config_entry.minor_version == AquareaConfigFlow.MINOR_VERSION
    assert dict(mock_config_entry.data) == data
    assert "newer than the supported" in caplog.text


async def test_newer_minor_version_loads(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry with a newer minor version is set back and loads."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, minor_version=AquareaConfigFlow.MINOR_VERSION + 1
    )
    data = dict(mock_config_entry.data)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.version == AquareaConfigFlow.VERSION
    assert mock_config_entry.minor_version == AquareaConfigFlow.MINOR_VERSION
    assert dict(mock_config_entry.data) == data
    assert "newer than the supported" in caplog.text


async def test_newer_version_without_credentials_fails(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A newer entry without a username is refused."""
    entry = MockConfigEntry(domain="aquarea", data={}, version=2)
    entry.add_to_hass(hass)

    assert not await async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert "unusable" in caplog.text


# The pump status sensor became a binary sensor in version 1.2.


def _issues(hass: HomeAssistant) -> list[ir.IssueEntry]:
    return [
        issue
        for (domain, _), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    ]


def _old_pump_sensor(
    hass: HomeAssistant, entry: MockConfigEntry, **kwargs
) -> er.RegistryEntry:
    return er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DEVICE_ID}_pump_status",
        config_entry=entry,
        suggested_object_id="heat_pump_pump_status",
        translation_key="pump_status",
        **kwargs,
    )


@pytest.fixture
def old_entry(hass: HomeAssistant, mock_config_entry: MockConfigEntry):
    """The config entry as a release before 1.2 stored it."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, minor_version=1)
    return mock_config_entry


async def test_migration_moves_pump_status_sensor(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """The registry entry moves to the binary_sensor domain, same unique id."""
    registry = er.async_get(hass)
    _old_pump_sensor(hass, old_entry)

    assert await async_migrate_entry(hass, old_entry)

    uid = f"{DEVICE_ID}_pump_status"
    assert registry.async_get_entity_id("sensor", DOMAIN, uid) is None
    assert (
        registry.async_get_entity_id("binary_sensor", DOMAIN, uid)
        == "binary_sensor.heat_pump_pump_status"
    )
    assert old_entry.minor_version == AquareaConfigFlow.MINOR_VERSION
    assert old_entry.version == 1


async def test_migration_keeps_customisations(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """Name, icon, device class, area, labels, aliases, hidden and options carry over."""
    registry = er.async_get(hass)
    area = ar.async_get(hass).async_create("Utility room")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=old_entry.entry_id, identifiers={(DOMAIN, "dev")}
    )
    old = _old_pump_sensor(hass, old_entry, device_id=device.id)
    registry.async_update_entity(
        old.entity_id,
        name="Circulation pump",
        icon="mdi:water-pump",
        device_class="running",
        area_id=area.id,
        aliases={"pump"},
        labels={"heating"},
        categories={"scope": "cat"},
        hidden_by=er.RegistryEntryHider.USER,
    )
    registry.async_update_entity_options(
        old.entity_id, "conversation", {"should_expose": True}
    )

    assert await async_migrate_entry(hass, old_entry)

    new = registry.async_get("binary_sensor.heat_pump_pump_status")
    assert new.name == "Circulation pump"
    assert new.icon == "mdi:water-pump"
    assert new.device_class == "running"
    assert new.area_id == area.id
    assert new.aliases == {"pump"}
    assert new.labels == {"heating"}
    assert new.categories == {"scope": "cat"}
    assert new.hidden_by is er.RegistryEntryHider.USER
    assert new.device_id == device.id
    assert new.config_entry_id == old_entry.entry_id
    assert new.options["conversation"] == {"should_expose": True}


async def test_migration_keeps_disabled_and_renamed_entity_id(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """A disabled entry stays disabled, and a renamed entity id is kept."""
    registry = er.async_get(hass)
    old = _old_pump_sensor(hass, old_entry, disabled_by=er.RegistryEntryDisabler.USER)
    registry.async_update_entity(old.entity_id, new_entity_id="sensor.my_pump")

    assert await async_migrate_entry(hass, old_entry)

    new = registry.async_get("binary_sensor.my_pump")
    assert new.unique_id == f"{DEVICE_ID}_pump_status"
    assert new.disabled_by is er.RegistryEntryDisabler.USER


async def test_migration_creates_repair_issue(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """A non-fixable repair issue lists the moved entity ids."""
    _old_pump_sensor(hass, old_entry)

    assert await async_migrate_entry(hass, old_entry)

    (issue,) = _issues(hass)
    assert issue.issue_id == f"pump_entities_changed_{old_entry.entry_id}"
    assert issue.translation_key == "pump_entities_changed"
    assert issue.is_fixable is False
    assert issue.is_persistent is True
    assert issue.translation_placeholders == {
        "entry_title": old_entry.title,
        "entities": (
            "- `sensor.heat_pump_pump_status` -> `binary_sensor.heat_pump_pump_status`"
        ),
    }


async def test_migration_of_fresh_install_is_a_no_op(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """An entry without a pump status sensor changes nothing and raises no issue."""
    registry = er.async_get(hass)
    before = sorted(registry.entities)

    assert await async_migrate_entry(hass, old_entry)

    assert sorted(registry.entities) == before
    assert _issues(hass) == []
    assert old_entry.minor_version == AquareaConfigFlow.MINOR_VERSION


async def test_migration_is_idempotent(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """A second run changes nothing and adds no second issue."""
    _old_pump_sensor(hass, old_entry)
    assert await async_migrate_entry(hass, old_entry)
    snapshot = sorted(er.async_get(hass).entities)
    issue_ids = [issue.issue_id for issue in _issues(hass)]

    hass.config_entries.async_update_entry(old_entry, minor_version=1)
    assert await async_migrate_entry(hass, old_entry)

    assert sorted(er.async_get(hass).entities) == snapshot
    assert [issue.issue_id for issue in _issues(hass)] == issue_ids


async def test_migration_skips_when_target_entity_id_is_taken(
    hass: HomeAssistant,
    old_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Another entity with the new entity id: the sensor stays, and the user is told."""
    registry = er.async_get(hass)
    old = _old_pump_sensor(hass, old_entry)
    registry.async_get_or_create(
        "binary_sensor",
        "other_integration",
        "unrelated",
        suggested_object_id="heat_pump_pump_status",
    )

    assert await async_migrate_entry(hass, old_entry)

    assert registry.async_get(old.entity_id) is not None
    assert (
        registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{DEVICE_ID}_pump_status"
        )
        is None
    )
    assert "already in use" in caplog.text
    # Not hidden: a repair issue names the entities, and the migration is not
    # marked as done, so that it is tried again at the next setup.
    (issue,) = _issues(hass)
    assert issue.issue_id == f"pump_entities_not_moved_{old_entry.entry_id}"
    assert issue.translation_key == "pump_entities_not_moved"
    assert issue.is_persistent is True
    assert issue.translation_placeholders == {
        "entry_title": old_entry.title,
        "entities": (
            "- `sensor.heat_pump_pump_status` -> `binary_sensor.heat_pump_pump_status`"
        ),
    }
    assert old_entry.minor_version == 1


async def test_migration_retries_once_the_entity_id_is_free(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """After the user frees the id, the next run moves the sensor and clears the issue."""
    registry = er.async_get(hass)
    _old_pump_sensor(hass, old_entry)
    other = registry.async_get_or_create(
        "binary_sensor",
        "other_integration",
        "unrelated",
        suggested_object_id="heat_pump_pump_status",
    )
    assert await async_migrate_entry(hass, old_entry)
    assert old_entry.minor_version == 1

    registry.async_remove(other.entity_id)
    assert await async_migrate_entry(hass, old_entry)

    assert old_entry.minor_version == AquareaConfigFlow.MINOR_VERSION
    assert registry.async_get("binary_sensor.heat_pump_pump_status") is not None
    assert [issue.translation_key for issue in _issues(hass)] == [
        "pump_entities_changed"
    ]


async def test_blocked_move_is_completed_after_the_platform_registered_a_suffixed_id(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    old_entry: MockConfigEntry,
) -> None:
    """Blocked at setup, the platform registers `_2`; once the id is free it is renamed.

    The user's customisations of the sensor are copied to the renamed entry,
    the issue goes and the migration is done.
    """
    registry = er.async_get(hass)
    uid = f"{DEVICE_ID}_pump_status"
    area = ar.async_get(hass).async_create("Utility room")
    old = _old_pump_sensor(hass, old_entry)
    registry.async_update_entity(
        old.entity_id,
        name="Circulation pump",
        icon="mdi:water-pump",
        area_id=area.id,
        labels={"heating"},
        aliases={"pump"},
        hidden_by=er.RegistryEntryHider.USER,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    registry.async_update_entity_options(
        old.entity_id, "conversation", {"should_expose": True}
    )
    other = registry.async_get_or_create(
        "binary_sensor",
        "other_integration",
        "unrelated",
        suggested_object_id="heat_pump_pump_status",
    )

    # First start: blocked. The platform registers the entity under `_2`.
    await hass.config_entries.async_setup(old_entry.entry_id)
    await hass.async_block_till_done()
    assert old_entry.state is ConfigEntryState.LOADED
    assert old_entry.minor_version == 1
    assert (
        registry.async_get_entity_id("binary_sensor", DOMAIN, uid)
        == "binary_sensor.heat_pump_pump_status_2"
    )

    # Reload with the id still taken: still blocked, nothing is reported as moved.
    await hass.config_entries.async_reload(old_entry.entry_id)
    await hass.async_block_till_done()
    assert old_entry.minor_version == 1
    assert registry.async_get(old.entity_id) is not None
    assert [issue.translation_key for issue in _issues(hass)] == [
        "pump_entities_not_moved"
    ]

    # The user frees the id and reloads.
    registry.async_remove(other.entity_id)
    await hass.config_entries.async_reload(old_entry.entry_id)
    await hass.async_block_till_done()

    assert old_entry.state is ConfigEntryState.LOADED
    assert old_entry.minor_version == AquareaConfigFlow.MINOR_VERSION
    assert registry.async_get_entity_id("sensor", DOMAIN, uid) is None
    assert (
        registry.async_get_entity_id("binary_sensor", DOMAIN, uid)
        == "binary_sensor.heat_pump_pump_status"
    )
    new = registry.async_get("binary_sensor.heat_pump_pump_status")
    assert new.name == "Circulation pump"
    assert new.icon == "mdi:water-pump"
    assert new.area_id == area.id
    assert new.labels == {"heating"}
    assert new.aliases == {"pump"}
    assert new.options["conversation"] == {"should_expose": True}
    assert new.hidden_by is er.RegistryEntryHider.USER
    assert new.disabled_by is er.RegistryEntryDisabler.USER
    assert [issue.translation_key for issue in _issues(hass)] == [
        "pump_entities_changed"
    ]


async def test_direction_notice_without_a_moved_sensor(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """The lowercase direction states matter even if no pump sensor was moved."""
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DEVICE_ID}_direction",
        config_entry=old_entry,
        suggested_object_id="heat_pump_direction",
        translation_key="direction",
    )

    assert await async_migrate_entry(hass, old_entry)

    (issue,) = _issues(hass)
    assert issue.issue_id == f"direction_case_changed_{old_entry.entry_id}"
    assert issue.translation_key == "direction_case_changed"
    assert issue.is_fixable is False
    assert issue.is_persistent is True
    assert issue.translation_placeholders == {"entry_title": old_entry.title}
    assert old_entry.minor_version == AquareaConfigFlow.MINOR_VERSION


async def test_moved_sensor_notice_covers_the_direction(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """With a moved pump sensor the one issue already names the direction change."""
    _old_pump_sensor(hass, old_entry)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DEVICE_ID}_direction",
        config_entry=old_entry,
        suggested_object_id="heat_pump_direction",
    )

    assert await async_migrate_entry(hass, old_entry)

    assert [issue.translation_key for issue in _issues(hass)] == [
        "pump_entities_changed"
    ]


async def test_removing_the_entry_deletes_the_migration_issues(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    old_entry: MockConfigEntry,
) -> None:
    """The persistent issues do not outlive the entry they are about."""
    registry = er.async_get(hass)
    _old_pump_sensor(hass, old_entry)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DEVICE_ID}_direction",
        config_entry=old_entry,
        suggested_object_id="heat_pump_direction",
    )
    assert await async_migrate_entry(hass, old_entry)
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"pump_entities_not_moved_{old_entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="pump_entities_not_moved",
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"direction_case_changed_{old_entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="direction_case_changed",
    )
    assert len(_issues(hass)) == 3

    await hass.config_entries.async_remove(old_entry.entry_id)
    await hass.async_block_till_done()

    assert _issues(hass) == []


async def test_migration_with_both_entries_keeps_the_new_one(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """An interrupted earlier run left both entries: keep the new, drop the old."""
    registry = er.async_get(hass)
    _old_pump_sensor(hass, old_entry)
    new = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DEVICE_ID}_pump_status",
        config_entry=old_entry,
        suggested_object_id="heat_pump_pump_status",
    )
    registry.async_update_entity(new.entity_id, name="Keep me")

    assert await async_migrate_entry(hass, old_entry)

    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{DEVICE_ID}_pump_status")
        is None
    )
    assert registry.async_get(new.entity_id).name == "Keep me"


async def test_migration_only_touches_its_own_entry(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """The pump sensor of another config entry is not moved."""
    other = MockConfigEntry(domain=DOMAIN, data=dict(old_entry.data), minor_version=1)
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "other-device_pump_status",
        config_entry=other,
        suggested_object_id="other_pump_status",
    )

    assert await async_migrate_entry(hass, old_entry)

    assert registry.async_get("sensor.other_pump_status") is not None
    assert _issues(hass) == []


async def test_setup_runs_the_migration(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    old_entry: MockConfigEntry,
) -> None:
    """Loading an old entry moves the sensor, and the binary sensor takes over."""
    old = _old_pump_sensor(hass, old_entry)
    assert old.entity_id == "sensor.heat_pump_pump_status"

    await hass.config_entries.async_setup(old_entry.entry_id)
    await hass.async_block_till_done()

    assert old_entry.state is ConfigEntryState.LOADED
    assert old_entry.minor_version == AquareaConfigFlow.MINOR_VERSION
    assert hass.states.get("sensor.heat_pump_pump_status") is None
    assert hass.states.get("binary_sensor.heat_pump_pump_status").state == "off"
    assert len(_issues(hass)) == 1


async def test_setup_loads_when_the_move_is_blocked(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    old_entry: MockConfigEntry,
) -> None:
    """A blocked move does not stop the entry: it loads, and the issue stays."""
    _old_pump_sensor(hass, old_entry)
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "other_integration",
        "unrelated",
        suggested_object_id="heat_pump_pump_status",
    )

    await hass.config_entries.async_setup(old_entry.entry_id)
    await hass.async_block_till_done()

    assert old_entry.state is ConfigEntryState.LOADED
    assert [issue.translation_key for issue in _issues(hass)] == [
        "pump_entities_not_moved"
    ]
    # The platform registered the entity under a suffixed id, not the taken one.
    assert (
        er.async_get(hass).async_get_entity_id(
            "binary_sensor", DOMAIN, f"{DEVICE_ID}_pump_status"
        )
        == "binary_sensor.heat_pump_pump_status_2"
    )


async def test_blocked_move_has_no_direction_issue(
    hass: HomeAssistant, old_entry: MockConfigEntry
) -> None:
    """The not-moved text already covers the direction states."""
    registry = er.async_get(hass)
    _old_pump_sensor(hass, old_entry)
    registry.async_get_or_create(
        "binary_sensor",
        "other_integration",
        "unrelated",
        suggested_object_id="heat_pump_pump_status",
    )
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DEVICE_ID}_direction",
        config_entry=old_entry,
        suggested_object_id="heat_pump_direction",
    )

    assert await async_migrate_entry(hass, old_entry)

    assert [issue.translation_key for issue in _issues(hass)] == [
        "pump_entities_not_moved"
    ]


async def _blocked_setup(hass: HomeAssistant, entry: MockConfigEntry, **old_kwargs):
    """Set the entry up with the pump sensor's move blocked; return (old, other)."""
    registry = er.async_get(hass)
    old = _old_pump_sensor(hass, entry)
    if old_kwargs:
        registry.async_update_entity(old.entity_id, **old_kwargs)
    other = registry.async_get_or_create(
        "binary_sensor",
        "other_integration",
        "unrelated",
        suggested_object_id="heat_pump_pump_status",
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{DEVICE_ID}_pump_status"
        )
        == "binary_sensor.heat_pump_pump_status_2"
    )
    return old, other


async def test_user_chosen_entity_id_is_left_alone(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock, old_entry: MockConfigEntry
) -> None:
    """A new entity the user gave an id of their own is kept; only the old sensor goes."""
    registry = er.async_get(hass)
    uid = f"{DEVICE_ID}_pump_status"
    old, _other = await _blocked_setup(hass, old_entry, name="Old name")
    registry.async_update_entity(
        "binary_sensor.heat_pump_pump_status_2", new_entity_id="binary_sensor.my_pump"
    )

    await hass.config_entries.async_reload(old_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get_entity_id("binary_sensor", DOMAIN, uid) == (
        "binary_sensor.my_pump"
    )
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get("binary_sensor.my_pump").name is None
    assert old_entry.minor_version == AquareaConfigFlow.MINOR_VERSION
    assert [issue.translation_key for issue in _issues(hass)] == [
        "pump_entities_changed"
    ]


async def test_empty_old_values_do_not_wipe_the_new_entity(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock, old_entry: MockConfigEntry
) -> None:
    """Values the user set on the new entity survive the rename."""
    registry = er.async_get(hass)
    area = ar.async_get(hass).async_create("Utility room")
    _old, other = await _blocked_setup(hass, old_entry, labels={"old"})
    registry.async_update_entity(
        "binary_sensor.heat_pump_pump_status_2",
        name="Mine",
        icon="mdi:pump",
        area_id=area.id,
        labels={"new"},
    )
    registry.async_remove(other.entity_id)

    await hass.config_entries.async_reload(old_entry.entry_id)
    await hass.async_block_till_done()

    new = registry.async_get("binary_sensor.heat_pump_pump_status")
    assert new.name == "Mine"
    assert new.icon == "mdi:pump"
    assert new.area_id == area.id
    assert new.labels == {"old", "new"}


async def test_rename_refused_by_the_registry_is_blocked(
    hass: HomeAssistant,
    mock_aquarea_client: AsyncMock,
    old_entry: MockConfigEntry,
) -> None:
    """A ValueError from the registry (reserved id) keeps the issue, not a migration error."""
    registry = er.async_get(hass)
    old, other = await _blocked_setup(hass, old_entry)
    registry.async_remove(other.entity_id)
    real = er.EntityRegistry.async_update_entity

    def refuse(self, entity_id, **kwargs):
        if "new_entity_id" in kwargs:
            raise ValueError("Entity with this ID is already registered")
        return real(self, entity_id, **kwargs)

    with patch.object(er.EntityRegistry, "async_update_entity", refuse):
        await hass.config_entries.async_reload(old_entry.entry_id)
        await hass.async_block_till_done()

    assert old_entry.state is ConfigEntryState.LOADED
    assert old_entry.minor_version == 1
    assert registry.async_get(old.entity_id) is not None
    assert [issue.translation_key for issue in _issues(hass)] == [
        "pump_entities_not_moved"
    ]
