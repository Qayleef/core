import cProfile
import pstats
import asyncio
from homeassistant.components.weather import WeatherEntity
from homeassistant.core import HomeAssistant
from typing import Any

class TestWeatherEntity(WeatherEntity):
    def __post_init__(self, *args: Any, **kwargs: Any) -> None:
        super().__post_init__(*args, **kwargs)
        self._attr_native_temperature = 22.0
        self._attr_native_pressure = 1013.25
        self.entity_id = "weather.test"

async def main():
    hass = HomeAssistant(config_dir="/tmp")
    await hass.async_start()
    weather_entity = TestWeatherEntity()
    weather_entity.hass = hass

    def profile_weather_entity():
        for _ in range(1000000):  # Increase the loop count to get more precise timing
            weather_entity.native_temperature
            weather_entity._default_temperature_unit
            weather_entity.state_attributes
            weather_entity.native_pressure
            weather_entity._default_pressure_unit
            weather_entity.condition
            weather_entity.state

    with cProfile.Profile() as pr:
        profile_weather_entity()

    # Save profiling stats
    stats = pstats.Stats(pr)
    stats.dump_stats("weather_entity_profile.prof")

    stats = pstats.Stats(pr)
    stats.sort_stats(pstats.SortKey.TIME)
    stats.print_stats()

asyncio.run(main())

