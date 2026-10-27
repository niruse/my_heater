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
DEFAULT_IDLE_OFF_MINUTES = 15
