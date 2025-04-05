# config_flow.py

import voluptuous as vol
from voluptuous import Range
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

# Assuming your const.py defines DOMAIN
from .const import DOMAIN

import logging
_LOGGER = logging.getLogger(__name__)

# --- Options Flow Handler ---
class MyHeaterOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for My Heater."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}

        if user_input is not None:
            # --- Validation for Options ---
            # Timer validation (already exists)
            if user_input.get("timer", 0) < 0:
                 errors["timer"] = "timer_negative" # Use specific field error if possible

            # Add validation for other options here if needed in the future

            if not errors:
                # Input is valid, update the options dictionary for this config entry
                _LOGGER.debug("Updating options for %s: %s", self.config_entry.entry_id, user_input)
                # async_create_entry updates the `.options` attribute of the config_entry
                return self.async_create_entry(title="", data=user_input)

        # --- Define the schema for the options form ---

        # Get current timer value: options -> data -> default (30)
        current_timer = self.config_entry.options.get(
            "timer", self.config_entry.data.get("timer", 30)
        )
        # --- ADDED: Get current remember_last_temp value: options -> data -> default (False) ---
        current_remember_last_temp = self.config_entry.options.get(
            "remember_last_temp", self.config_entry.data.get("remember_last_temp", False)
        )

        # Define the schema using current values as defaults for the form
        options_schema = vol.Schema(
            {
                # Timer field
                vol.Required(
                    "timer",
                    default=current_timer
                ): vol.All(vol.Coerce(int), Range(min=0)),

                # --- ADDED: remember_last_temp field ---
                # Use vol.Optional here consistent with initial setup schema
                vol.Optional(
                    "remember_last_temp",
                    default=current_remember_last_temp
                ): bool,

                # Add other options here in the future
            }
        )

        # Show the options form to the user
        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
            errors=errors,
            # Optional: Add description placeholders for context
            # description_placeholders={
            #     "timer_desc": "Set auto-off timer in minutes (0 to disable).",
            #     "remember_desc": "Restore last set temperature on next HEAT cycle."
            # }
        )


# --- Main Config Flow ---
class MyHeaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for My Heater."""

    VERSION = 1 # Keep version consistent or increment if changing config data structure

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            # --- Validation for Initial Setup ---
            if user_input["min_temp"] >= user_input["max_temp"]:
                 errors["base"] = "min_max_temp_invalid" # Use base for cross-field errors

            # Add other validation as needed

            if not errors:
                # Data is valid, create the config entry
                _LOGGER.debug("Creating new config entry with data: %s", user_input)
                # async_create_entry stores user_input in the `.data` attribute
                return self.async_create_entry(title=user_input["heater_name"], data=user_input)

        # --- Schema for initial setup (Unchanged) ---
        initial_schema = vol.Schema(
             {
                vol.Required("heater_name"): str,
                vol.Required("scene_turn_on_off"): selector({"entity": {"domain": "scene"}}),
                vol.Required("temperature_up_scene"): selector({"entity": {"domain": "scene"}}),
                vol.Required("temperature_down_scene"): selector({"entity": {"domain": "scene"}}),
                vol.Required("temperature_sensor"): selector({"entity": {"domain": "sensor", "device_class": "temperature"}}),
                vol.Required("min_temp", default=16): vol.Coerce(float),
                vol.Required("max_temp", default=30): vol.Coerce(float),
                vol.Required("default_temp", default=20): vol.Coerce(float),
                vol.Required("power_usage"): selector({"entity": {"domain": "sensor"}}),
                vol.Required("timer", default=30): vol.All(vol.Coerce(int), Range(min=0)),
                vol.Optional("remember_last_temp", default=False): bool, # Stays Optional here
            }
        )

        # Show the form to the user for initial setup
        return self.async_show_form(
            step_id="user",
            data_schema=initial_schema,
            errors=errors
            # Add description placeholders if helpful
        )

    # --- Link to the Options Flow (Unchanged) ---
    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> MyHeaterOptionsFlowHandler: # Return instance of our options handler
        """Get the options flow for this handler."""
        return MyHeaterOptionsFlowHandler(config_entry)