"""Constants for the Aquarea Smart Cloud integration."""

DOMAIN = "aquarea"

CONF_CONSUMPTION_INTERVAL = "consumption_interval"
# The OAuth refresh token Panasonic returned at login (also after multi-factor
# authentication), kept in the entry's data so a restart does not need the
# password or a new MFA code.
CONF_REFRESH_TOKEN = "refresh_token"
# Form field of the multi-factor steps.
CONF_CODE = "code"
CONF_RESEND_CODE = "resend_code"

DEFAULT_SCAN_INTERVAL = 60
DEFAULT_CONSUMPTION_INTERVAL = 60

# Hours into the day during which yesterday's hourly consumption is refetched
YESTERDAY_REFETCH_HOURS = 3

ATTRIBUTION = "Data provided by Aquarea Smart Cloud"

# aioaquarea's AuthenticationErrorCodes.MFA_REQUIRED, compared as a string so
# releases without the member still import.
MFA_REQUIRED = "MFA_REQUIRED"
# aioaquarea-ng 1.3.0: a wrong code (retry) and an expired MFA transaction (log in again).
MFA_INVALID_CODE = "MFA_INVALID_CODE"
MFA_EXPIRED = "MFA_EXPIRED"

IDLE = "idle"
HEATING = "heating"
