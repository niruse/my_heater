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
from .const import (
    DOMAIN,
    CONF_POWER_THRESHOLD,
    CONF_IDLE_OFF_MINUTES,
    CONF_TEMP_MAINTENANCE,
    DEFAULT_POWER_THRESHOLD,
    DEFAULT_IDLE_OFF_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

# --- Configuration Constants ---
MONITORING_INTERVAL_SECONDS = 30   # How often to check timer / idle / temp in HEAT mode
TEMP_MAINTENANCE_INTERVAL_SECONDS = 120  # Min gap between temp-maintenance IR presses
SCENE_ACTIVATION_DELAY = 1.0     # Short delay after activating some scenes
# After WE send an IR command the heater needs time to react and the plug needs time
# to report it. Real capture (2026-10-27): after "off" the heater draws ~21 W for
# 20-35 s while it winds down, and the plug reports the drop up to ~36 s later.
# Power readings inside this window are never used to guess the heater's state.
COMMAND_SETTLE_SECONDS = 90
# Auto-ON double check: a high reading must still be high this long after it was seen.
AUTO_ON_CONFIRM_SECONDS = 20
# OFF double check: after the OFF toggle, power must be below the threshold by then.
OFF_VERIFY_SECONDS = 90
OFF_VERIFY_RETRIES = 1           # Extra OFF toggles if the heater is still drawing power

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
        self._verify_task = None            # OFF double-check after an OFF toggle
        self._confirm_task = None           # Auto-ON double-check after a high reading
        self._heat_start_time = None        # When heating started (for timer)
        self._options_listener_remove = None # To unsubscribe from options updates
        self.last_mode_change_time = None   # Track last mode change for cooldowns
        self._last_command_time = None      # Last time WE sent the on/off IR toggle
        self._last_heating_seen = None      # Last time the plug showed real heating
        self._last_maintenance_time = None  # Last temp-maintenance IR press
        self._last_sync_reason = None       # Why the mode last changed without a user command
        self._power_sensor_listener_remove = None # To unsubscribe from power sensor updates

        # --- Unique ID ---
        # Ensure a unique ID based on the config entry for persistence
        self._attr_unique_id = f"{config_entry.entry_id}_climate"


    # --- Helper Properties for Options ---
    def _option(self, key, default):
        """Return an option value: options -> data -> default."""
        return self.config_entry.options.get(key, self.config_entry.data.get(key, default))

    @property
    def _timer_duration_minutes(self) -> int:
        """Return the configured timer duration in minutes."""
        return self._option("timer", 0)

    @property
    def _power_threshold(self) -> float:
        """Watts above which the heater is really heating (not winding down)."""
        return float(self._option(CONF_POWER_THRESHOLD, DEFAULT_POWER_THRESHOLD))

    @property
    def _idle_off_minutes(self) -> int:
        """Minutes in HEAT without any heating before the heater is treated as off."""
        return int(self._option(CONF_IDLE_OFF_MINUTES, DEFAULT_IDLE_OFF_MINUTES))

    @property
    def _temp_maintenance_enabled(self) -> bool:
        """Whether the monitor may press the UP/DOWN scenes on its own."""
        return bool(self._option(CONF_TEMP_MAINTENANCE, False))

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
        """Get the current power usage from the sensor, in watts."""
        if not self._power_usage_sensor: return None
        return self._power_watts(self._hass.states.get(self._power_usage_sensor))

    def _power_watts(self, sensor_state: State | None):
        """Convert a power sensor state to watts (kW sensors are scaled up)."""
        if sensor_state and sensor_state.state not in (None, "unknown", "unavailable"):
            try:
                value = float(sensor_state.state)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "%s: Power usage sensor %s returned invalid value: %s",
                    self.entity_id, self._power_usage_sensor, sensor_state.state,
                )
                return None
            if sensor_state.attributes.get("unit_of_measurement") == "kW":
                value *= 1000
            return value
        return None

    def _is_heating(self, power) -> bool:
        """True only for a reading that proves the heating element is on."""
        return power is not None and power >= self._power_threshold

    def _in_settle_window(self, now=None) -> bool:
        """True while the heater may still be reacting to our last IR toggle."""
        if self._last_command_time is None:
            return False
        now = now or datetime.now(timezone.utc)
        return (now - self._last_command_time).total_seconds() < COMMAND_SETTLE_SECONDS

    @property
    def target_temperature_step(self):
        """Return the supported step for temperature adjustment."""
        return 1.0

    @property
    def extra_state_attributes(self):
        """Expose what the integration believes and why, for troubleshooting."""
        return {
            "power_threshold": self._power_threshold,
            "last_heating_seen": self._last_heating_seen.isoformat() if self._last_heating_seen else None,
            "last_command": self._last_command_time.isoformat() if self._last_command_time else None,
            "last_sync_reason": self._last_sync_reason,
        }


    # --- Core Methods ---
    async def async_set_hvac_mode(self, hvac_mode):
        """Set the HVAC mode via Home Assistant service call."""
        now = datetime.now(timezone.utc)
        current_mode = self._attr_hvac_mode

        _LOGGER.debug("%s: async_set_hvac_mode called: requested=%s, current=%s", self.entity_id, hvac_mode, current_mode)

        if hvac_mode == current_mode:
             _LOGGER.debug("%s: Requested HVAC mode %s is already active.", self.entity_id, hvac_mode)
             return

        # Stop any existing monitoring / double-check tasks before changing mode
        await self._stop_monitoring_task()
        self._cancel_task("_verify_task")
        self._cancel_task("_confirm_task")
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
            _LOGGER.debug("%s: Checking power usage for HEAT mode switch: value=%s, threshold=%s", self.entity_id, power, self._power_threshold)

            # Send the toggle unless the plug proves the heater is already heating
            if self._is_heating(power):
                 _LOGGER.debug("%s: Power usage (%.2f) >= threshold (%s). Assuming ON. Skipping scene activation.", self.entity_id, power, self._power_threshold)
            else:
                 _LOGGER.debug("%s: Power usage low/unknown. Activating ON/OFF scene for HEAT.", self.entity_id)
                 scene_activated = await self._send_toggle("turn ON for HEAT")
                 if scene_activated: await asyncio.sleep(SCENE_ACTIVATION_DELAY)

            # Set internal state and start monitoring
            now = datetime.now(timezone.utc)
            self._attr_hvac_mode = HVACMode.HEAT
            self._heat_start_time = now # Record start time for timer
            self._last_heating_seen = now # Idle-off counts from here
            self.last_mode_change_time = now # Record mode change time
            self._last_sync_reason = None
            await self._start_monitoring_task()
            _LOGGER.info("%s: Set HVAC mode to HEAT. Monitoring task started. Scene activated: %s", self.entity_id, scene_activated)

        elif hvac_mode == HVACMode.OFF:
            _LOGGER.debug("%s: Attempting to switch to OFF mode.", self.entity_id)
            scene_activated = await self._send_toggle("turn OFF")
            self._attr_hvac_mode = HVACMode.OFF
            self._heat_start_time = None # Clear timer start time
            self.last_mode_change_time = datetime.now(timezone.utc) # Record mode change time
            self._last_sync_reason = None
            # Double check in the background that the heater really stopped
            self._verify_task = self.hass.async_create_task(self._verify_off("manual OFF"))
            _LOGGER.info("%s: Set HVAC mode to OFF. OFF verification scheduled. Scene activated: %s", self.entity_id, scene_activated)

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
            _LOGGER.debug("%s: Finished setting target temperature to: %.1f", self.entity_id, self._attr_target_temperature)
        else:
             # If steps failed, update to the partially achieved temperature
             self._attr_target_temperature = new_calculated_temp
             _LOGGER.warning(
                  "%s: Target temperature may not be %.1f due to scene activation errors (%d/%d steps succeeded). Set to %.1f.",
                  self.entity_id, target_temp, success_count, steps, self._attr_target_temperature
             )

        self.async_write_ha_state() # Update HA state with the final temperature


    # --- Task Management ---
    def _cancel_task(self, attr):
        """Cancel a helper task stored on self.<attr> (if it is not the caller)."""
        task = getattr(self, attr)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        setattr(self, attr, None)

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
            if self._monitoring_task is asyncio.current_task():
                # Called from inside the monitor (timer/idle-off): it exits on its own
                self._monitoring_task = None
                return
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


    # --- OFF Double Check ---
    async def _verify_off(self, reason):
        """After an OFF toggle, confirm with the plug that heating stopped.

        Only a reading at or above the threshold counts as "still on": the ~21 W
        wind-down tail and a heater idling on its own thermostat both read low.
        A missed IR command leaves the element drawing 1-2 kW, so a high reading
        after OFF_VERIFY_SECONDS proves the toggle did not land and is safe to resend.
        """
        try:
            for attempt in range(OFF_VERIFY_RETRIES + 1):
                await asyncio.sleep(OFF_VERIFY_SECONDS)
                if self._attr_hvac_mode != HVACMode.OFF:
                    return
                power = self.current_power_usage
                if not self._is_heating(power):
                    _LOGGER.debug("%s: OFF verified after %s (power %s).", self.entity_id, reason, power)
                    return
                if attempt < OFF_VERIFY_RETRIES:
                    _LOGGER.warning(
                        "%s: Still drawing %.1f W %ds after %s. Sending OFF toggle again.",
                        self.entity_id, power, OFF_VERIFY_SECONDS, reason,
                    )
                    await self._send_toggle(f"{reason} retry {attempt + 1}")
            # Still heating after the retries: tell the truth instead of showing OFF
            _LOGGER.error(
                "%s: Heater still drawing %.1f W after %d OFF attempts. Showing HEAT.",
                self.entity_id, power, OFF_VERIFY_RETRIES + 1,
            )
            self._sync_mode(HVACMode.HEAT, "off_failed")
        except asyncio.CancelledError:
            pass


    # --- Mode Sync (no IR sent) ---
    def _sync_mode(self, hvac_mode, reason):
        """Change the shown mode to match the real heater, without sending IR."""
        now = datetime.now(timezone.utc)
        self._attr_hvac_mode = hvac_mode
        self.last_mode_change_time = now
        self._last_sync_reason = reason
        if hvac_mode == HVACMode.HEAT:
            if self._heat_start_time is None:
                self._heat_start_time = now
            self._last_heating_seen = now
            self.hass.async_create_task(self._start_monitoring_task())
        else:
            self._heat_start_time = None
        _LOGGER.info("%s: Synced mode to %s (%s).", self.entity_id, hvac_mode, reason)
        self.async_write_ha_state()


    # --- Monitoring Task ---
    async def _monitor_power_and_temperature(self):
        """Background task for timer, idle-off detection and temperature maintenance while in HEAT mode."""
        _LOGGER.info("Monitoring task started for %s.", self.entity_id)
        try:
            while True:
                if self._attr_hvac_mode != HVACMode.HEAT:
                    _LOGGER.info("%s: HVAC mode is no longer HEAT (%s). Stopping monitoring task.", self.entity_id, self._attr_hvac_mode)
                    break

                now = datetime.now(timezone.utc)
                power = self.current_power_usage
                if self._is_heating(power):
                    self._last_heating_seen = now

                # --- 1. Timer Check ---
                timer_duration_minutes = self._timer_duration_minutes
                if timer_duration_minutes > 0 and self._heat_start_time:
                    elapsed_seconds = (now - self._heat_start_time).total_seconds()
                    if elapsed_seconds >= timer_duration_minutes * 60:
                        _LOGGER.info(
                            "%s: Timer expired (elapsed %.1fs >= %d min). Turning OFF.",
                            self.entity_id, elapsed_seconds, timer_duration_minutes
                        )
                        await self._send_toggle("timer expired turn OFF")
                        self._attr_hvac_mode = HVACMode.OFF
                        self._heat_start_time = None
                        self.last_mode_change_time = datetime.now(timezone.utc)
                        self._last_sync_reason = "timer"
                        self.async_write_ha_state()
                        self._cancel_task("_verify_task")
                        self._verify_task = self.hass.async_create_task(self._verify_off("timer OFF"))
                        break

                # --- 2. Idle-Off Check: the heater was turned off outside HA ---
                # The heater's own thermostat pauses heating for a few minutes at a
                # time (2-7 min observed). Much longer with no heating at all means
                # it was switched off by its own remote/button or lost power.
                idle_minutes = self._idle_off_minutes
                if (
                    idle_minutes > 0
                    and self._last_heating_seen is not None
                    and not self._in_settle_window(now)
                    and power is not None
                    and not self._is_heating(power)
                    and (now - self._last_heating_seen) >= timedelta(minutes=idle_minutes)
                ):
                    _LOGGER.info(
                        "%s: No heating for %d min (power %.1f W). Heater is off; syncing to OFF without IR.",
                        self.entity_id, idle_minutes, power,
                    )
                    self._sync_mode(HVACMode.OFF, "no_heating")
                    break

                # --- 3. Temperature Maintenance (opt-in) ---
                if self._temp_maintenance_enabled:
                    await self._maintain_temperature(now, power)

                await asyncio.sleep(MONITORING_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            _LOGGER.info("%s: Monitoring task explicitly cancelled.", self.entity_id)
        except Exception as e:
            _LOGGER.exception("%s: Unhandled exception in monitoring task: %s", self.entity_id, e)
        finally:
            _LOGGER.info("%s: Monitoring task stopped.", self.entity_id)
            if self._monitoring_task is asyncio.current_task():
                self._monitoring_task = None

    async def _maintain_temperature(self, now, power):
        """Press UP/DOWN when the room is far from target (only if enabled in options).

        Off by default: when the target is the heater's own dial value (e.g. 43)
        rather than a room temperature, this pressed UP every 2 minutes forever.
        """
        if self._last_maintenance_time and (now - self._last_maintenance_time).total_seconds() < TEMP_MAINTENANCE_INTERVAL_SECONDS:
            return
        current_temp = self.current_temperature
        target_temp = self._attr_target_temperature
        if current_temp is None or target_temp is None or power is None:
            return
        difference = current_temp - target_temp
        if difference < -1 and not self._is_heating(power):
            _LOGGER.debug("%s: [Monitor] Temp too low (%.1f) & not heating. Activating UP scene.", self.entity_id, difference)
            self._last_maintenance_time = now
            await self._activate_scene(self._temperature_up_scene, "monitor temp up")
        elif difference > 1 and self._is_heating(power):
            _LOGGER.debug("%s: [Monitor] Temp too high (%.1f) & heating. Activating DOWN scene.", self.entity_id, difference)
            self._last_maintenance_time = now
            await self._activate_scene(self._temperature_down_scene, "monitor temp down")


    # --- Power Sensor Change Callback ---
    @callback
    def _async_power_sensor_changed(self, event: Event) -> None:
        """Handle power sensor updates: remember real heating, and auto-ON with a double check."""
        new_state: State | None = event.data.get("new_state")
        power = self._power_watts(new_state)
        if power is None:
            return

        now = datetime.now(timezone.utc)
        if self._is_heating(power):
            self._last_heating_seen = now

        if self._attr_hvac_mode != HVACMode.OFF or not self._is_heating(power):
            return
        if self._in_settle_window(now):
            # Our own toggle is still landing (wind-down tail, late plug report)
            _LOGGER.debug("%s: %.1f W within %ds of our IR command. Ignoring for auto-on.", self.entity_id, power, COMMAND_SETTLE_SECONDS)
            return
        if self._confirm_task and not self._confirm_task.done():
            return
        _LOGGER.debug("%s: %.1f W while OFF. Confirming in %ds before auto-on.", self.entity_id, power, AUTO_ON_CONFIRM_SECONDS)
        self._confirm_task = self.hass.async_create_task(self._confirm_auto_on())

    async def _confirm_auto_on(self):
        """Second look: the heater must still be heating before we show HEAT."""
        try:
            await asyncio.sleep(AUTO_ON_CONFIRM_SECONDS)
            power = self.current_power_usage
            if (
                self._attr_hvac_mode == HVACMode.OFF
                and not self._in_settle_window()
                and self._is_heating(power)
            ):
                _LOGGER.info(
                    "%s: Auto-ON confirmed: %.1f W >= %.0f W twice, %ds apart. Setting mode to HEAT.",
                    self.entity_id, power, self._power_threshold, AUTO_ON_CONFIRM_SECONDS,
                )
                self._sync_mode(HVACMode.HEAT, "heating_detected")
            else:
                _LOGGER.debug("%s: Auto-ON not confirmed (power %s).", self.entity_id, power)
        except asyncio.CancelledError:
            pass


    # --- Scene Activation Helpers ---
    async def _send_toggle(self, action_description):
        """Send the on/off IR toggle and start the settle window."""
        self._last_command_time = datetime.now(timezone.utc)
        return await self._activate_scene(self._scene_turn_on_off, action_description)

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
        restored_temp = None

        if last_state is not None:
            _LOGGER.debug("%s: Found last state: %s", self.entity_id, last_state)
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

        # The mode is never restored: after a restart the plug decides (auto-ON below)
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = restored_temp if restored_temp is not None else self._default_temp # Use stored default

        _LOGGER.debug("%s: Final initial state - Mode: %s, Target Temp: %.1f",
                      self.entity_id, self._attr_hvac_mode, self._attr_target_temperature)

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
            # Heater already heating when HA starts: show it (same double check)
            if self._is_heating(self.current_power_usage):
                self._confirm_task = self.hass.async_create_task(self._confirm_auto_on())
        else:
            _LOGGER.debug("%s: Power sensor not configured, skipping auto-on listener setup.", self.entity_id)


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

        # Stop background tasks
        self._cancel_task("_verify_task")
        self._cancel_task("_confirm_task")
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
