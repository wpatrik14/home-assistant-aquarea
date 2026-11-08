# Manage Panasonic Aquarea Smart Cloud devices from Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
![GitHub Release (latest SemVer including pre-releases)](https://img.shields.io/github/v/release/wpatrik14/home-assistant-aquarea?include_prereleases)
[![Validate with hassfest](https://github.com/wpatrik14/home-assistant-aquarea/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/wpatrik14/home-assistant-aquarea/actions/workflows/hassfest.yaml)
[![Validate with HACS](https://github.com/wpatrik14/home-assistant-aquarea/actions/workflows/hacs.yaml/badge.svg)](https://github.com/wpatrik14/home-assistant-aquarea/actions/workflows/hacs.yaml)
[![Type check with mypy](https://github.com/wpatrik14/home-assistant-aquarea/actions/workflows/mypy.yaml/badge.svg)](https://github.com/wpatrik14/home-assistant-aquarea/actions/workflows/mypy.yaml)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-FFDD00.svg?logo=buy-me-a-coffee&logoColor=black)](https://www.buymeacoffee.com/wpatrik14e)

Panasonic Aquarea Smart Cloud is a cloud service that allows you to control your Panasonic Aquarea heat pump from your smartphone. This integration allows you to control your heat pump from Home Assistant.

This is a fork of the original integration by [cjaliaga](https://github.com/cjaliaga/home-assistant-aquarea).

The integration uses [aioaquarea](https://github.com/cjaliaga/aioaquarea) to communicate with the Panasonic Aquarea Smart Cloud service.

This integration is actively maintained. Please report any issues you find and any feedback you may have. Thanks!

## Features
* Climate entity per device zone that allows you to control the operation mode, read the current temperature of the water in the device/zone and (if the zone supports it), change the target temperature. In auto mode the `auto_mode` attribute (`heat` or `cool`) shows which side the heat pump has switched to, even while it is idle.
* Sensor entity for the outdoor temperature.
* Sensor entity per zone for the zone temperature reported by the device. For zones that use a water temperature sensor this is the water (flow) temperature (inferred); for other sensor types it may be a room temperature.
* Water heater entity for the hot water tank (if the device has one), that allows you to control the operation mode (enabled/disabled) and read the current temperature of the water in the tank.
* Diagnostic sensor to indicate if the device has any problem (such not enough water flow).
* Diagnostic binary sensors for the bivalent (second heat source) and external heater flags the device reports, disabled by default. The meaning of their raw values is unverified, so the raw value is kept as an attribute.
* Diagnostic sensor for the current error/fault code (e.g. `H62`) and its description, when the device is in an error state.
* Diagnostic binary sensor for the electric anode flag of the hot water tank, disabled by default and only for devices with a tank. The meaning of its raw value is unverified, so the raw value is kept as an attribute.
* Diagnostic sensor for the pump status and current direction (idle/pump/water).
* Diagnostic binary sensor for the `uncontrollable` flag the adapter reports (enabled by default). It is informational only: no entity becomes unavailable when it is on, and a warning is logged when it turns on.
* Diagnostic sensors counting today's DHW heating, zone and defrost cycles.
* Read-only diagnostic binary sensor showing whether the weekly schedule is switched on, with the scheduled days and the number of scheduled changes as attributes. Disabled by default; the schedule is only read (at most hourly) while it is enabled.
* Energy consumption sensors: Accumulated (month to date, for the Energy dashboard) and Consumption (disabled by default; the energy published by the cloud since the previous fetch, 0 when nothing new, updated every consumption interval)
* Lifetime consumption sensors (heating, cooling, tank, total): all the energy since the cloud's first record, for the Energy dashboard, see [Lifetime consumption](#lifetime-consumption)
* Consumption today sensors (total enabled by default, heating, cooling and tank disabled): the energy of the current calendar day in your Home Assistant time zone, see [Daily and other period totals](#daily-and-other-period-totals)
* Quiet mode select entity
* Request defrost
* Powerful mode select entity
* Holiday timer
* Force DHW
* Force heater
* Set the device in eco mode/comfort mode (if the device supports it).
* Diagnostics download (account, IDs and names redacted)

## Lifetime consumption
The *Lifetime consumption* sensors (total, heating, tank and cooling; the cooling one is disabled by default, like the Accumulated one) report all the energy the heat pump has used since the cloud's first record. They are `total_increasing` energy sensors in kWh, so you can use them in the Energy dashboard.

How they work:
* **First start only: the history is read once.** After the first setup the integration reads the past consumption from the Aquarea Smart Cloud in the background (one request per two years, a few seconds apart, and never more than 15 years back), and stops at the month your unit was registered in the cloud. It does not delay the setup. Until it has finished, the lifetime sensors are *unknown*; this is deliberate, because the Energy dashboard would otherwise show the whole history as one huge spike when the figure jumps from a partial to the full value.
* **It is not read again** on a restart, a reload, a reconfiguration, an options change or an update of the integration, nor after a failed refresh. The finished months are kept in Home Assistant's storage (`.storage/aquarea.lifetime.<entry id>`), kept when the entry is reloaded, and deleted only when you delete the integration. Only three things read the history again: no stored data exists (first install, or the file was deleted), the stored data is found corrupt, or you press the **Rebuild lifetime consumption** button (a configuration entity, disabled by default).
* **An interrupted read resumes** where it stopped. If Panasonic refuses the requests (for example an HTTP 403 block of your IP address) or anything else fails, the read is aborted and tried again hours later (6 hours after a refusal, 1 hour after other failures, doubling up to 12 hours). It never retries in a loop.
* **It grows with every refresh.** The value is the finished months plus the current month as the cloud reports it at the latest consumption refresh, so new energy is added as soon as Panasonic publishes it, and a day that Panasonic revises later is replaced, not added twice. The value never decreases.
* **Use the Lifetime *or* the Accumulated sensors in the Energy dashboard, not both**, or the energy is counted twice.

If a rebuild finds more history than before (for example Panasonic added older data), the lifetime figure rises once by the difference, and the Energy dashboard shows it as consumption in that hour. You can correct that in Developer Tools > Statistics ("Fix issue" or adjust the sum).

## Daily and other period totals
The *Consumption today* sensors are built from the hourly records the integration already fetches, so they cost no extra requests. They count the hours of the **local calendar day** of your Home Assistant time zone (23 or 25 hours on the days the clocks change), reset at local midnight (`state_class: total` with `last_reset`), and replace the value of an hour when Panasonic revises it. An hour that Panasonic publishes after midnight for the previous day is counted in that day, not in today. The cloud labels its own days in UTC, which is why the Accumulated sensors reset a few hours after the cloud's midnight, while the today sensors follow your local day. Only the total is enabled by default.

For other periods (weekly, monthly, yearly, or a daily total that follows tariff hours) the integration does not create helpers. Build a **Utility Meter** helper on top of a lifetime sensor:
1. Settings > Devices & services > Helpers > Create helper > Utility Meter.
2. Name it (for example "Heat pump energy weekly"), choose **Weekly** (or Daily, Monthly, ...) as the meter reset cycle, and pick the *Lifetime consumption* sensor of your heat pump as the input sensor. Leave *Periodically resetting* off: the lifetime sensor never resets. Add tariffs if you want peak and off-peak totals.
3. Submit. The new sensor can be used in the Energy dashboard or on a dashboard card.

## Features in the works
* Improve translations
* Rework of the water tank entity
* Additional sensors/switches for the device.

## Remarks
Panasonic only allows one connection per account at the same time. This means that if you open the session from the Panasonic Comfort Cloud app or the Panasonic Comfort Cloud website, the session will be closed and you will be disconnected from Home Assistant. The integration will try to reconnect automatically, closing the session from the app or the website. If you want to use the app or the website, you will have to temporarily disable the integration.

A possible solution to this behaviour is to use a second Panasonic ID specifically for Home Assistant, which your main account has granted access to the device. Then you can use the app with your main account and the integration with the second one at the same time.

Panasonic has retired the Aquarea Smart Cloud website (`aquarea-smart.panasonic.com`) and the account site the old instructions used (`csapl.pcpf.panasonic.com`). Both the registration and the access request now have to go through the official Panasonic Comfort Cloud app, which uses the same Panasonic ID as this integration. If you have set this up with the current app, a PR or issue with the exact steps is welcome.

If the integration suddenly fails to log in (e.g. `Error in get_token ... Missing required parameter: code`), log in to the official Comfort Cloud app with the same account first. Panasonic periodically publishes new terms/policies, and the login fails until they are accepted there. If the app doesn't ask, update it.

Panasonic now enforces multi-factor authentication (2-step verification) on Panasonic IDs. Accounts that use SMS codes or an authenticator app are supported: after the password, setup and reauthentication ask for the 6-digit code (for SMS there is a "Send a new code" option). The integration then keeps the refresh token Panasonic returns, so a Home Assistant restart or an expired session does not ask for a code again; you are only asked when Panasonic stops accepting that token. Other methods (for example push notifications) are not supported and show "requires multi-factor authentication". Requires aioaquarea-ng 1.3.0 or newer. Discussion: [#112](https://github.com/wpatrik14/home-assistant-aquarea/issues/112).

If Home Assistant shows a repair "Panasonic rejected the app version", Panasonic changed which app version its login accepts. Your username and password are fine and you do not need to log in again. Update the integration (and, through it, aioaquarea-ng) and restart Home Assistant; the integration keeps retrying and the repair disappears after the next successful login. If you are up to date, [open an issue](https://github.com/wpatrik14/home-assistant-aquarea/issues). Needs aioaquarea-ng 1.5.0 or newer to show this message; older releases just report a failed login.

### Minimum Home Assistant version required
The minimum supported version of Home Assistant is **2026.3**. Older versions can keep using the last release that supports them: `v1.9.1` for 2025.8 to 2026.2, and `v1.0.62` for 2024.12 (see [#98](https://github.com/wpatrik14/home-assistant-aquarea/issues/98))

## Installation

### Using [HACS](https://hacs.xyz/) (recommended)

1. Download the integration via (one of them):
   - [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=wpatrik14&repository=home-assistant-aquarea&category=integration)
   - Go to HACS > Integrations > Look for "Aquarea" 

2. Restart Home Assistant
3. Add integration via (one of them):
   - [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=aquarea)
   - Go to "Settings" >> "Devices & Services", click "+ ADD INTEGRATION" and select "Aquarea Smart Cloud"
4. Follow the configuration steps. You'll need to provide your Panasonic ID and your password. The integration will discover the devices associated to your Panasonic ID. 

### Manual installation
1. Copy the folder named `aquarea` from the [latest release](https://github.com/wpatrik14/home-assistant-aquarea/releases/latest) to the `custom_components` folder in your config folder.
2. Restart Home Assistant
3. Add integration via (one of them):
   - [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=aquarea)
   - Go to "Settings" >> "Devices & Services", click "+ ADD INTEGRATION" and select "Aquarea Smart Cloud"
4. Follow the configuration steps. You'll need to provide your Panasonic ID and your password. The integration will discover the devices associated to your Panasonic ID.

## Removing the integration

This integration follows standard integration removal; no extra steps are required in Home Assistant.

1. Go to **Settings** > **Devices & services** and select **Aquarea Smart Cloud**.
2. Next to the entry, select the three dots **⋮** menu, then **Delete**. This also deletes the stored consumption history of the lifetime sensors (`.storage/aquarea.lifetime.<entry id>`); removing only the files does not.

To also remove the files:
- **HACS:** open HACS, find **Aquarea Smart Cloud**, select the three dots **⋮** menu, then **Remove**, and restart Home Assistant.
- **Manual installation:** delete the `custom_components/aquarea` folder from your config folder and restart Home Assistant.

If you created a second Panasonic account just for Home Assistant (see [Remarks](#remarks)), you can remove it from the `Users` -> `Userlist` of your main account once you no longer need it.

## Running the tests

There are two test suites, and CI runs both:

- `tests/test_*.py`: stdlib-only scripts, each run directly (`python3 tests/test_user_step.py`). They need Python 3.14 and nothing else.
- `tests/ha/`: a pytest suite on Home Assistant's own test harness ([pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)). The Panasonic cloud is mocked, so no account is needed.

  ```bash
  pip install -r requirements_test.txt
  pytest
  ```

  It needs Python 3.14 and **does not run on native Windows**: Home Assistant's test runner imports `fcntl`, so test collection fails there. On Windows, use WSL2 or a container:

  ```bash
  docker run --rm -v "$PWD:/w" -w /w python:3.14-slim sh -c "pip install -r requirements_test.txt && pytest"
  ```

## Disclaimer

THIS PROJECT IS NOT IN ANY WAY ASSOCIATED WITH OR RELATED TO PANASONIC. The information here and online is for educational and resource purposes only and therefore the developers do not endorse or condone any inappropriate use of it, and take no legal responsibility for the functionality or security of your devices.

## Support this project

If this integration saved you an evening of fighting with Panasonic's cloud API, consider [buying me a coffee](https://www.buymeacoffee.com/wpatrik14e) — it helps keep it maintained.

## Acknowledgements and alternatives

- Big thanks to [cjaliaga](https://github.com/cjaliaga) for the original work on this integration.
- Big thanks to [ronhks](https://github.com/ronhks) for his awesome work on the [Panasonic Aquaera Smart Cloud integration with MQTT](https://github.com/ronhks/panasonic-aquarea-smart-cloud-mqtt). You can use his integration if you want to use MQTT instead.
- Panasonic introduced authentication breaking changes on March 18th 2024. A heartfelt thank you to [bimusiek](https://github.com/bimusiek) for generously sharing their implementation in the [Homebridge plugin](https://github.com/Hernas/homebridge-panasonic-heat-pump). Their contribution played a crucial role in having the integration working back, and I am truly grateful for their remarkable help.
