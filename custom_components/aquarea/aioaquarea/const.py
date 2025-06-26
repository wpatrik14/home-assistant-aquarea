"""Constants definition"""

from enum import IntEnum


AQUAREA_SERVICE_BASE = "https://accsmart.panasonic.com/"
AQUAREA_SERVICE_DEMO_BASE = "https://accsmart.panasonic.com/"
AQUAREA_SERVICE_LOGIN = "remote/v1/api/auth/login"
AQUAREA_SERVICE_DEVICES = "remote/v1/api/devices"
AQUAREA_SERVICE_CONSUMPTION = "remote/v1/api/consumption"
AQUAREA_SERVICE_CONTRACT = "remote/contract"
AQUAREA_SERVICE_A2W_STATUS_DISPLAY = "remote/a2wStatusDisplay"
AQUAREA_SERVICE_AUTH_CLIENT_ID = "Xmy6xIYIitMxngjB2rHvlm6HSDNnaMJx"
AQUAREA_SERVICE_AUTH0_CLIENT = "eyJuYW1lIjoiQXV0aDAuQW5kcm9pZCIsImVudiI6eyJhbmRyb2lkIjoiMzAifSwidmVyc2lvbiI6IjIuOS4zIn0="
REDIRECT_URI = "panasonic-iot-cfc://authglb.digital.panasonic.com/android/com.panasonic.ACCsmart/callback"
AQUAREA_SERVICE_TOKEN = "https://authglb.digital.panasonic.com/oauth/token"
AQUAREA_SERVICE_ACC_LOGIN = "https://accsmart.panasonic.com/auth/v2/login"

APP_CLIENT_ID = "Xmy6xIYIitMxngjB2rHvlm6HSDNnaMJx"
AUTH_0_CLIENT = "eyJuYW1lIjoiQXV0aDAuQW5kcm9pZCIsImVudiI6eyJhbmRyb2lkIjoiMzAifSwidmVyc2lvbiI6IjIuOS4zIn0="
BASE_PATH_ACC = "https://accsmart.panasonic.com"
BASE_PATH_AUTH = "https://authglb.digital.panasonic.com"
AUTH_API_USER_AGENT = "okhttp/4.10.0"
AUTH_BROWSER_USER_AGENT = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Mobile Safari/537.36"

INVALID_TEMPERATURE = 126

DEFAULT_X_APP_VERSION = "1.21.0"

MAX_VERSION_AGE = 2

SETTING_ACCESS_TOKEN = "access_token"
SETTING_ACCESS_TOKEN_EXPIRES = "access_token_expires"
SETTING_REFRESH_TOKEN = "refresh_token"
SETTING_SCOPE = "scope"
SETTING_VERSION = "android_version"
SETTING_VERSION_DATE = "android_version_date"
SETTING_CLIENT_ID = "clientId"

CHECK_RESPONSE_ERROR_MESSAGE = """Error in %s
Expected status code '%s' but received '%s'
Response body: %s"""
CHECK_RESPONSE_ERROR_MESSAGE_WITH_PAYLOAD = """Error in %s
Expected status code '%s' but received '%s'
Payload: %s
Response body: %s"""

PANASONIC = "Panasonic"


class AquareaEnvironment(IntEnum):
    """Aquarea environment"""

    PRODUCTION = 0
    DEMO = 1
