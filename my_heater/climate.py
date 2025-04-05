# climate.py

import asyncio
from datetime import datetime, timedelta, timezone
import logging

# Core Home Assistant components
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, callback, Event, State # Added State for type hinting
from homeassistant.helpers.entity_platform import AddEntitiesCallback
# Event helpers
from homeassistant.helpers.event import async_track_state_change_event
# State restoration helper
from homeassistant.helpers.restore_state import RestoreEntity # Use RestoreEntity for state restoration capabilities

# Local constants
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# --- Configuration Constants ---
MONITORING_INTERVAL_SECONDS = 120  # How often to check temp/power in HEAT mode
Sleep_After_Power_Off = 60      # How long to wait after timer OFF before checking power again
SCENE_ACTIVATION_DELAY = 1.0     # Short delay after activating some scenes
VOLTAGE_ON_THRESHOLD = 10       # Power (W or kW) threshold to consider the heater ON
AUTO_ON_COOLDOWN_SECONDS = 15    # Min duration after manual OFF before auto ON can trigger

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
):
    """Set up climate entities for My Heater from config entry."""
    _LOGGER.debug(
        "Setting up My Heater climate entity. Config Data: %s, Options: %s",
        config_entry.data,
        config_entry.options,
    )
    # Check for essential keys before proceeding
    if "power_usage" not in config_entry.data:
         _LOGGER.error(
             "Configuration data is missing 'power_usage' key/value. "
             "This is required for power monitoring and auto-on features. "
             "Please remove and re-add the integration."
         )
         return False # Indicate setup failure

    async_add_entities([MyHeaterClimate(hass=hass, config_entry=config_entry)])


