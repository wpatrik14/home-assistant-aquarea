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
