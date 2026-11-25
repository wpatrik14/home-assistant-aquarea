"""Moving a config entry of the old `aquarea` domain to `aquarea_smart_cloud`.

The domain collided with heishamon's (same folder name). The old entries stay
in storage, so adding the integration offers to move one: entities (ids and
customisations), devices and the lifetime totals follow, the old entry goes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aquarea_smart_cloud.const import (
    CONF_LEGACY_ENTRY,
    DOMAIN,
    LEGACY_DOMAIN,
)
from custom_components.aquarea_smart_cloud.lifetime import STORAGE_VERSION

from .conftest import DEVICE_ID, PASSWORD, USERNAME

UID = f"{DEVICE_ID}_pump_status"


def _legacy_entry(hass: HomeAssistant, **kwargs) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=LEGACY_DOMAIN,
        title=USERNAME,
        unique_id=USERNAME.lower(),
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


def _old_registry_rows(hass: HomeAssistant, old: MockConfigEntry) -> str:
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=old.entry_id, identifiers={(LEGACY_DOMAIN, DEVICE_ID)}
    )
    entity = er.async_get(hass).async_get_or_create(
        "binary_sensor",
        LEGACY_DOMAIN,
        UID,
        config_entry=old,
        device_id=device.id,
        suggested_object_id="my_pump",
    )
    er.async_get(hass).async_update_entity(entity.entity_id, name="Custom name")
    return entity.entity_id


async def _move(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "legacy_import"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "legacy_import"
    assert result["description_placeholders"] == {"username": USERNAME}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_entry_entities_devices_and_totals_move(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """The new entry takes over everything; the old entry is gone."""
    old = _legacy_entry(hass)
    entity_id = _old_registry_rows(hass, old)
    old_store = Store(hass, STORAGE_VERSION, f"{LEGACY_DOMAIN}.lifetime.{old.entry_id}")
    await old_store.async_save({"devices": {DEVICE_ID: {"marker": 1}}})

    await _move(hass)

    (new,) = hass.config_entries.async_entries(DOMAIN)
    assert new.state is ConfigEntryState.LOADED
    assert new.unique_id == USERNAME.lower()
    assert new.data[CONF_USERNAME] == USERNAME
    assert CONF_LEGACY_ENTRY not in new.data
    assert hass.config_entries.async_get_entry(old.entry_id) is None

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    assert entry is not None
    assert entry.platform == DOMAIN
    assert entry.config_entry_id == new.entry_id
    assert entry.name == "Custom name"
    assert entry.unique_id == UID

    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), new.entry_id)
    assert device.identifiers == {(DOMAIN, DEVICE_ID)}
    assert device.config_entries == {new.entry_id}
    assert not dr.async_entries_for_config_entry(dr.async_get(hass), old.entry_id)

    moved = Store(hass, STORAGE_VERSION, f"{DOMAIN}.lifetime.{new.entry_id}")
    assert await moved.async_load() == {"devices": {DEVICE_ID: {"marker": 1}}}
    assert await old_store.async_load() is None


async def test_no_legacy_entry_goes_straight_to_the_form(
    hass: HomeAssistant,
) -> None:
    """Without an old entry the user step is unchanged."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_foreign_entries_of_the_old_domain_are_ignored(
    hass: HomeAssistant,
) -> None:
    """A heishamon entry (no Panasonic credentials) is not offered."""
    MockConfigEntry(domain=LEGACY_DOMAIN, data={"discovery_prefix": "x"}).add_to_hass(
        hass
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_another_account_can_be_added_instead(hass: HomeAssistant) -> None:
    """The menu's second option leads to the usual credentials form."""
    _legacy_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_import_aborts_when_the_account_is_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The account already has a new entry: nothing is created twice."""
    mock_config_entry.add_to_hass(hass)
    _legacy_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "legacy_import"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_import_without_the_old_entry_falls_back_to_the_form(
    hass: HomeAssistant,
) -> None:
    """The old entry vanished between the menu and the choice."""
    old = _legacy_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    await hass.config_entries.async_remove(old.entry_id)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "legacy_import"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_setup_after_an_interruption_finishes_the_move(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """The old entry is already gone (earlier run): the marker is still dropped."""
    new = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME.lower(),
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_LEGACY_ENTRY: "does-not-exist",
        },
    )
    new.add_to_hass(hass)
    await hass.config_entries.async_setup(new.entry_id)
    await hass.async_block_till_done()
    assert new.state is ConfigEntryState.LOADED
    assert CONF_LEGACY_ENTRY not in new.data


async def test_a_blocked_entity_is_kept_not_fatal(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock, caplog
) -> None:
    """An entity that cannot move is logged; the rest of the move goes on."""
    old = _legacy_entry(hass)
    registry = er.async_get(hass)
    entity_id = _old_registry_rows(hass, old)
    # The new domain already has this unique id (a half-finished earlier run).
    registry.async_get_or_create("binary_sensor", DOMAIN, UID, suggested_object_id="x")

    await _move(hass)

    assert "Not moving" in caplog.text
    # The new domain's own entity stays; the old duplicate goes with its entry.
    assert registry.async_get(entity_id) is None
    assert registry.async_get("binary_sensor.x") is not None
    (new,) = hass.config_entries.async_entries(DOMAIN)
    assert new.state is ConfigEntryState.LOADED


async def test_registry_refusals_are_logged_not_fatal(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock, caplog
) -> None:
    """A loaded entity or a conflicting device does not stop the move."""
    old = _legacy_entry(hass)
    _old_registry_rows(hass, old)

    with (
        patch.object(
            er.EntityRegistry,
            "async_update_entity_platform",
            side_effect=ValueError("loaded"),
        ),
        patch.object(
            dr.DeviceRegistry, "async_update_device", side_effect=ValueError("taken")
        ),
    ):
        await _move(hass)

    assert "Not moving binary_sensor.my_pump: loaded" in caplog.text
    assert "taken" in caplog.text
    (new,) = hass.config_entries.async_entries(DOMAIN)
    assert new.state is ConfigEntryState.LOADED


async def test_a_running_old_entry_is_unloaded_first(
    hass: HomeAssistant, mock_aquarea_client: AsyncMock
) -> None:
    """The old folder is still installed and its entry loaded: unload it, then move."""
    old = _legacy_entry(hass)
    old.mock_state(hass, ConfigEntryState.LOADED)
    entity_id = _old_registry_rows(hass, old)

    with patch.object(
        hass.config_entries, "async_unload", AsyncMock(return_value=True)
    ) as unload:
        await _move(hass)

    assert unload.await_args_list[0] == call(old.entry_id)
    assert er.async_get(hass).async_get(entity_id).platform == DOMAIN