# Inherit from RestoreEntity to enable state restoration
class MyHeaterClimate(ClimateEntity, RestoreEntity):
    """Representation of My Heater as a climate entity with state restoration."""

    _attr_temperature_unit = "°C"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
    ):
        """Initialize the My Heater climate entity."""
        self._hass = hass
        self.config_entry = config_entry

        # --- Extract configuration from config_entry ---
        # Use .get for safer access, though name is usually set
        self._attr_name = self.config_entry.data.get("heater_name", "My Heater")
        self._scene_turn_on_off = self.config_entry.data["scene_turn_on_off"]
        self._temperature_sensor = self.config_entry.data["temperature_sensor"]
        self._temperature_up_scene = self.config_entry.data["temperature_up_scene"]
        self._temperature_down_scene = self.config_entry.data["temperature_down_scene"]

        self._power_usage_sensor = self.config_entry.data.get("power_usage")
        # Basic validation (more robust check happens in async_setup_entry)
        if not self._power_usage_sensor:
             _LOGGER.warning("%s: Power sensor ID is missing in config data.", self._attr_name)

        # Store min/max/default temps from config
        self._attr_min_temp = self.config_entry.data["min_temp"]
        self._attr_max_temp = self.config_entry.data["max_temp"]
        self._default_temp = self.config_entry.data["default_temp"] # Store default separately

        # --- Entity State (initialized here, potentially overridden by restoration) ---
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = self._default_temp # Initialize with default

        # --- Internal State Management ---
        self._monitoring_task = None        # Holds the asyncio task for monitoring
        self._heat_start_time = None        # When heating started (for timer)
        self._options_listener_remove = None # To unsubscribe from options updates
        self.last_mode_change_time = None   # Track last mode change for cooldowns
        self._power_sensor_listener_remove = None # To unsubscribe from power sensor updates

        # --- Unique ID ---
        # Ensure a unique ID based on the config entry for persistence
        self._attr_unique_id = f"{config_entry.entry_id}_climate"


    # --- Helper Property for Timer Duration ---
    @property
    def _timer_duration_minutes(self) -> int:
        """Return the configured timer duration in minutes."""
        # Get timer from options first, fallback to initial data
        return self.config_entry.options.get(
            "timer", self.config_entry.data.get("timer", 0)
        )

    # --- Standard Properties ---
    @property
    def current_temperature(self):
        """Get the current temperature from the sensor."""
        if not self._temperature_sensor: return None
        sensor_state = self._hass.states.get(self._temperature_sensor)
        if sensor_state and sensor_state.state not in (None, "unknown", "unavailable"):
            try:
                return float(sensor_state.state)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "%s: Sensor %s returned invalid temperature: %s",
                    self.entity_id, self._temperature_sensor, sensor_state.state,
                )
        return None

    @property
    def current_power_usage(self):
        """Get the current power usage from the sensor."""
        if not self._power_usage_sensor: return None
        sensor_state = self._hass.states.get(self._power_usage_sensor)
        if sensor_state and sensor_state.state not in (None, "unknown", "unavailable"):
            try:
                return float(sensor_state.state)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "%s: Power usage sensor %s returned invalid value: %s",
                    self.entity_id, self._power_usage_sensor, sensor_state.state,
                )
        return None

    @property
    def target_temperature_step(self):
        """Return the supported step for temperature adjustment."""
        return 1.0


    # --- Core Methods ---
    async def async_set_hvac_mode(self, hvac_mode):
        """Set the HVAC mode via Home Assistant service call."""
        now = datetime.now(timezone.utc)
        current_mode = self._attr_hvac_mode

        _LOGGER.debug("%s: async_set_hvac_mode called: requested=%s, current=%s", self.entity_id, hvac_mode, current_mode)

        if hvac_mode == current_mode:
             _LOGGER.debug("%s: Requested HVAC mode %s is already active.", self.entity_id, hvac_mode)
             return

        # Stop any existing monitoring task before changing mode
        await self._stop_monitoring_task()
        scene_activated = False

        if hvac_mode == HVACMode.HEAT:
            _LOGGER.debug("%s: Attempting to switch to HEAT mode.", self.entity_id)
            # Simple cooldown to prevent rapid toggling via UI/automation
            if (
                self.last_mode_change_time is not None
                and self.last_mode_change_time > now - timedelta(seconds=10)
            ):
                _LOGGER.debug("%s: Mode changed recently, delaying 10s before switching to HEAT.", self.entity_id)
                await asyncio.sleep(10)

            power = self.current_power_usage
            _LOGGER.debug("%s: Checking power usage for HEAT mode switch: value=%s, threshold=%s", self.entity_id, power, VOLTAGE_ON_THRESHOLD)

            # Activate ON scene only if power is low/unknown (or sensor missing)
            if self._power_usage_sensor and (power is None or power < VOLTAGE_ON_THRESHOLD):
                 _LOGGER.debug("%s: Power usage low/unknown. Activating ON/OFF scene for HEAT.", self.entity_id)
                 scene_activated = await self._activate_scene(self._scene_turn_on_off, "turn ON for HEAT")
                 if scene_activated: await asyncio.sleep(SCENE_ACTIVATION_DELAY)
            elif not self._power_usage_sensor:
                 _LOGGER.debug("%s: Power sensor not configured. Activating ON/OFF scene for HEAT as failsafe.", self.entity_id)
                 scene_activated = await self._activate_scene(self._scene_turn_on_off, "turn ON for HEAT (no power sensor)")
                 if scene_activated: await asyncio.sleep(SCENE_ACTIVATION_DELAY)
            else:
                 # Power is high, assume already ON physically
                 _LOGGER.debug("%s: Power usage (%.2f) >= threshold (%s). Assuming ON. Skipping scene activation.", self.entity_id, power, VOLTAGE_ON_THRESHOLD)

            # Set internal state and start monitoring
            self._attr_hvac_mode = HVACMode.HEAT
            self._heat_start_time = now # Record start time for timer
            self.last_mode_change_time = now # Record mode change time
            await self._start_monitoring_task()
            _LOGGER.info("%s: Set HVAC mode to HEAT. Monitoring task started. Scene activated: %s", self.entity_id, scene_activated)

        elif hvac_mode == HVACMode.OFF:
            _LOGGER.debug("%s: Attempting to switch to OFF mode.", self.entity_id)
            # Activate the OFF scene
            scene_activated = await self._activate_scene(self._scene_turn_on_off, "turn OFF")
            # Set internal state (monitoring task already stopped by call above)
            self._attr_hvac_mode = HVACMode.OFF
            self._heat_start_time = None # Clear timer start time
            self.last_mode_change_time = now # Record mode change time
            _LOGGER.info("%s: Set HVAC mode to OFF. Monitoring task stopped. Scene activated: %s", self.entity_id, scene_activated)

        else:
             _LOGGER.warning("%s: Unsupported HVAC mode requested: %s.", self.entity_id, hvac_mode)
             return

        # Update state in Home Assistant
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set the target temperature by activating scenes."""
        target_temp = kwargs.get(ATTR_TEMPERATURE)
        if target_temp is None: return

        # Validate temperature range
        if not (self._attr_min_temp <= target_temp <= self._attr_max_temp):
            _LOGGER.warning("%s: Target temp %.1f out of range (%.1f-%.1f)", self.entity_id, target_temp, self._attr_min_temp, self._attr_max_temp)
            return

        current_target = self._attr_target_temperature
        # Calculate difference in whole steps
        difference = round(target_temp - current_target)

        if difference == 0:
            _LOGGER.debug("%s: Target temp %.1f already set.", self.entity_id, target_temp)
            # Ensure internal state matches requested state if somehow different
            if self._attr_target_temperature != target_temp:
                 self._attr_target_temperature = target_temp
                 self.async_write_ha_state()
            return

        # Determine scene and number of steps
        if difference > 0:
            scene, action, steps = self._temperature_up_scene, "increase", difference
        else: # difference < 0
            scene, action, steps = self._temperature_down_scene, "decrease", abs(difference)

        _LOGGER.debug("%s: Need to %s temperature by %d steps.", self.entity_id, action, steps)
        success_count = 0
        for i in range(steps):
            _LOGGER.debug("%s: Activating %s scene (Step %d/%d)", self.entity_id, action, i + 1, steps)
            if not await self._activate_scene(scene, f"{action} temp step {i+1}"):
                 _LOGGER.error("%s: Stopping temperature change due to scene activation failure on step %d.", self.entity_id, i + 1)
                 break # Stop trying if one activation fails
            success_count += 1
            await asyncio.sleep(1.5) # Delay between scene activations

        # Update internal target temperature based on successful steps
        # This makes the UI reflect the change even if not all steps completed
        new_calculated_temp = current_target + success_count * (1 if difference > 0 else -1)
        # Clamp calculated temp to min/max just in case
        new_calculated_temp = max(self._attr_min_temp, min(self._attr_max_temp, new_calculated_temp))

        if success_count == steps:
            # If all steps succeeded, ensure it's exactly the requested target_temp
            self._attr_target_temperature = target_temp
            _LOGGER.debug("%s: Finished setting target temperature to: %.1f°C", self.entity_id, self._attr_target_temperature)
        else:
             # If steps failed, update to the partially achieved temperature
             self._attr_target_temperature = new_calculated_temp
             _LOGGER.warning(
                  "%s: Target temperature may not be %.1f due to scene activation errors (%d/%d steps succeeded). Set to %.1f.",
                  self.entity_id, target_temp, success_count, steps, self._attr_target_temperature
             )

        self.async_write_ha_state() # Update HA state with the final temperature


    # --- Monitoring Task Management ---
    async def _start_monitoring_task(self):
        """Start the background monitoring task if not already running."""
        if self._monitoring_task is None or self._monitoring_task.done():
            _LOGGER.debug("%s: Creating and starting monitoring task.", self.entity_id)
            self._monitoring_task = self.hass.async_create_task(self._monitor_power_and_temperature())
        else:
             _LOGGER.debug("%s: Monitoring task already running or starting.", self.entity_id)

    async def _stop_monitoring_task(self):
        """Stop the background monitoring task gracefully."""
        if self._monitoring_task and not self._monitoring_task.done():
            _LOGGER.debug("%s: Attempting to cancel monitoring task.", self.entity_id)
            self._monitoring_task.cancel()
            try:
                await asyncio.wait_for(self._monitoring_task, timeout=2)
            except asyncio.CancelledError:
                _LOGGER.debug("%s: Monitoring task cancelled successfully.", self.entity_id)
            except asyncio.TimeoutError:
                 _LOGGER.warning("%s: Monitoring task did not cancel within timeout.", self.entity_id)
            except Exception as e:
                 _LOGGER.error("%s: Error awaiting monitoring task cancellation: %s", self.entity_id, e)
            finally:
                 self._monitoring_task = None
        else:
             _LOGGER.debug("%s: No active monitoring task to stop.", self.entity_id)


    # --- Monitoring Task ---
    async def _monitor_power_and_temperature(self):
        """Background task for timer and temperature maintenance while in HEAT mode."""
        _LOGGER.info("Monitoring task started for %s.", self.entity_id)
        try:
            while True:
                if self._attr_hvac_mode != HVACMode.HEAT:
                    _LOGGER.info("%s: HVAC mode is no longer HEAT (%s). Stopping monitoring task.", self.entity_id, self._attr_hvac_mode)
                    break

                now = datetime.now(timezone.utc)

                # --- 1. Timer Check --- # <<< CORRECTED TIMER LOGIC >>>
                timer_duration_minutes = self._timer_duration_minutes
                if timer_duration_minutes > 0 and self._heat_start_time:
                    elapsed_seconds = (now - self._heat_start_time).total_seconds()
                    timer_duration_seconds = timer_duration_minutes * 60

                    if elapsed_seconds >= timer_duration_seconds:
                        _LOGGER.info(
                            "%s: Timer expired (elapsed %.1fs >= duration %ds). Initiating turn OFF sequence.",
                            self.entity_id, elapsed_seconds, timer_duration_seconds
                        )
                        # --- Start Timer Turn Off Sequence ---
                        _LOGGER.debug("%s: Timer Expired: Activating OFF scene (Attempt 1).", self.entity_id)
                        first_activation_success = await self._activate_scene(
                            self._scene_turn_on_off, "timer expired turn OFF (1st attempt)"
                        )
                        if not first_activation_success:
                             _LOGGER.warning("%s: Timer Expired: First attempt to activate OFF scene failed.", self.entity_id)

                        _LOGGER.debug("%s: Timer Expired: Waiting %d seconds before checking power state.", self.entity_id, Sleep_After_Power_Off)
                        await asyncio.sleep(Sleep_After_Power_Off)

                        _LOGGER.debug("%s: Timer Expired: Checking power state after delay.", self.entity_id)
                        power_after_delay = self.current_power_usage

                        if self._power_usage_sensor and power_after_delay is not None and power_after_delay >= VOLTAGE_ON_THRESHOLD:
                            _LOGGER.warning(
                                "%s: Timer Expired: Heater power usage (%.2f) still high after %ds. Activating OFF scene again (Attempt 2).",
                                self.entity_id, power_after_delay, Sleep_After_Power_Off
                            )
                            await self._activate_scene(self._scene_turn_on_off, "timer expired turn OFF (2nd attempt)")
                        elif not self._power_usage_sensor:
                             _LOGGER.warning("%s: Timer Expired: Cannot verify power state after delay (no power sensor). Assuming OFF sequence complete.", self.entity_id)
                        else:
                            _LOGGER.debug(
                                "%s: Timer Expired: Heater power usage (%.2f) low or unavailable after %ds. OFF sequence complete.",
                                self.entity_id, power_after_delay if power_after_delay is not None else -1.0, Sleep_After_Power_Off
                            )

                        _LOGGER.info("%s: Timer Expired: Setting internal state to OFF.", self.entity_id)
                        self._attr_hvac_mode = HVACMode.OFF
                        self._heat_start_time = None
                        self.last_mode_change_time = datetime.now(timezone.utc)
                        self.async_write_ha_state()

                        _LOGGER.debug("%s: Timer expiry sequence complete. Exiting monitoring loop.", self.entity_id)
                        break
                        # --- End Timer Turn Off Sequence ---

                # --- 2. Temperature Maintenance Check (Only if Timer Didn't Expire) ---
                if self._attr_hvac_mode == HVACMode.HEAT:
                    current_temp = self.current_temperature
                    target_temp = self._attr_target_temperature

                    if current_temp is not None and target_temp is not None:
                        difference = current_temp - target_temp
                        power = self.current_power_usage

                        _LOGGER.debug(
                            "%s: [Monitor] Current: %.1f, Target: %.1f, Diff: %.1f, Power: %s",
                            self.entity_id, current_temp, target_temp, difference, power
                        )

                        if self._power_usage_sensor and power is not None:
                            # Temp too low AND heater seems OFF -> Try turning UP
                            if difference < -1 and power <= VOLTAGE_ON_THRESHOLD:
                                _LOGGER.debug("%s: [Monitor] Temp too low (%.1f) & power low. Activating UP scene.", self.entity_id, difference)
                                if await self._activate_scene(self._temperature_up_scene, "monitor temp up"):
                                    await asyncio.sleep(SCENE_ACTIVATION_DELAY)
                                    await self._activate_scene(self._temperature_up_scene, "monitor temp up 2")

                            # Temp too high AND heater seems ON -> Try turning DOWN
                            elif difference > 1 and power >= VOLTAGE_ON_THRESHOLD:
                                _LOGGER.debug("%s: [Monitor] Temp too high (%.1f) & power high. Activating DOWN scene.", self.entity_id, difference)
                                if await self._activate_scene(self._temperature_down_scene, "monitor temp down"):
                                    await asyncio.sleep(SCENE_ACTIVATION_DELAY)
                                    await self._activate_scene(self._temperature_down_scene, "monitor temp down 2")
                            else:
                                _LOGGER.debug("%s: [Monitor] Temp/Power state OK (Diff: %.1f, Power: %.1f).", self.entity_id, difference, power)
                        elif not self._power_usage_sensor:
                            _LOGGER.debug("%s: [Monitor] Power sensor not configured. Skipping temperature maintenance.", self.entity_id)
                        else: # power is None
                            _LOGGER.debug("%s: [Monitor] Power sensor unavailable. Skipping temperature maintenance check.", self.entity_id)
                    else:
                        _LOGGER.debug("%s: [Monitor] Current or target temperature unavailable.", self.entity_id)

                # --- Wait for next interval ---
                if self._attr_hvac_mode == HVACMode.HEAT:
                    _LOGGER.debug("%s: [Monitor] Sleeping for %d seconds.", self.entity_id, MONITORING_INTERVAL_SECONDS)
                    await asyncio.sleep(MONITORING_INTERVAL_SECONDS)
                else:
                    _LOGGER.debug("%s: [Monitor] Mode changed during checks. Exiting loop instead of sleeping.", self.entity_id)
                    break

        except asyncio.CancelledError:
            _LOGGER.info("%s: Monitoring task explicitly cancelled.", self.entity_id)
        except Exception as e:
            _LOGGER.exception("%s: Unhandled exception in monitoring task: %s", self.entity_id, e)
        finally:
            _LOGGER.info("%s: Monitoring task stopped.", self.entity_id)
            if self._monitoring_task and self._monitoring_task.done():
                self._monitoring_task = None


    # --- Power Sensor Change Callback ---
    @callback
    async def _async_power_sensor_changed(self, event: Event) -> None:
        """Handle state changes of the power usage sensor to auto-turn ON."""
        new_state: State | None = event.data.get("new_state")
        entity_id = event.data.get("entity_id")

        if new_state is None or new_state.state in (None, "unknown", "unavailable"):
            _LOGGER.debug("%s: Power sensor %s changed to unavailable state: %s. Ignoring.", self.entity_id, entity_id, new_state.state if new_state else "None")
            return

        if self._attr_hvac_mode != HVACMode.OFF:
            _LOGGER.debug("%s: Power sensor %s changed, but climate is not OFF (Mode: %s). Ignoring for auto-on.", self.entity_id, entity_id, self._attr_hvac_mode)
            return

        now = datetime.now(timezone.utc)
        if self.last_mode_change_time and (now - self.last_mode_change_time) < timedelta(seconds=AUTO_ON_COOLDOWN_SECONDS):
             _LOGGER.debug("%s: Power sensor %s changed, but mode changed recently (<%ds ago). Ignoring potential auto-on flip-back.", self.entity_id, entity_id, AUTO_ON_COOLDOWN_SECONDS)
             return

        try:
            power_usage = float(new_state.state)
            _LOGGER.debug("%s: Power sensor %s changed to %.2f.", self.entity_id, entity_id, power_usage)
        except (ValueError, TypeError):
            _LOGGER.warning("%s: Power sensor %s returned non-numeric state: %s. Cannot check threshold.", self.entity_id, entity_id, new_state.state)
            return

        # --- Auto-ON Logic ---
        if power_usage > VOLTAGE_ON_THRESHOLD:
            _LOGGER.info(
                "%s: Auto-ON condition met. Power usage %.2f > threshold %.1f while HVAC mode was OFF. Setting mode to HEAT.",
                self.entity_id, power_usage, VOLTAGE_ON_THRESHOLD
            )
            # Set state to HEAT
            self._attr_hvac_mode = HVACMode.HEAT
            current_time = datetime.now(timezone.utc)
            if self._heat_start_time is None: # Should be None if mode was OFF
                 self._heat_start_time = current_time
            self.last_mode_change_time = current_time
            self.async_write_ha_state()
            # Start the monitoring task
            await self._start_monitoring_task()
        else:
             _LOGGER.debug("%s: Power sensor %s changed to %.2f, below threshold %.1f. No auto-on action needed.", self.entity_id, entity_id, power_usage, VOLTAGE_ON_THRESHOLD)


    # --- Scene Activation Helper ---
    async def _activate_scene(self, scene_entity_id, action_description=""):
        """Helper to call the scene.turn_on service."""
        if not scene_entity_id:
             _LOGGER.error("%s: Scene entity ID missing for action: %s", self.entity_id, action_description)
             return False
        _LOGGER.debug("%s: Activating scene '%s' for action: %s", self.entity_id, scene_entity_id, action_description)
        try:
            await self.hass.services.async_call(
                domain="scene",
                service="turn_on",
                service_data={"entity_id": scene_entity_id},
                blocking=True
            )
            _LOGGER.debug("%s: Successfully called service for scene '%s'", self.entity_id, scene_entity_id)
            return True
        except asyncio.TimeoutError:
             _LOGGER.error("%s: Timeout error during scene activation for '%s'", self.entity_id, action_description)
        except Exception as e:
            _LOGGER.error("%s: Error calling scene '%s' for action '%s': %s", self.entity_id, scene_entity_id, action_description, e)
        return False


    # --- Entity Lifecycle and Options Handling ---
    async def async_added_to_hass(self):
        """Run when entity about to be added to hass, including state restoration."""
        # Call RestoreEntity's async_added_to_hass first
        await super().async_added_to_hass()
        _LOGGER.debug("%s: Entity added to HASS. Attempting state restoration.", self.entity_id)

        # --- State Restoration ---
        last_state = await self.async_get_last_state() # From RestoreEntity
        restored_mode = None
        restored_temp = None

        if last_state is not None:
            _LOGGER.debug("%s: Found last state: %s", self.entity_id, last_state)
            # Restore HVAC mode
            if last_state.state in self._attr_hvac_modes:
                restored_mode = last_state.state
                _LOGGER.debug("%s: Restored hvac_mode: %s", self.entity_id, restored_mode)
            else:
                 _LOGGER.warning("%s: Invalid hvac_mode '%s' found in last state.", self.entity_id, last_state.state)

            # Restore target temperature
            if ATTR_TEMPERATURE in last_state.attributes:
                temp_from_state = last_state.attributes[ATTR_TEMPERATURE]
                try:
                    temp_float = float(temp_from_state)
                    if self._attr_min_temp <= temp_float <= self._attr_max_temp:
                        restored_temp = temp_float
                        _LOGGER.debug("%s: Restored target_temperature: %.1f", self.entity_id, restored_temp)
                    else:
                        _LOGGER.warning("%s: Restored target_temperature %.1f out of range (%.1f-%.1f). Will use default.", self.entity_id, temp_float, self._attr_min_temp, self._attr_max_temp)
                except (ValueError, TypeError):
                     _LOGGER.warning("%s: Invalid target_temperature '%s' found in last_state attributes.", self.entity_id, temp_from_state)
            else:
                _LOGGER.debug("%s: target_temperature not found in last_state attributes.", self.entity_id)
        else:
            _LOGGER.debug("%s: No last state found.", self.entity_id)

        # --- Apply Restored State or Defaults ---
        self._attr_hvac_mode = restored_mode if restored_mode is not None else HVACMode.OFF
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = restored_temp if restored_temp is not None else self._default_temp # Use stored default

        _LOGGER.debug("%s: Final initial state - Mode: %s, Target Temp: %.1f",
                      self.entity_id, self._attr_hvac_mode, self._attr_target_temperature)

        # If restored to HEAT mode, reset the start time
        if self._attr_hvac_mode == HVACMode.HEAT:
             self._heat_start_time = datetime.now(timezone.utc)
             _LOGGER.debug("%s: Mode is HEAT after restoration/init, setting heat_start_time.", self.entity_id)


        # --- Setup Listeners ---
        self._options_listener_remove = self.config_entry.add_update_listener(
            self._async_options_updated
        )
        _LOGGER.debug("%s: Added options update listener.", self.entity_id)

        if self._power_usage_sensor:
            _LOGGER.debug("%s: Adding power sensor listener for %s", self.entity_id, self._power_usage_sensor)
            self._power_sensor_listener_remove = async_track_state_change_event(
                self.hass, [self._power_usage_sensor], self._async_power_sensor_changed
            )
        else:
            _LOGGER.debug("%s: Power sensor not configured, skipping auto-on listener setup.", self.entity_id)

        # --- Start Monitoring Task if Needed ---
        if self._attr_hvac_mode == HVACMode.HEAT:
            if not self._monitoring_task or self._monitoring_task.done():
               _LOGGER.info("%s: Starting monitoring task on add_to_hass as final mode is HEAT.", self.entity_id)
               # Use create_task to avoid blocking setup if task takes time to start
               self.hass.create_task(self._start_monitoring_task())


    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        _LOGGER.debug("%s: Entity removing from HASS.", self.entity_id)
        # Remove options listener
        if self._options_listener_remove:
            self._options_listener_remove()
            _LOGGER.debug("%s: Removed options update listener.", self.entity_id)
            self._options_listener_remove = None

        # Remove power sensor listener
        if self._power_sensor_listener_remove:
            self._power_sensor_listener_remove()
            _LOGGER.debug("%s: Removed power sensor listener.", self.entity_id)
            self._power_sensor_listener_remove = None

        # Stop monitoring task
        await self._stop_monitoring_task()
        # Call super last
        await super().async_will_remove_from_hass()
        _LOGGER.debug("%s: Entity removal complete.", self.entity_id)


    @callback
    def _async_options_updated(self, hass: HomeAssistant, entry: ConfigEntry):
        """Handle options update from the UI."""
        _LOGGER.info(
            "%s: Options updated: %s. Monitoring task will use new values on next check.",
             self.entity_id, entry.options
        )
        # Update state if visual representation depends on options (e.g., timer display)
        self.async_write_ha_state()