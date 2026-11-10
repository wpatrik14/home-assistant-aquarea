"""Repair issues of the Aquarea Smart Cloud integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

ISSUES_URL = "https://github.com/wpatrik14/home-assistant-aquarea/issues"
APP_VERSION_REJECTED_MESSAGE = (
    "Panasonic rejected the app version this integration announces (not your "
    "login): update the integration, see the repair issue"
)


def _app_version_issue_id(entry: ConfigEntry) -> str:
    return f"app_version_rejected_{entry.entry_id}"


@callback
def async_create_app_version_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Tell the user that Panasonic stopped accepting the app version.

    It is a change on Panasonic's side that an update of the integration
    usually fixes. The credentials are fine, so this is a repair issue and not
    a reauth flow. Not fixable from here: there is nothing to confirm.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _app_version_issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        learn_more_url=ISSUES_URL,
        translation_key="app_version_rejected",
        translation_placeholders={"issues_url": ISSUES_URL},
    )


@callback
def async_clear_app_version_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the app version issue, if there is one (after a working login)."""
    ir.async_delete_issue(hass, DOMAIN, _app_version_issue_id(entry))


def _pump_entities_issue_id(entry: ConfigEntry) -> str:
    return f"pump_entities_changed_{entry.entry_id}"


@callback
def async_create_pump_entities_issue(
    hass: HomeAssistant, entry: ConfigEntry, moved: dict[str, str]
) -> None:
    """Tell the user that the pump status entity moved and the direction states changed.

    The pump status sensor became a binary sensor, which changes its entity id
    (the domain is part of it), so automations, templates and dashboards that
    use the old id need updating. The states of the direction sensor changed
    to lowercase at the same time. Nothing can be confirmed or fixed from
    here, so the issue is not fixable; the user ignores it once done.
    """
    # Its text covers the direction states too.
    ir.async_delete_issue(hass, DOMAIN, _direction_case_issue_id(entry))
    ir.async_create_issue(
        hass,
        DOMAIN,
        _pump_entities_issue_id(entry),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        learn_more_url=ISSUES_URL,
        translation_key="pump_entities_changed",
        translation_placeholders={
            "entry_title": entry.title,
            "entities": "\n".join(
                f"- `{old}` -> `{new}`" for old, new in moved.items()
            ),
        },
    )


def _pump_not_moved_issue_id(entry: ConfigEntry) -> str:
    return f"pump_entities_not_moved_{entry.entry_id}"


def _direction_case_issue_id(entry: ConfigEntry) -> str:
    return f"direction_case_changed_{entry.entry_id}"


@callback
def async_create_pump_not_moved_issue(
    hass: HomeAssistant, entry: ConfigEntry, blocked: dict[str, str]
) -> None:
    """Tell the user that a pump status sensor could not move to the new domain.

    ``blocked`` maps the sensor's entity id to the binary sensor id that
    something else already uses. The user frees that id (renames or removes the
    other entity) and reloads the integration: the move is tried again on
    every setup until it works, so the issue stays until then. Not fixable from
    here for the same reason as the issue above.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _pump_not_moved_issue_id(entry),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        learn_more_url=ISSUES_URL,
        translation_key="pump_entities_not_moved",
        translation_placeholders={
            "entry_title": entry.title,
            "entities": "\n".join(
                f"- `{old}` -> `{new}`" for old, new in blocked.items()
            ),
        },
    )


@callback
def async_create_direction_case_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Tell the user that the pump direction states are lowercase now.

    Only for an entry that has a direction sensor but no moved pump status
    sensor (the user removed it, or it could not move): the other issue
    already says it. Automations that compare the old uppercase states break
    silently, so this is worth a notice of its own.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _direction_case_issue_id(entry),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        learn_more_url=ISSUES_URL,
        translation_key="direction_case_changed",
        translation_placeholders={"entry_title": entry.title},
    )


@callback
def async_pump_entities_issue_exists(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return whether the moved-entities issue (which covers the direction too) exists."""
    registry = ir.async_get(hass)
    return registry.async_get_issue(DOMAIN, _pump_entities_issue_id(entry)) is not None


@callback
def async_clear_pump_not_moved_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the not-moved issue, once the move has worked."""
    ir.async_delete_issue(hass, DOMAIN, _pump_not_moved_issue_id(entry))


@callback
def async_clear_migration_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the persistent migration issues of an entry that is being removed."""
    ir.async_delete_issue(hass, DOMAIN, _pump_entities_issue_id(entry))
    ir.async_delete_issue(hass, DOMAIN, _pump_not_moved_issue_id(entry))
    ir.async_delete_issue(hass, DOMAIN, _direction_case_issue_id(entry))
