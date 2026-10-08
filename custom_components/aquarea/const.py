"""Constants for the Aquarea Smart Cloud integration."""

DOMAIN = "aquarea"

CONF_CONSUMPTION_INTERVAL = "consumption_interval"

DEFAULT_SCAN_INTERVAL = 60
DEFAULT_CONSUMPTION_INTERVAL = 60

ATTRIBUTION = "Data provided by Aquarea Smart Cloud"

# aioaquarea's AuthenticationErrorCodes.MFA_REQUIRED, compared as a string so
# releases without the member still import.
MFA_REQUIRED = "MFA_REQUIRED"

IDLE = "idle"
HEATING = "heating"
