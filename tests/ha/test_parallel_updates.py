"""Every platform declares PARALLEL_UPDATES.

Without it Home Assistant picks a default per platform. Platforms that send commands to
the Panasonic cloud take one call at a time; the read-only ones never wait.
"""

from importlib import import_module

import pytest

from custom_components.aquarea import PLATFORMS

EXPECTED = {
    "sensor": 0,
    "binary_sensor": 0,
    "climate": 1,
    "switch": 1,
    "select": 1,
    "button": 1,
    "water_heater": 1,
}


@pytest.mark.parametrize(("platform", "limit"), EXPECTED.items())
def test_platform_declares_parallel_updates(platform: str, limit: int) -> None:
    """Each platform module has its PARALLEL_UPDATES constant."""
    module = import_module(f"custom_components.aquarea.{platform}")
    assert limit == module.PARALLEL_UPDATES


def test_every_platform_is_covered() -> None:
    """A platform added to PLATFORMS needs a choice here too."""
    assert {p.value for p in PLATFORMS} == set(EXPECTED)
