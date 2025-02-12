from homeassistant import config_entries
from homeassistant.core import HomeAssistant
import voluptuous as vol
from .const import DOMAIN, _LOGGER

class RefossConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Refoss."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the user configuration step."""
        if user_input is not None:
            return self.async_create_entry(title="Refoss Settings", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("user_reset_day", default=24): int,
                vol.Required("device_reset_day", default=1): int,
            }),
        )
