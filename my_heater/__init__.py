# __init__.py

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

# Assuming your const.py defines DOMAIN and PLATFORMS (likely ["climate"])
from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

# --- Main Setup Function ---
async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the My Heater component."""
    # This is usually empty for config flow integrations
    hass.data.setdefault(DOMAIN, {})
    _LOGGER.debug("Async_setup called for My Heater")
    return True

# --- Setup Entry from Config Flow ---
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up My Heater from a config entry."""
    _LOGGER.debug("Setting up config entry: %s", entry.entry_id)
    hass.data[DOMAIN][entry.entry_id] = entry.data # Store data if needed centrally

    # --- Register the update listener ---
    # This listener will be called when options are updated
    entry.async_on_unload(entry.add_update_listener(async_update_options_listener))

    # Forward the setup to the climate platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True

# --- Unload Entry ---
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading config entry: %s", entry.entry_id)

    # Forward the unload to platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Clean up hass.data if you stored anything there for this entry
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.debug("Successfully unloaded entry: %s", entry.entry_id)

    return unload_ok

# --- Options Update Listener ---
async def async_update_options_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    # This function is called when the user saves changes in the options flow.
    # The most common action is to reload the config entry to apply the changes.
    _LOGGER.info("Options updated for %s, reloading integration.", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)