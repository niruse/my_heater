DOMAIN = "my_heater"
PLATFORMS = ["climate"]

# Options
CONF_POWER_THRESHOLD = "power_threshold"
CONF_IDLE_OFF_MINUTES = "idle_off_minutes"
CONF_TEMP_MAINTENANCE = "temperature_maintenance"

# Watts that prove the heating element is on. Must sit above the ~21 W
# wind-down tail the heater draws for 20-35 s after it switches off.
DEFAULT_POWER_THRESHOLD = 100
# Minutes in HEAT with no heating before the heater is treated as switched off.
DEFAULT_IDLE_OFF_MINUTES = 45   # thermostat rests read 0 W, same as off; seen up to 20 min
# Optional button/scene that steps the heater's mode (low heat -> high heat -> fan
# only -> low heat on the parents heater). Setting it adds the FAN_ONLY mode.
CONF_MODE_BUTTON = "mode_button"
