# aioaquarea Library Documentation

This document provides a detailed technical overview of the `aioaquarea` library, an asynchronous Python library designed to interface with Panasonic Aquarea Smart Cloud. It is intended for AI agents and developers to understand the library's architecture, data models, and communication patterns.

## Table of Contents
1. [Architecture Overview](#architecture-overview)
2. [Core Components](#core-components)
3. [Authentication Flow](#authentication-flow)
4. [Data Models & Entities](#data-models--entities)
5. [Device Control & Management](#device-control--management)
6. [Consumption & Statistics](#consumption--statistics)
7. [Error Handling](#error-handling)
8. [API Endpoints](#api-endpoints)

---

## Architecture Overview

The library follows a modular, layered architecture:
- **High-Level Interface**: `AquareaClient` (aliased as `Client`) provides the primary entry point for users.
- **Domain Logic**: `DeviceManager`, `DeviceControl`, and `ConsumptionManager` handle specific functional areas.
- **Data Layer**: `data.py` and `entities.py` define the state and behavior of devices, zones, and tanks.
- **Communication Layer**: `AquareaAPIClient` handles raw HTTP requests, header management, and basic error parsing.
- **Authentication Layer**: `Authenticator` and `PanasonicSettings` manage the complex OAuth2/OpenID Connect flow required by Panasonic.

---

## Core Components

### `AquareaClient` ([`core.py`](aioaquarea/aioaquarea/core.py))
The main coordinator. It initializes all sub-managers and provides high-level methods like `get_devices()`, `get_device_status()`, and various `post_device_*` methods for control.

### `AquareaAPIClient` ([`api_client.py`](aioaquarea/aioaquarea/api_client.py))
A wrapper around `aiohttp.ClientSession`.
- **Request Handling**: Automatically injects required Panasonic headers.
- **Error Detection**: Inspects JSON responses for error messages, as the API often returns HTTP 200 even for failures.
- **Token Management**: Tracks access token expiration and updates tokens from response data when available.

### `Authenticator` ([`auth.py`](aioaquarea/aioaquarea/auth.py))
Handles the multi-step authentication process:
1. **Authorize**: Initiates OAuth2 flow with PKCE (code challenge/verifier).
2. **Login**: Submits credentials to the Panasonic ID service.
3. **Callback**: Handles the redirect and extracts the authorization code.
4. **Token Request**: Exchanges the code for access and refresh tokens.
5. **Client ACC Login**: Retrieves a `clientId` specific to the Aquarea service.

---

## Authentication Flow

Panasonic uses a complex authentication system involving:
- **User-Agents**: Specific strings for API and Browser simulation.
- **App Versioning**: `CCAppVersion` dynamically fetches the latest version from the Apple App Store to stay compatible.
- **API Key Generation**: `PanasonicRequestHeader._get_api_key` generates a SHA256 hash based on a timestamp, hardcoded salt, and the current access token.
- **Headers**: Every request requires `x-app-version`, `x-app-timestamp`, `x-cfc-api-key`, and `x-user-authorization-v2`.

---

## Data Models & Entities

### Device Hierarchy
- **`Device`**: Abstract base class representing a Heat Pump unit.
- **`DeviceZone`**: Represents a heating/cooling zone (up to 2 zones supported).
- **`Tank`**: Represents the Domestic Hot Water (DHW) tank.

### Key Enums ([`data.py`](aioaquarea/aioaquarea/data.py))
- `OperationStatus`: `ON` (1), `OFF` (0).
- `ExtendedOperationMode`: `HEAT`, `COOL`, `AUTO_HEAT`, `AUTO_COOL`.
- `UpdateOperationMode`: Used for commands (`HEAT`=2, `COOL`=3, `AUTO`=8).
- `DeviceAction`: Current activity (`HEATING`, `COOLING`, `HEATING_WATER`, `IDLE`).
- `QuietMode`: `OFF`, `LEVEL1`, `LEVEL2`, `LEVEL3`.
- `SpecialStatus`: `ECO` (1), `COMFORT` (2).

### State Objects
- `DeviceInfo`: Static configuration (ID, name, model, zones, tank presence).
- `DeviceStatus`: Dynamic state (temperatures, current mode, errors, pump duty).

---

## Device Control & Management

### `DeviceManager` ([`device_manager.py`](aioaquarea/aioaquarea/device_manager.py))
- **Discovery**: `get_devices()` fetches the group and device list.
- **Status**: `get_device_status()` retrieves the full state. It attempts a "live" request first (`deviceDirect=1`) and falls back to "cached" (`deviceDirect=0`) if it fails.

### `AquareaDeviceControl` ([`device_control.py`](aioaquarea/aioaquarea/device_control.py))
Implements the low-level POST requests to change device state.
- **Transfer API**: Many commands use a generic `/remote/v1/app/common/transfer` endpoint where the actual API call is wrapped in the JSON payload.
- **Direct API**: Some commands (like Quiet Mode or Powerful Mode) use direct REST endpoints like `remote/v1/api/devices/{id}`.

---

## Consumption & Statistics

### `AquareaConsumptionManager` ([`consumption_manager.py`](aioaquarea/aioaquarea/consumption_manager.py))
Retrieves energy usage data.
- **Aggregation**: Supports `DAY`, `MONTH`, `YEAR`.
- **Timezones**: Requires proper timezone offset formatting (`+HH:MM`).
- **Data Points**: Returns `Consumption` objects containing heat, cool, and tank energy (kWh) and costs.

---

## Error Handling

### Exception Hierarchy ([`errors.py`](aioaquarea/aioaquarea/errors.py))
- `ClientError`: Base class.
- `ApiError`: Returned by the Panasonic API (includes `error_code` and `error_message`).
- `AuthenticationError`: Specific to login/token issues.
- `RequestFailedError`: Network or HTTP-level failures.

### Decorators ([`decorators.py`](aioaquarea/aioaquarea/decorators.py))
- **`@auth_required`**: Automatically checks if the user is logged in before calling a method. If the token is expired or a "Missing Authentication Token" error is caught, it attempts to re-login and retry the operation once.

---

## API Endpoints

| Purpose | Endpoint | Method |
|---------|----------|--------|
| Base URL | `https://accsmart.panasonic.com/` | - |
| Device List | `device/group` | GET |
| Device Status | `remote/v1/api/devices` | GET |
| Control (Generic) | `remote/v1/app/common/transfer` | POST |
| Control (Direct) | `remote/v1/api/devices/{id}` | POST |
| Consumption | `remote/v1/api/consumption` | POST |
| Auth Token | `https://authglb.digital.panasonic.com/oauth/token` | POST |
| Auth Login | `https://accsmart.panasonic.com/auth/v2/login` | POST |

---

## Appendix: API JSON Examples

These examples illustrate the structure of the data exchanged with the Panasonic Smart Cloud.

### 1. Device Status (Live/Cached)
**Endpoint**: `remote/v1/app/common/transfer` (POST)
**Payload**: `{"apiName": "/remote/v1/api/devices?gwid=[ID]&deviceDirect=1", "requestMethod": "GET"}`

```json
{
  "a2wName": "[DEVICE NAME]",
  "status": {
    "operationMode": 1,
    "outdoorNow": 21,
    "pumpDuty": 0,
    "tankStatus": {
      "heatMax": 65,
      "heatMin": 40,
      "heatSet": 50,
      "operationStatus": 1,
      "temperatureNow": 75
    },
    "zoneStatus": [
      {
        "zoneId": 1,
        "operationStatus": 0,
        "temperatureNow": 28,
        "heatSet": 26,
        "coolSet": 14
      }
    ],
    "quietMode": 0,
    "forceDHW": 0,
    "forceHeater": 0,
    "deiceStatus": 0
  }
}
```

### 2. Device Control (Operation Update)
**Endpoint**: `remote/v1/app/common/transfer` (POST)
**Payload Example**: Turning on Heat mode for Zone 1 and Tank.

```json
{
  "apiName": "/remote/v1/api/devices",
  "requestMethod": "POST",
  "bodyParam": {
    "gwid": "[DEVICE GUID]",
    "operationMode": 2,
    "operationStatus": 1,
    "zoneStatus": [
      {
        "zoneId": 1,
        "operationStatus": 1
      },
      {
        "zoneId": 2,
        "operationStatus": 0
      }
    ],
    "tankStatus": {
      "operationStatus": 1
    }
  }
}
```

### 3. Consumption Data
**Endpoint**: `remote/v1/app/common/transfer` (POST)
**Payload**: `{"apiName": "/remote/v1/api/consumption", "requestMethod": "POST", "bodyParam": {...}}`

```json
{
  "historyDataList": [
    {
      "dataTime": "20250513",
      "heatConsumption": 4.522,
      "heatCost": 11.19756,
      "outdoorTemp": 9.90625,
      "tankConsumption": 1.521,
      "tankCost": 2.00772
    }
  ]
}
```

