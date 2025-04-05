# "My Heater" Custom Climate Integration for Home Assistant

This custom integration provides a Home Assistant `climate` entity to control heaters that are typically operated via an Infrared (IR) remote. It relies on existing Home Assistant entities for physical control and state monitoring:

* **IR Control:** Uses Home Assistant `scene` entities (which you must create) to send IR commands (like On/Off, Temp Up, Temp Down) via a compatible IR blaster device.
* **Power Monitoring:** Uses a smart plug with power monitoring to detect if the heater is physically drawing power. This helps determine the actual state and enables features like auto-on.
* **Temperature Sensing:** Uses a separate temperature sensor entity to read the current room temperature.

**Example Hardware:**

This integration was developed considering hardware like:

* **Power Monitoring Plug:** [Wifi/Zigbee Smart Plug with Power Monitoring](https://he.aliexpress.com/item/1005005481310738.html) (or similar Tuya/Zigbee plugs integrated via ZHA/Zigbee2MQTT)
* **Temperature Sensor & IR Blaster:** [Wifi/Zigbee Thermostat / IR Remote ZHT-006-GB](https://he.aliexpress.com/item/1005003603612061.html) (or similar devices providing temperature sensing and IR remote control capabilities integrated via ZHA/Zigbee2MQTT). *Note: The IR capability of such devices is used via scenes.*

**Example UI:**

<img width="380" alt="image" src="https://github.com/user-attachments/assets/9862eb96-857f-488a-be88-cca5525839cf" />


## Features

* **Standard Climate Interface:** Provides `HVACMode.HEAT` and `HVACMode.OFF` controls. Allows setting a target temperature.
* **Scene-Based Control:** Triggers user-defined Home Assistant scenes to interact with the physical heater via IR.
* **Power Monitoring Awareness:** Uses a power sensor to infer the heater's actual operational state.
* **Auto-On Functionality:** If the power sensor detects significant usage (`> 10` W/kW) while the climate entity is `OFF`, it automatically switches the entity to `HEAT` mode (with a short cooldown to prevent flapping after manual turn-off).
* **Optional Off-Timer:** Automatically triggers the OFF sequence after a configurable duration (set in minutes via integration options, 0 disables).
* **Timer Off Failsafe:** When the timer expires, it activates the OFF scene, waits 60 seconds (`Sleep_After_Power_Off`), checks the power sensor again, and re-activates the OFF scene if power usage is still high.
* **Basic Temperature Maintenance:** While in `HEAT` mode, periodically checks if the current temperature has drifted significantly from the target and if the power state seems wrong (e.g., temp too low but power is off, or temp too high but power is on). If so, it attempts to correct by activating the UP or DOWN temperature scenes.
* **State Restoration:** Remembers and restores the last set target temperature and HVAC mode across Home Assistant restarts.

## Prerequisites

1.  **Hardware:**
    * An IR-controlled Heater.
    * A compatible **Smart Plug with Power Monitoring** already integrated into Home Assistant (e.g., via ZHA, Zigbee2MQTT) providing a `sensor` entity for power usage (in W or kW).
    * A compatible **Temperature Sensor** already integrated into Home Assistant providing a `sensor` entity for the room temperature.
    * A compatible **IR Blaster** (like the Moes device) already integrated into Home Assistant, capable of sending IR commands via a service call (e.g., `remote.send_command`, `climate.send_ir_hvac`, etc.).
2.  **Software:**
    * Home Assistant installation.
    * Working Zigbee integration (ZHA, Zigbee2MQTT, or other) if using Zigbee hardware.
    * The power sensor, temperature sensor, and IR blaster entities must exist and be functional within Home Assistant.
    * **Three crucial Home Assistant `scene` entities must be pre-configured by YOU.** (See Scene Setup below).

## Installation

1.  **Manual:**
    * Copy the entire `my_heater` directory (containing `climate.py`, `manifest.json`, `__init__.py`, `const.py`, etc.) into your Home Assistant's `<config>/custom_components/` directory.
    * Restart Home Assistant.
2.  **HACS (Not currently configured for HACS):**
    * If this component were packaged for HACS, you would typically add it as a custom repository and install it from there.

## Configuration

Configuration is done via the Home Assistant UI:

1.  Go to **Settings -> Devices & Services -> Integrations**.
2.  Click **Add Integration** and search for "My Heater".
3.  Follow the configuration flow, providing the following information:
    * **Heater Name:** A friendly name for this climate entity (e.g., "Living Room Heater").
    * **On/Off Scene:** The `entity_id` of the Home Assistant Scene you created to toggle the heater's power via IR (e.g., `scene.heater_power_toggle`).
    * **Temperature Up Scene:** The `entity_id` of the Scene that sends the "Temperature Up" IR command (e.g., `scene.heater_temp_up`).
    * **Temperature Down Scene:** The `entity_id` of the Scene that sends the "Temperature Down" IR command (e.g., `scene.heater_temp_down`).
    * **Temperature Sensor:** The `entity_id` of your room temperature sensor (e.g., `sensor.living_room_temperature`).
    * **Power Usage Sensor:** The `entity_id` of your smart plug's power sensor (e.g., `sensor.heater_plug_power`).
    * **Min Temperature:** The minimum target temperature allowed (°C).
    * **Max Temperature:** The maximum target temperature allowed (°C).
    * **Default Temperature:** The target temperature to use on first setup or if state restoration fails (°C).
4.  **Options (After Setup):**
    * To set the automatic OFF timer, find the "My Heater" integration card under **Settings -> Devices & Services -> Integrations**.
    * Click **Configure**.
    * Enter the desired **Timer Duration** in minutes (e.g., `120` for 2 hours). Enter `0` to disable the timer.

## Scene Setup (VERY IMPORTANT!)

This integration **does not directly send IR codes**. It relies on **you** creating three specific Home Assistant `scene` entities that perform the actual IR actions using your IR blaster entity. The accuracy of this integration depends entirely on these scenes working correctly.

You need to create these scenes manually via the Home Assistant UI (Settings -> Automations & Scenes -> Scenes -> Create Scene) or YAML:

1.  **On/Off Scene (e.g., `scene.heater_power_toggle`)**
    * This scene should trigger your IR blaster to send the correct IR code to **toggle** the heater's power. Some IR protocols have discrete On/Off, others use a single toggle command. Configure accordingly.
    * *Example Action (using `remote.send_command`):*
        ```yaml
        action:
          - service: remote.send_command
            target:
              entity_id: remote.your_ir_blaster_entity # <--- Your IR blaster entity
            data:
              command: b64:YOUR_IR_CODE_BASE64_HERE # <--- Base64 encoded toggle command
              # Or other parameters like device, command_type based on your remote entity
        ```

2.  **Temperature Up Scene (e.g., `scene.heater_temp_up`)**
    * This scene should trigger your IR blaster to send the "Temperature Up" command.
    * *Example Action:*
        ```yaml
        action:
          - service: remote.send_command
            target:
              entity_id: remote.your_ir_blaster_entity # <--- Your IR blaster entity
            data:
              command: b64:YOUR_TEMP_UP_IR_CODE_BASE64 # <--- Base64 encoded Temp Up command
        ```

3.  **Temperature Down Scene (e.g., `scene.heater_temp_down`)**
    * This scene should trigger your IR blaster to send the "Temperature Down" command.
    * *Example Action:*
        ```yaml
        action:
          - service: remote.send_command
            target:
              entity_id: remote.your_ir_blaster_entity # <--- Your IR blaster entity
            data:
              command: b64:YOUR_TEMP_DOWN_IR_CODE_BASE64 # <--- Base64 encoded Temp Down command
        ```

**Note:** The exact service call (`remote.send_command`, `climate.send_ir_hvac`, etc.) and data parameters depend on how your specific IR blaster is integrated into Home Assistant. You **must** determine the correct service calls and IR codes for your hardware. Use the Home Assistant Developer Tools (Services tab) to test your IR commands before creating the scenes.

## Usage

Once configured, you can add the "My Heater" climate entity to your dashboard. You can:

* Turn the heater On (Heat mode) or Off.
* Adjust the target temperature using the '+' and '-' buttons (or slider if your frontend supports it). This will trigger the Temp Up/Down scenes.
* See the current room temperature (read from your specified sensor).

## Known Issues / Limitations

* **Sync Issues:** If the heater is physically controlled by its original remote without Home Assistant knowing, the state can become out of sync. The Auto-On feature helps detect when it's turned on externally, but detecting external "Off" is only possible via the timer or manual control within HA.
* **Scene Dependency:** The integration's effectiveness is entirely dependent on the correct configuration and reliable execution of the three required Home Assistant scenes. If scene activation fails, the heater won't respond as expected.
* **Basic Temp Maintenance:** The temperature maintenance logic is basic and reactive. It doesn't implement sophisticated PID control. It relies on large deviations and power state checks.

