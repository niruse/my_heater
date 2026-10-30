# climate.py

import asyncio
from datetime import datetime, timedelta, timezone
import logging

# Core Home Assistant components
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import HVACAction, HVACMode
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
    CONF_MODE_BUTTON,
    DEFAULT_POWER_THRESHOLD,
    DEFAULT_IDLE_OFF_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

# --- Configuration Constants ---
MONITORING_INTERVAL_SECONDS = 30   # How often to check timer / idle / temp in HEAT mode
# Off or just resting? The heater stops on its own thermostat once the room is warm,
# and the plug reads 0 W then, exactly as when it is off. The room temperature tells
# them apart (bathroom, 2026-10-28): resting, it reheats once the room falls only
# 0.1-0.4 deg below its peak; switched off, the room kept falling (32.2 -> 28.2 in
# 2.5 h). So: no heating and the room ROOM_DROP_OFF below its peak since the last
# heating = off. With a room reading, the time-only idle-off waits this long instead.
ROOM_DROP_OFF = 1.0
IDLE_OFF_WITH_ROOM_TEMP_MINUTES = 180
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
# Fan-only mode (parents heater, 2026-10-30): the remote's bars button cycles
# low heat (~950 W) -> high heat (~1860 W) -> fan only (~22 W) -> low heat, and the
# plug shows each press 18-30 s later. The fan alone draws about what the wind-down
# tail after OFF does (~21 W), but it holds: the tail lasted 11-41 s in every
# capture (10-27/10-28), so a fan-band reading held FAN_CONFIRM_SECONDS is fan mode.
FAN_MIN_WATTS = 10
FAN_MAX_WATTS = 60
FAN_CONFIRM_SECONDS = 60
MODE_STEP_SECONDS = 45           # wait after each mode press before reading the plug
# A press sometimes moves the heater two levels (the IR lands twice: 10-30, high ->
# fan -> low within ~15 s on both bathroom heaters), so allow two full turns.
MODE_MAX_PRESSES = 6
# The bars key is the HOTBOX KPT-2000B power level: 1 = fan only, 2 = half heat
# (~1 kW), 3 = full heat (~2 kW). Watts at or above this read as level 3.
FULL_HEAT_WATTS = 1500
FAN_STOPPED_PASSES = 2           # monitor passes at ~0 W before FAN_ONLY shows OFF

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

        # Options can replace it (10-30: the downstairs entry pointed at a localtuya
        # sensor reading 10x the real watts)
        self._power_usage_sensor = self._option("power_usage", None)
        # Basic validation (more robust check happens in async_setup_entry)
        if not self._power_usage_sensor:
             _LOGGER.warning("%s: Power sensor ID is missing in config data.", self._attr_name)

        # Store min/max/default temps from config
        self._attr_min_temp = self.config_entry.data["min_temp"]
        self._attr_max_temp = self.config_entry.data["max_temp"]
        # Optional mode button: with it the heater also offers fan only
        self._mode_button = self._option(CONF_MODE_BUTTON, None)
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT] + ([HVACMode.FAN_ONLY] if self._mode_button else [])
        self._default_temp = self.config_entry.data["default_temp"] # Store default separately

        # --- Entity State (initialized here, potentially overridden by restoration) ---
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = self._default_temp # Initialize with default

        # --- Internal State Management ---
        self._monitoring_task = None        # Holds the asyncio task for monitoring
        self._verify_task = None            # OFF double-check after an OFF toggle
        self._confirm_task = None           # Auto-ON double-check after a high reading
        self._mode_task = None              # Presses the mode button until the plug agrees
        self._fan_confirm_task = None       # Fan-mode double-check after a fan-band reading
        self._fan_zero_passes = 0           # Monitor passes at ~0 W while in FAN_ONLY
        self._heat_start_time = None        # When heating started (for timer)
        self._options_listener_remove = None # To unsubscribe from options updates
        self.last_mode_change_time = None   # Track last mode change for cooldowns
        self._last_command_time = None      # Last time WE sent the on/off IR toggle
        self._last_heating_seen = None      # Last time the plug showed real heating
        self._rest_peak_temp = None         # Warmest room reading since heating last stopped
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

    def _is_fan(self, power) -> bool:
        """True for a reading in the fan-only band (only with a mode button)."""
        return bool(self._mode_button) and power is not None and FAN_MIN_WATTS <= power <= FAN_MAX_WATTS

    def _stepping_mode(self) -> bool:
        """True while we are pressing the mode button towards a target mode."""
        return self._mode_task is not None and not self._mode_task.done()

    def _in_settle_window(self, now=None) -> bool:
        """True while the heater may still be reacting to our last IR toggle."""
        if self._last_command_time is None:
            return False
        now = now or datetime.now(timezone.utc)
        return (now - self._last_command_time).total_seconds() < COMMAND_SETTLE_SECONDS

    @property
    def hvac_action(self):
        """What the heater is doing now, read from the plug."""
        if self._attr_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        power = self.current_power_usage
        if self._is_heating(power):
            return HVACAction.HEATING
        if self._attr_hvac_mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        return HVACAction.IDLE

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
            "rest_peak_temperature": self._rest_peak_temp,
            # Saved with the state, so a restart resumes the timer instead of restarting it
            "heat_started": self._heat_start_time.isoformat() if self._heat_start_time else None,
            "last_command": self._last_command_time.isoformat() if self._last_command_time else None,
            "last_sync_reason": self._last_sync_reason,
            "power_level": self._power_level(self.current_power_usage),
        }

    def _power_level(self, power):
        """Bars level read from the plug: 1 fan, 2 half heat, 3 full heat (None at rest/off)."""
        if not self._mode_button or power is None:
            return None
        if self._is_fan(power):
            return 1
        if self._is_heating(power):
            return 3 if power >= FULL_HEAT_WATTS else 2
        return None


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
        self._cancel_task("_mode_task")
        self._cancel_task("_fan_confirm_task")
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

            # Already on in fan only: the toggle would switch it OFF, step the mode instead
            if current_mode == HVACMode.FAN_ONLY:
                 _LOGGER.debug("%s: In FAN_ONLY. Stepping the mode to heat, no toggle.", self.entity_id)
            # Send the toggle unless the plug proves the heater is already heating
            elif self._is_heating(power):
                 _LOGGER.debug("%s: Power usage (%.2f) >= threshold (%s). Assuming ON. Skipping scene activation.", self.entity_id, power, self._power_threshold)
            else:
                 _LOGGER.debug("%s: Power usage low/unknown. Activating ON/OFF scene for HEAT.", self.entity_id)
                 scene_activated = await self._send_toggle("turn ON for HEAT")
                 if scene_activated: await asyncio.sleep(SCENE_ACTIVATION_DELAY)

            # Set internal state and start monitoring
            now = datetime.now(timezone.utc)
            self._attr_hvac_mode = HVACMode.HEAT
            if current_mode != HVACMode.FAN_ONLY or self._heat_start_time is None:
                self._heat_start_time = now # Record start time for timer (fan -> heat keeps it)
            self._rest_peak_temp = None  # a peak from an earlier session must not count
            self._last_heating_seen = now # Idle-off counts from here
            self.last_mode_change_time = now # Record mode change time
            self._last_sync_reason = None
            await self._start_monitoring_task()
            if self._mode_button and (current_mode == HVACMode.FAN_ONLY or scene_activated):
                # Switched on, the heater resumes its last mode, which may be fan only
                self._mode_task = self.hass.async_create_task(
                    self._step_mode_to(HVACMode.HEAT, wait_first=scene_activated))
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

        elif hvac_mode == HVACMode.FAN_ONLY and self._mode_button:
            power = self.current_power_usage
            switched_on = False
            if current_mode == HVACMode.OFF and not self._is_heating(power) and not self._is_fan(power):
                switched_on = await self._send_toggle("turn ON for FAN")
            now = datetime.now(timezone.utc)
            self._attr_hvac_mode = HVACMode.FAN_ONLY
            if self._heat_start_time is None:
                self._heat_start_time = now # The timer covers fan only too
            self.last_mode_change_time = now
            self._last_sync_reason = None
            self._fan_zero_passes = 0
            self._mode_task = self.hass.async_create_task(
                self._step_mode_to(HVACMode.FAN_ONLY, wait_first=switched_on))
            await self._start_monitoring_task()
            _LOGGER.info("%s: Set HVAC mode to FAN_ONLY. Toggle sent: %s", self.entity_id, switched_on)

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
                if not self._is_heating(power) and not self._is_fan(power):
                    _LOGGER.debug("%s: OFF verified after %s (power %s).", self.entity_id, reason, power)
                    return
                if attempt < OFF_VERIFY_RETRIES:
                    _LOGGER.warning(
                        "%s: Still drawing %.1f W %ds after %s. Sending OFF toggle again.",
                        self.entity_id, power, OFF_VERIFY_SECONDS, reason,
                    )
                    await self._send_toggle(f"{reason} retry {attempt + 1}")
            # Still on after the retries: tell the truth instead of showing OFF
            still = HVACMode.FAN_ONLY if self._is_fan(power) else HVACMode.HEAT
            _LOGGER.error(
                "%s: Heater still drawing %.1f W after %d OFF attempts. Showing %s.",
                self.entity_id, power, OFF_VERIFY_RETRIES + 1, still,
            )
            self._sync_mode(still, "off_failed")
        except asyncio.CancelledError:
            pass


    # --- Mode Sync (no IR sent) ---
    def _sync_mode(self, hvac_mode, reason):
        """Change the shown mode to match the real heater, without sending IR."""
        now = datetime.now(timezone.utc)
        self._attr_hvac_mode = hvac_mode
        self.last_mode_change_time = now
        self._last_sync_reason = reason
        self._rest_peak_temp = None
        self._fan_zero_passes = 0
        if hvac_mode in (HVACMode.HEAT, HVACMode.FAN_ONLY):
            if self._heat_start_time is None:
                self._heat_start_time = now
            if hvac_mode == HVACMode.HEAT:
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
                if self._attr_hvac_mode not in (HVACMode.HEAT, HVACMode.FAN_ONLY):
                    _LOGGER.info("%s: HVAC mode is now %s. Stopping monitoring task.", self.entity_id, self._attr_hvac_mode)
                    break
                fan_mode = self._attr_hvac_mode == HVACMode.FAN_ONLY

                now = datetime.now(timezone.utc)
                power = self.current_power_usage
                room = self.current_temperature
                if self._is_heating(power):
                    self._last_heating_seen = now
                    self._rest_peak_temp = None
                elif (room is not None and power is not None and not fan_mode
                      and not self._is_fan(power) and not self._in_settle_window(now)):
                    self._rest_peak_temp = room if self._rest_peak_temp is None else max(self._rest_peak_temp, room)

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

                # --- 1b. Fan only: the fan never rests, ~0 W means it was switched off ---
                if fan_mode:
                    if (power is not None and power < FAN_MIN_WATTS
                            and not self._in_settle_window(now) and not self._stepping_mode()):
                        self._fan_zero_passes += 1
                        if self._fan_zero_passes >= FAN_STOPPED_PASSES:
                            _LOGGER.info("%s: FAN_ONLY but %.1f W for %d checks. Heater is off; syncing to OFF without IR.",
                                         self.entity_id, power, self._fan_zero_passes)
                            self._sync_mode(HVACMode.OFF, "fan_stopped")
                            break
                    else:
                        self._fan_zero_passes = 0
                    await asyncio.sleep(MONITORING_INTERVAL_SECONDS)
                    continue

                # --- 2. Room-cooling Check: switched off outside HA (see ROOM_DROP_OFF) ---
                if (
                    self._rest_peak_temp is not None
                    and room is not None
                    and power is not None
                    and not self._is_heating(power)
                    and not self._is_fan(power)
                    and not self._in_settle_window(now)
                    and room <= self._rest_peak_temp - ROOM_DROP_OFF
                ):
                    _LOGGER.info(
                        "%s: Room %.1f is %.1f below its %.1f peak with no heating: a resting heater "
                        "would have reheated. Heater is off; syncing to OFF without IR.",
                        self.entity_id, room, self._rest_peak_temp - room, self._rest_peak_temp,
                    )
                    self._sync_mode(HVACMode.OFF, "room_cooling")
                    break

                # --- 3. Idle-Off Check (fallback): no heating for a long time ---
                # The heater's own thermostat pauses heating, and the plug reads 0 W
                # then, exactly as when the heater is off. The pauses grow as the room
                # warms: 9, 15, 19, 20, 20 min on 2026-10-28 (bathroom). The old 15 min
                # fired inside them and every re-detection restarted the timer, so the
                # 30-min timer never turned the heater off. Much longer than the
                # longest pause means it was switched off outside HA or lost power.
                idle_minutes = self._idle_off_minutes
                if idle_minutes > 0 and room is not None:
                    # The room rule decides; time alone only when the room stays flat
                    idle_minutes = max(idle_minutes, IDLE_OFF_WITH_ROOM_TEMP_MINUTES)
                if (
                    idle_minutes > 0
                    and self._last_heating_seen is not None
                    and not self._in_settle_window(now)
                    and power is not None
                    and not self._is_heating(power)
                    and not self._is_fan(power)
                    and (now - self._last_heating_seen) >= timedelta(minutes=idle_minutes)
                ):
                    _LOGGER.info(
                        "%s: No heating for %d min (power %.1f W). Heater is off; syncing to OFF without IR.",
                        self.entity_id, idle_minutes, power,
                    )
                    self._sync_mode(HVACMode.OFF, "no_heating")
                    break

                # --- 4. Temperature Maintenance (opt-in) ---
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
            self._rest_peak_temp = None
        if self._attr_hvac_mode != HVACMode.OFF:
            self.async_write_ha_state()  # hvac_action follows the plug

        # Fan only switched on or chosen on the remote: confirm it holds
        if self._is_fan(power):
            if (self._attr_hvac_mode != HVACMode.FAN_ONLY and not self._stepping_mode()
                    and not (self._fan_confirm_task and not self._fan_confirm_task.done())):
                self._fan_confirm_task = self.hass.async_create_task(self._confirm_fan())
        else:
            self._cancel_task("_fan_confirm_task")

        if self._attr_hvac_mode not in (HVACMode.OFF, HVACMode.FAN_ONLY) or not self._is_heating(power):
            return
        if self._stepping_mode():
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
                self._attr_hvac_mode in (HVACMode.OFF, HVACMode.FAN_ONLY)
                and not self._in_settle_window()
                and not self._stepping_mode()
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


    async def async_update(self) -> None:
        """Polled every minute: catch a plug reading whose change event we missed.

        A full restart (10-30) brought fan only back as OFF: the plug reported after
        the entity was set up and no fan check ever started. A steady reading sends no
        new events, so look here as well.
        """
        if self._stepping_mode() or self._in_settle_window():
            return
        power = self.current_power_usage
        if (self._attr_hvac_mode != HVACMode.FAN_ONLY and self._is_fan(power)
                and not (self._fan_confirm_task and not self._fan_confirm_task.done())):
            # OFF or HEAT while the plug sits in the fan band (10-30, downstairs)
            self._fan_confirm_task = self.hass.async_create_task(self._confirm_fan())
        elif (self._attr_hvac_mode == HVACMode.OFF and self._is_heating(power)
                and not (self._confirm_task and not self._confirm_task.done())):
            self._confirm_task = self.hass.async_create_task(self._confirm_auto_on())

    async def _confirm_fan(self):
        """A fan-band reading held FAN_CONFIRM_SECONDS (longer than the OFF tail) is fan only."""
        try:
            await asyncio.sleep(FAN_CONFIRM_SECONDS)
            power = self.current_power_usage
            if (
                self._attr_hvac_mode != HVACMode.FAN_ONLY
                and self._is_fan(power)
                and not self._in_settle_window()
                and not self._stepping_mode()
            ):
                _LOGGER.info("%s: %.1f W held %ds: fan only. Setting mode to FAN_ONLY.",
                             self.entity_id, power, FAN_CONFIRM_SECONDS)
                self._sync_mode(HVACMode.FAN_ONLY, "fan_detected")
        except asyncio.CancelledError:
            pass

    async def _step_mode_to(self, target, wait_first=False):
        """Press the mode button until the plug shows `target`.

        Fan only is the fan band; heat is anything else (it may be resting at 0 W).
        The plug shows a press 18-30 s late, so each press waits MODE_STEP_SECONDS
        before reading; pressing faster would skip past the wanted mode.
        """
        try:
            if wait_first:
                await asyncio.sleep(MODE_STEP_SECONDS)  # the ON toggle lands first
            for press in range(MODE_MAX_PRESSES + 1):
                if self._attr_hvac_mode != target:
                    return
                power = self.current_power_usage
                if power is None:
                    return
                if self._is_fan(power) == (target == HVACMode.FAN_ONLY):
                    _LOGGER.info("%s: Mode is %s (%.1f W) after %d press(es).", self.entity_id, target, power, press)
                    return
                if press == MODE_MAX_PRESSES:
                    _LOGGER.warning("%s: Still %.1f W after %d mode presses; %s not reached.",
                                    self.entity_id, power, press, target)
                    return
                await self._press_mode(f"step to {target} ({press + 1})")
                await asyncio.sleep(MODE_STEP_SECONDS)
        except asyncio.CancelledError:
            pass

    async def _press_mode(self, action_description):
        """Press the mode button (button.press, or scene.turn_on for a scene)."""
        self._last_command_time = datetime.now(timezone.utc)
        domain = self._mode_button.split(".", 1)[0]
        try:
            await self.hass.services.async_call(
                domain=domain,
                service="press" if domain == "button" else "turn_on",
                service_data={"entity_id": self._mode_button},
                blocking=True,
            )
            _LOGGER.debug("%s: Pressed mode button %s: %s", self.entity_id, self._mode_button, action_description)
            return True
        except Exception as e:
            _LOGGER.error("%s: Error pressing mode button %s: %s", self.entity_id, self._mode_button, e)
            return False


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

        # The mode starts OFF; a heater that was in HEAT is resumed below, once the
        # power listener is in place (see _restore_heat).
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
            if last_state is not None and last_state.state == HVACMode.HEAT:
                self._restore_heat(last_state)
            elif (last_state is not None and last_state.state == HVACMode.FAN_ONLY and self._mode_button
                  and (self.current_power_usage is None or self._is_fan(self.current_power_usage))):
                # The fan never rests: still in the fan band means still in fan only. On a
                # full restart the plug often reports after us (10-30: fan_only came back
                # OFF), so no reading yet also resumes; the monitor shows OFF if it then reads ~0 W.
                try:
                    self._heat_start_time = datetime.fromisoformat(last_state.attributes["heat_started"])
                except (KeyError, TypeError, ValueError):
                    pass
                self._sync_mode(HVACMode.FAN_ONLY, "restored")
            elif self._attr_hvac_mode == HVACMode.OFF and self._is_fan(self.current_power_usage):
                self._fan_confirm_task = self.hass.async_create_task(self._confirm_fan())
            # Heater already heating when HA starts: show it (same double check)
            if self._attr_hvac_mode == HVACMode.OFF and self._is_heating(self.current_power_usage):
                self._confirm_task = self.hass.async_create_task(self._confirm_auto_on())
        else:
            _LOGGER.debug("%s: Power sensor not configured, skipping auto-on listener setup.", self.entity_id)


    def _restore_heat(self, last_state) -> None:
        """Resume HEAT after a restart, keeping the timer start and the room peak.

        Before 2026-10-29 every restart showed the heater OFF: a resting heater (0 W)
        stayed OFF until it reheated (up to ~20 min) and the timer then started from
        zero, so each restart could add 20 + 30 min. Now the saved start time and room
        peak come back with the state. If the room already fell ROOM_DROP_OFF below the
        saved peak with no heating, the heater was switched off while HA was down:
        it stays OFF and nothing is sent. A timer that ran out meanwhile fires on the
        first monitor pass.
        """
        attrs = last_state.attributes
        now = datetime.now(timezone.utc)

        def when(key):
            try:
                return datetime.fromisoformat(attrs[key]) if attrs.get(key) else None
            except (TypeError, ValueError):
                return None

        try:
            peak = float(attrs["rest_peak_temperature"]) if attrs.get("rest_peak_temperature") is not None else None
        except (TypeError, ValueError):
            peak = None
        seen = when("last_heating_seen")
        if seen is not None and now - seen > timedelta(minutes=IDLE_OFF_WITH_ROOM_TEMP_MINUTES):
            _LOGGER.info("%s: Was HEAT before restart but no heating since %s. Staying OFF.", self.entity_id, seen)
            return
        power, room = self.current_power_usage, self.current_temperature
        if (peak is not None and room is not None and power is not None
                and not self._is_heating(power) and room <= peak - ROOM_DROP_OFF):
            _LOGGER.info("%s: Room %.1f is %.1f below its %.1f peak since before the restart. "
                         "Switched off while HA was down; staying OFF.", self.entity_id, room, peak - room, peak)
            self._last_sync_reason = "room_cooling"
            return
        self._attr_hvac_mode = HVACMode.HEAT
        self._heat_start_time = when("heat_started") or now
        self._last_heating_seen = seen or now
        self._rest_peak_temp = peak
        self.last_mode_change_time = now
        self._last_sync_reason = "restored"
        _LOGGER.info("%s: Restored HEAT (started %s).", self.entity_id, self._heat_start_time)
        self.hass.async_create_task(self._start_monitoring_task())

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
        self._cancel_task("_mode_task")
        self._cancel_task("_fan_confirm_task")
        await self._stop_monitoring_task()
        # Call super last
        await super().async_will_remove_from_hass()
        _LOGGER.debug("%s: Entity removal complete.", self.entity_id)


    async def _async_options_updated(self, hass: HomeAssistant, entry: ConfigEntry):
        """Handle options update from the UI.

        Update listeners must be coroutines: as a plain @callback HA failed to schedule
        it and every options save answered 500 (the entry still reloaded via __init__).
        """
        _LOGGER.info(
            "%s: Options updated: %s. Monitoring task will use new values on next check.",
             self.entity_id, entry.options
        )
        # Update state if visual representation depends on options (e.g., timer display)
        self.async_write_ha_state()
