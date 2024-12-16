"""Constants for the Airzone integration."""

from typing import Final

from aioairzone.common import TemperatureUnit

from homeassistant.const import UnitOfTemperature

# The domain identifier for the Airzone integration
DOMAIN: Final = "airzone"
# Manufacturer name for Airzone devices
MANUFACTURER: Final = "Airzone"
# Timeout duration for aioairzone device communication in seconds
AIOAIRZONE_DEVICE_TIMEOUT_SEC: Final = 10
# Step value for temperature adjustments in Airzone devices
API_TEMPERATURE_STEP: Final = 0.5
# Mapping Airzone's temperature units to Home Assistant's temperature units
TEMP_UNIT_LIB_TO_HASS: Final[dict[TemperatureUnit, str]] = {
    TemperatureUnit.CELSIUS: UnitOfTemperature.CELSIUS,
    TemperatureUnit.FAHRENHEIT: UnitOfTemperature.FAHRENHEIT,
}
