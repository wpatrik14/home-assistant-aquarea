"""The Aquarea Smart Cloud integration."""

from functools import partial
import logging
from typing import Any

import aioaquarea
import aiohttp
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .config_flow import AquareaConfigFlow
from .const import APP_VERSION_REJECTED, CONF_REFRESH_TOKEN, DOMAIN, MFA_REQUIRED
from .coordinator import (
    AGREEMENT_NOT_ACCEPTED_CODE,
    AquareaConfigEntry,
    AquareaDataUpdateCoordinator,
)
from .issues import (
    async_clear_agreement_issue,
    async_clear_app_version_issue,
    async_clear_migration_issues,
    async_clear_pump_not_moved_issue,
    async_create_agreement_issue,
    async_create_app_version_issue,
    async_create_direction_case_issue,
    async_create_pump_entities_issue,
    async_create_pump_not_moved_issue,
    async_pump_entities_issue_exists,
)
from .lifetime import LifetimeStore

_LOGGER = logging.getLogger(__name__)

# Unique id suffix of the pump status entity, a sensor before version 1.2.
PUMP_STATUS_SUFFIX = "_pump_status"
# Unique id suffix of the pump direction sensor, whose states became lowercase.
DIRECTION_SUFFIX = "_direction"

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.CLIMATE,
    Platform.BINARY_SENSOR,
    Platform.WATER_HEATER,
    Platform.SWITCH,
    Platform.SELECT,
]


def _create_client(hass: HomeAssistant, entry: AquareaConfigEntry) -> aioaquarea.Client:
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    # One session per entry, not the shared one: the login cookies live in it
    # and must not mix between accounts. Created during setup, Home Assistant
    # detaches (closes) it when the entry unloads or its setup fails.
    session = async_create_clientsession(hass)

    @callback
    def _store_refresh_token(token: str) -> None:
        """Keep the entry's refresh token current (it can rotate on every refresh)."""
        if entry.data.get(CONF_REFRESH_TOKEN) != token:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
            )

    # Without a stored token the password login is used. Setup and polling run
    # unattended: a login that needs multi-factor authentication must not text
    # a code nobody is waiting for; it starts reauth, which asks for the code.
    return aioaquarea.Client(
        session,
        username,
        password,
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        mfa_send_code=False,
        refresh_token_callback=_store_refresh_token,
    )


def _copy_customisations(
    registry: er.EntityRegistry, new_id: str, old: er.RegistryEntry
) -> None:
    """Copy what the user set by hand on the sensor; the platform fills in the rest."""
    customised: dict[str, Any] = {
        "name": old.name,
        "icon": old.icon,
        "device_class": old.device_class,
        "area_id": old.area_id,
        "aliases": old.aliases,
        "labels": old.labels,
        "categories": old.categories,
    }
    registry.async_update_entity(new_id, **customised)


