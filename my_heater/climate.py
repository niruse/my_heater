import asyncio
import time
from datetime import datetime, timedelta
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    HVACMode,
    ClimateEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE  # Removed TEMP_CELSIUS


from .const import DOMAIN

import logging
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up climate entities for My Heater."""
    _LOGGER.debug("Setting up My Heater climate entity with data: %s", config_entry.data)
    data = config_entry.data
    async_add_entities(
        [
            MyHeaterClimate(
                data["heater_name"],
                data["scene_turn_on_off"],
                data["temperature_sensor"],
                data["min_temp"],
                data["max_temp"],
                data["default_temp"],
                data["temperature_up_scene"],
                data["temperature_down_scene"],
                data["voltage_usage"],  # Updated: voltage_usage
                hass,
            )
        ]
    )


class MyHeaterClimate(ClimateEntity):
    """Representation of My Heater as a climate entity."""

    def __init__(
        self,
        name,
        scene_turn_on_off,
        temperature_sensor,
        min_temp,
        max_temp,
        default_temp,
        temperature_up_scene,
        temperature_down_scene,
        voltage_usage,  # Updated: voltage_usage
        hass,
    ):
        self._name = name
        self._scene_turn_on_off = scene_turn_on_off
        self._temperature_sensor = temperature_sensor
        self._min_temp = min_temp
        self._max_temp = max_temp
        self._temperature_up_scene = temperature_up_scene
        self._temperature_down_scene = temperature_down_scene
        self._voltage_usage = voltage_usage  # Updated: voltage_usage
        self._hass = hass
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = (min_temp + max_temp) / 2
        self._target_temperature = default_temp
        self._attr_temperature_unit = "°C"
        self._power_state_check_task = None
        self.last_mode_change = None

        # Start monitoring logic
        self._start_power_monitoring()
    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID for this entity."""
        return f"{self._name}_unique_id"

    @property
    def supported_features(self):
        return ClimateEntityFeature.TARGET_TEMPERATURE

    @property
    def hvac_modes(self):
        return [HVACMode.OFF, HVACMode.HEAT]

    @property
    def hvac_mode(self):
        return self._hvac_mode

    @property
    def current_temperature(self):
        """Get the current temperature from the sensor."""
        sensor_state = self._hass.states.get(self._temperature_sensor)
        if sensor_state and sensor_state.state not in (None, "unknown", "unavailable"):
            try:
                return float(sensor_state.state)
            except ValueError:
                _LOGGER.warning(
                    "Sensor %s returned invalid temperature: %s",
                    self._temperature_sensor,
                    sensor_state.state,
                )
                return None
        return None


    @property
    def current_voltage_usage(self):
        """Get the current voltage usage from the sensor."""
        sensor_state = self._hass.states.get(self._voltage_usage)
        if sensor_state and sensor_state.state not in (None, "unknown", "unavailable"):
            try:
                return float(sensor_state.state)
            except ValueError:
                _LOGGER.warning(
                    "Voltage usage sensor %s returned invalid value: %s",
                    self._voltage_usage,
                    sensor_state.state,
                )
                return None
        return None

    @property
    def target_temperature(self):
        return self._target_temperature

    @property
    def min_temp(self):
        """Return the minimum temperature that can be set."""
        return self._min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature that can be set."""
        return self._max_temp

    @property
    def target_temperature_step(self):
        """Return the supported step for temperature."""
        return 1


    async def async_set_hvac_mode(self, hvac_mode):
        """Set the HVAC mode."""
        now = datetime.now()
        if hvac_mode == HVACMode.HEAT:

            if (
                hasattr(self, "last_mode_change") 
                and self.last_mode_change is not None 
                and self.last_mode_change > now - timedelta(seconds=10)
            ):
                _LOGGER.debug("Recently changed to OFF. Delaying before switching to HEAT.")
                await asyncio.sleep(10)
            voltage = self.current_voltage_usage
            if voltage is not None and voltage >= 10:  # Check voltage usage
                _LOGGER.debug("Voltage usage is sufficient; no need to toggle power button.")
            else:
                _LOGGER.warning("Voltage usage is low; toggling power button.")
                await self._hass.services.async_call(
                    "scene", "turn_on", {"entity_id": self._scene_turn_on_off}
                )
            _LOGGER.debug("Changing HVAC mode to HEAT.")
        elif hvac_mode == HVACMode.OFF:
            await self._hass.services.async_call(
                "scene", "turn_on", {"entity_id": self._scene_turn_on_off}
            )
            _LOGGER.debug("Changing HVAC mode to OFF.")
            self.last_mode_change = now
        self._hvac_mode = hvac_mode
        self.async_write_ha_state()

        

    async def async_set_temperature(self, **kwargs):
        """Set the target temperature."""
        if ATTR_TEMPERATURE in kwargs:
            target_temp = kwargs[ATTR_TEMPERATURE]

            # Ensure target temperature is within range
            if target_temp < self._min_temp or target_temp > self._max_temp:
                _LOGGER.warning(
                    "Target temperature %.1f is out of range (%.1f-%.1f)",
                    target_temp,
                    self._min_temp,
                    self._max_temp,
                )
                return

            # Adjust temperature and trigger the appropriate scene
            difference = target_temp - self._target_temperature + 1
            if difference > 0:
                scene = self._temperature_up_scene
                steps = int(difference)  # Convert to integer steps for up
            elif difference < 0:
                scene = self._temperature_down_scene
                steps = int(abs(difference))  # Convert to integer steps for down
            else:
                steps = 0  # No adjustment needed

            for _ in range(steps):
                await self._hass.services.async_call(
                    "scene", "turn_on", {"entity_id": scene}
                )
                await asyncio.sleep(1)  # Sleep 1 second between clicks

            # Update the target temperature
            self._target_temperature = target_temp
            _LOGGER.debug("Set target temperature to: %.1f°C", self._target_temperature)
            self.async_write_ha_state()


            
    def _start_power_monitoring(self):
        """Start a background task to monitor voltage and temperature."""
        if self._power_state_check_task is None:
            self._power_state_check_task = self._hass.loop.create_task(self._monitor_power_and_temperature())


    async def _monitor_power_and_temperature(self):
        """Background task to monitor voltage usage and temperature."""
        while True:
            if self._hvac_mode == HVACMode.HEAT:
                if self.current_temperature is not None:
                    difference = self.current_temperature - self._target_temperature

                    # Skip scene activation if difference is within (-1, 1)
                    if -1 < difference < 1:
                        _LOGGER.debug(
                            "Temperature difference (%.1f°C) is within range (-1°C, 1°C). No adjustment needed.",
                            difference,
                        )
                    else:
                        # Room temperature is below target
                        if difference < -1:
                            voltage = self.current_voltage_usage
                            if voltage is not None and voltage <= 10:  # Check voltage usage
                                _LOGGER.debug("Room temperature is below target; increasing temperature.")
                                await self._hass.services.async_call(
                                    "scene", "turn_on", {"entity_id": self._temperature_up_scene}
                                )
                                await asyncio.sleep(1)  # Sleep between consecutive scene calls
                                await self._hass.services.async_call(
                                    "scene", "turn_on", {"entity_id": self._temperature_up_scene}
                                )

                        # Room temperature is above target
                        elif difference > 1:
                            voltage = self.current_voltage_usage
                            if voltage is not None and voltage >= 10:  # Check voltage usage
                                _LOGGER.debug("Room temperature is above target; decreasing temperature.")
                                await self._hass.services.async_call(
                                    "scene", "turn_on", {"entity_id": self._temperature_down_scene}
                                )
                                await asyncio.sleep(1)  # Sleep between consecutive scene calls
                                await self._hass.services.async_call(
                                    "scene", "turn_on", {"entity_id": self._temperature_down_scene}
                                )

            # Wait before the next check
            await asyncio.sleep(120)  # Check every 2 minutes
