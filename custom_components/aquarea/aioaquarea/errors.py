"""Errors for aioaquarea."""
from __future__ import annotations

try:
    from enum import StrEnum
except ImportError:
    from strenum import StrEnum


class ClientError(Exception):
    """Base exception for all client errors"""


class RequestFailedError(ClientError):
    """Exception raised when request to the server fails"""

    def __init__(self, response):
        self.response = response
        super().__init__()

    def __str__(self):
        return f"Invalid response: {self.response.status} - {self.response.reason}"


class ApiError(ClientError):
    """API error"""

    def __init__(self, error_code, error_message):
        super().__init__()
        self.error_code = error_code
        self.error_message = error_message

    def __str__(self) -> str:
        return f"API error: {self.error_code} - {self.error_message}"


class AuthenticationError(ApiError):
    """Authentication error"""

    def __str__(self) -> str:
        return f"Authentication error: {self.error_code} - {self.error_message}"


class InvalidData(ClientError):
    """Invalid data"""

    def __init__(self, data):
        self.data = data
        super().__init__()

    def __str__(self):
        return f"Invalid data from server: {self.data!r}"


class AuthenticationErrorCodes(StrEnum):
    """Authentication error codes"""

    SESSION_CLOSED = "1001-0001"
    INVALID_USERNAME_OR_PASSWORD = "1001-1401"
    INVALID_CREDENTIALS = "1000-1401"
    API_ERROR = "API_ERROR"
    TOKEN_EXPIRED = "TOKEN_EXPIRED" # Added for token expiration

class DataNotAvailableError(Exception):
    """Exception raised when data is not available"""