def _move_pump_status_sensors(
    hass: HomeAssistant, entry: AquareaConfigEntry
) -> tuple[dict[str, str], dict[str, str]]:
    """Re-register the pump status sensors as binary sensors.

    The registry refuses a domain change on an existing entry
    (`async_update_entity(new_entity_id=...)` raises, and the registry key
    includes the domain), so the sensor entry is removed and a binary sensor
    entry with the same unique id and object id is created, carrying over what
    the user customised. The recorder history stays under the old entity id.

    Returns two mappings of old entity id -> new entity id: what was moved, and
    what was left alone because the new entity id belongs to something else.
    """
    registry = er.async_get(hass)
    moved: dict[str, str] = {}
    blocked: dict[str, str] = {}
    for old in er.async_entries_for_config_entry(registry, entry.entry_id):
        if old.domain != "sensor" or not old.unique_id.endswith(PUMP_STATUS_SUFFIX):
            continue
        object_id = old.entity_id.split(".", 1)[1]
        preferred = f"binary_sensor.{object_id}"
        existing = registry.async_get_entity_id("binary_sensor", DOMAIN, old.unique_id)
        if existing == preferred:
            # An interrupted earlier run already created the new entry; keep it.
            new_id = existing
        elif registry.async_is_registered(preferred) or hass.states.get(preferred):
            _LOGGER.warning(
                "Not moving %s: %s is already in use", old.entity_id, preferred
            )
            blocked[old.entity_id] = preferred
            continue
        elif existing is None:
            options = {key: dict(value) for key, value in old.options.items()}
            created = registry.async_get_or_create(
                "binary_sensor",
                DOMAIN,
                old.unique_id,
                config_entry=entry,
                config_subentry_id=old.config_subentry_id,
                device_id=old.device_id,
                suggested_object_id=object_id,
                disabled_by=old.disabled_by,
                hidden_by=old.hidden_by,
                get_initial_options=partial(dict, options) if options else None,
                entity_category=old.entity_category,
                has_entity_name=old.has_entity_name,
                original_name=old.original_name,
                translation_key=old.translation_key,
            )
            new_id = created.entity_id
            _copy_customisations(registry, new_id, old)
        else:
            # An earlier run was blocked and the binary sensor platform has
            # registered the entity meanwhile, under a suffixed id (_2). The
            # preferred id is free now: take it, and carry over what the user
            # set on the sensor, which that entry does not have.
            new_id = registry.async_update_entity(
                existing, new_entity_id=preferred
            ).entity_id
            for domain, value in old.options.items():
                registry.async_update_entity_options(new_id, domain, dict(value))
            registry.async_update_entity(
                new_id, disabled_by=old.disabled_by, hidden_by=old.hidden_by
            )
            _copy_customisations(registry, new_id, old)
        registry.async_remove(old.entity_id)
        moved[old.entity_id] = new_id
        _LOGGER.info("Moved entity %s -> %s", old.entity_id, new_id)
    return moved, blocked


def _has_direction_sensor(hass: HomeAssistant, entry: AquareaConfigEntry) -> bool:
    """Return whether the entry has a pump direction sensor in the registry."""
    registry = er.async_get(hass)
    return any(
        e.domain == "sensor" and e.unique_id.endswith(DIRECTION_SUFFIX)
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    )


