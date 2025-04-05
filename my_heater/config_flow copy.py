from homeassistant.core import callback
from homeassistant import config_entries
import voluptuous as vol
from homeassistant.helpers.selector import selector  # Import the selector

from .const import DOMAIN

class MyHeaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for My Heater."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is not None:
            # Save the data and create an entry
            return self.async_create_entry(title=user_input["heater_name"], data=user_input)

        # Define the schema with a dropdown for scenes
        schema = vol.Schema(
            {
                vol.Required("heater_name"): str,  # Free text input for heater name
                vol.Required("scene_turn_on_off"): selector({"entity": {"domain": "scene"}}),  # Dropdown for selecting scenes
                vol.Required("temperature_up_scene"): selector({"entity": {"domain": "scene"}}),
                vol.Required("temperature_down_scene"): selector({"entity": {"domain": "scene"}}),                
                vol.Required("temperature_sensor"): selector({"entity": {"domain": "sensor"}}),  # Dropdown for selecting temperature sensor
                vol.Required("min_temp", default=16): vol.All(vol.Coerce(float)),  # Minimum temperature (float)
                vol.Required("max_temp", default=30): vol.All(vol.Coerce(float)),  # Maximum temperature (float)
                vol.Required("default_temp", default=30): vol.All(vol.Coerce(float)),  # Maximum temperature (float)
                vol.Required("voltage_usage"): selector({"entity": {"domain": "sensor"}}),  # New sensor
            }
        )

        # Show the form to the user
        return self.async_show_form(step_id="user", data_schema=schema)
