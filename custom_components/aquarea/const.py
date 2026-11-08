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

# aioaquarea-ng 1.5.0 (AppVersionRejectedError): Panasonic's login gate rejected
# the app version the library announces (code 4106), not the credentials.
# Compared by the error code, as a string, so older releases (which never raise
# it) still import; the library's exception subclasses AuthenticationError.
APP_VERSION_REJECTED = "APP_VERSION_REJECTED"

IDLE = "idle"
HEATING = "heating"

# Lifetime energy totals (lifetime.py).
# The backfill walks back through the cloud's history with one YEAR query per
# 24 months, never further than this many years.
LIFETIME_MAX_YEARS_BACK = 15
# It stops after this many windows in a row without any consumption.
LIFETIME_EMPTY_WINDOWS_STOP = 2
# Pause before the first request of a sync (the setup has just made its own
# requests) and between two requests, in seconds.
LIFETIME_START_DELAY = 30
LIFETIME_REQUEST_DELAY = 5
# The backfill takes "no data" answers as the end of the history only when the
# registration month of the unit never became known, and then only after this
# many such answers spread over at least this long (the library cannot tell
# "no data" from a failed request). Otherwise they are failures to retry.
LIFETIME_NO_DATA_MIN_RESPONSES = 5
LIFETIME_NO_DATA_MIN_SPAN = timedelta(hours=72)
# A month with more energy than this (per kind) is not plausible: stored data
# holding one is treated as corrupt.
LIFETIME_MAX_MONTH_KWH = 50_000
# A finished month is looked up once more this long after it ended, for hours
# the cloud published late; the larger figure wins.
LIFETIME_RECONCILE_DELAY = timedelta(hours=24)
# Wait before the next attempt after a failed sync: doubles from the first to
# the maximum; an authentication or access failure (for example an HTTP 403
# block) starts at the longer one.
LIFETIME_RETRY_INITIAL = timedelta(hours=1)
LIFETIME_RETRY_AUTH_INITIAL = timedelta(hours=6)
LIFETIME_RETRY_MAX = timedelta(hours=12)
# How much hourly history the today sensors keep across restarts.
LIFETIME_HOURS_KEPT = timedelta(days=4)
# Delay before a changed state is written to storage.
LIFETIME_SAVE_DELAY = 30