async def async_migrate_entry(hass: HomeAssistant, entry: AquareaConfigEntry) -> bool:
    """Migrate a config entry to the current version.

    Home Assistant calls this whenever the stored version differs from the
    flow's. Entries older than 1.2 get the pump status sensor moved to the
    binary_sensor domain. An entry written by a newer release (restoring a backup, or going
    back to an older release) would otherwise end in `migration_error`. The
    data has not changed shape, so keep it and only set the version back.
    """
    supported = (AquareaConfigFlow.VERSION, AquareaConfigFlow.MINOR_VERSION)
    if entry.version == 1 and entry.minor_version < 2:
        # 1.2: the pump status sensor became a binary sensor.
        moved, blocked = _move_pump_status_sensors(hass, entry)
        if moved:
            async_create_pump_entities_issue(hass, entry, moved)
        if blocked:
            # Keep the old minor version: the move is tried again on the next
            # setup, once the user has freed the entity id, instead of being
            # given up silently.
            async_create_pump_not_moved_issue(hass, entry, blocked)
        else:
            async_clear_pump_not_moved_issue(hass, entry)
        if (
            not blocked
            and not async_pump_entities_issue_exists(hass, entry)
            and _has_direction_sensor(hass, entry)
        ):
            # No notice about the pump sensor, which also covers the direction
            # states (the moved and the not-moved text both do): the lowercase
            # change needs its own.
            async_create_direction_case_issue(hass, entry)
        if not blocked:
            hass.config_entries.async_update_entry(entry, minor_version=2)
        return True
    if (entry.version, entry.minor_version) <= supported:
        return True
    if not entry.data.get(CONF_USERNAME):
        _LOGGER.error(
            "Config entry has version %s.%s, newer than the supported %s.%s, "
            "and its data is unusable",
            entry.version,
            entry.minor_version,
            *supported,
        )
        return False
    _LOGGER.warning(
        "Config entry has version %s.%s, newer than the supported %s.%s; "
        "keeping its data and setting the version back",
        entry.version,
        entry.minor_version,
        *supported,
    )
    hass.config_entries.async_update_entry(
        entry, version=supported[0], minor_version=supported[1]
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AquareaConfigEntry) -> bool:
    """Set up Aquarea Smart Cloud from a config entry."""

    client = _create_client(hass, entry)
    coordinators: dict[str, AquareaDataUpdateCoordinator] = {}
    # The persisted lifetime totals: read once per setup, and kept when the
    # entry is reloaded (removed only with the entry, see async_remove_entry).
    lifetime_store = LifetimeStore(hass, entry.entry_id)
    await lifetime_store.async_load()

    try:
        await client.login()
        async_clear_app_version_issue(hass, entry)
        # Get all the devices, we will filter the disabled ones later
        devices = await client.get_devices()
        async_clear_agreement_issue(hass, entry)

        # One coordinator per device; the platforms read them from the
        # entry's runtime_data.
        for device in devices:
            coordinator = AquareaDataUpdateCoordinator(
                hass=hass,
                entry=entry,
                client=client,
                device_info=device,
                lifetime_store=lifetime_store,
            )
            coordinators[device.device_id] = coordinator
            _LOGGER.debug("Performing first refresh for device %s", device.device_id)
            await coordinator.async_config_entry_first_refresh()

        entry.runtime_data = coordinators
        _LOGGER.debug("Forwarding entry setups for platforms")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except aioaquarea.AuthenticationError as err:
        if err.error_code in (
            aioaquarea.AuthenticationErrorCodes.INVALID_USERNAME_OR_PASSWORD,
            aioaquarea.AuthenticationErrorCodes.INVALID_CREDENTIALS,
        ):
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_credentials"
            ) from err
        if err.error_code == APP_VERSION_REJECTED:
            # Panasonic stopped accepting the app version: the credentials are
            # fine, so no reauth (it would ask for a password that is not the
            # problem). Show a repair issue, and retry: an update of the
            # library or Panasonic accepting the version again fixes it.
            async_create_app_version_issue(hass, entry)
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="app_version_rejected"
            ) from err
        if err.error_code == MFA_REQUIRED:
            # Retrying cannot pass the challenge, and every attempt sends the
            # user a Panasonic login email. Reauth asks for the code.
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="mfa_required"
            ) from err
        # Any other authentication failure (SESSION_CLOSED, API_ERROR,
        # TOKEN_EXPIRED, ...) is transient. Never fall through to `return True`
        # here: that would report a successful setup while no devices,
        # coordinators or platforms had been set up.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="authentication_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    except aioaquarea.ClientError as err:
        if getattr(err, "error_code", None) == AGREEMENT_NOT_ACCEPTED_CODE:
            # The user can fix it (accept the terms in the Panasonic app): say
            # so in a repair issue, which the next working setup clears.
            async_create_agreement_issue(hass, entry)
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="agreement_not_accepted"
            ) from err
        # ApiError, RequestFailedError, InvalidData - the cloud is reachable
        # but unhappy. Worth retrying.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cloud_error",
            translation_placeholders={"error": str(err)},
        ) from err
    except (aiohttp.ClientError, TimeoutError) as err:
        # DNS failures, connection resets, timeouts. Without this, a transient
        # network blip leaves the entry in `setup_error` forever, because Home
        # Assistant only retries setup when ConfigEntryNotReady is raised.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    return True


async def async_remove_entry(hass: HomeAssistant, entry: AquareaConfigEntry) -> None:
    """Drop the repair issues and the stored lifetime totals of a removed entry."""
    async_clear_app_version_issue(hass, entry)
    async_clear_agreement_issue(hass, entry)
    async_clear_migration_issues(hass, entry)
    await LifetimeStore(hass, entry.entry_id).async_remove()


async def async_unload_entry(hass: HomeAssistant, entry: AquareaConfigEntry) -> bool:
    """Unload a config entry."""
    # Stop reading the history first (Home Assistant cancels an entry's
    # background tasks only after this returns), then write the state, so the
    # next setup reads everything and resumes where this stopped.
    coordinators = entry.runtime_data.values()
    for coordinator in coordinators:
        await coordinator.async_stop_lifetime_sync()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for store in {c.lifetime_store for c in coordinators}:
            await store.async_flush()
    else:
        # The entry stays loaded: its coordinators have to carry on.
        for coordinator in coordinators:
            coordinator.resume_lifetime_sync()
    return unloaded
