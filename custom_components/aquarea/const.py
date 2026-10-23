"""Constants for the Aquarea Smart Cloud integration."""

from datetime import timedelta

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
DEFAULT_CONSUMPTION_INTERVAL = 15
# The cloud publishes new consumption data at most every ~15 minutes.
MIN_CONSUMPTION_INTERVAL = 15

# The weekly schedule changes rarely: read it at most this often, and only
# while a weekly schedule entity is enabled.
WEEKLY_TIMER_INTERVAL = timedelta(hours=1)
# The weekly schedule entity is unavailable once the last read is older than this.
WEEKLY_TIMER_MAX_AGE = timedelta(hours=3)

# Hours into the day during which yesterday's hourly consumption is refetched
YESTERDAY_REFETCH_HOURS = 3

ATTRIBUTION = "Data provided by Aquarea Smart Cloud"

# aioaquarea's AuthenticationErrorCodes.MFA_REQUIRED, compared as a string so
# releases without the member still import.
MFA_REQUIRED = "MFA_REQUIRED"
# aioaquarea-ng 1.3.0: a wrong code (retry) and an expired MFA transaction (log in again).
MFA_INVALID_CODE = "MFA_INVALID_CODE"
MFA_EXPIRED = "MFA_EXPIRED"

# aioaquarea-ng, release after 1.4.1 (AppVersionRejectedError): Panasonic's login gate rejected
# the app version the library announces (code 4106), not the credentials.
# Compared by the error code, as a string, so older releases (which never raise
# it) still import; the library's exception subclasses AuthenticationError.
APP_VERSION_REJECTED = "APP_VERSION_REJECTED"

IDLE = "idle"
HEATING = "heating"
