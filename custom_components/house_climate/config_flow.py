"""Room-based setup and options, stored in Home Assistant's config entry."""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import probatio as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .configuration import entry_configuration, load_configuration, validate_actuators
from .const import DOMAIN
from .controller.config import Config, Settings
from .controller.sensors import from_f, to_f

PROFILE_FIELDS = ("target_f", "half_band_f", "priority", "comfort_enabled", "fan_percentage", "low_f", "high_f")


class RoomEditor:
    """A private draft: cancelling any step leaves the running config untouched."""

    def _begin(self, configuration=None, live=False):
        self.draft = deepcopy(configuration) if configuration else {
            "schema_version": 2, "timezone": self.hass.config.time_zone,
            "heads": {}, "rooms": {}, "helper_prefix": "house_climate",
            "low_f": 66, "high_f": 75,
        }
        self.live = live
        self.room_id = None
        self.schedule_index = None
        self.fan_index = None

    @property
    def unit(self):
        return self.hass.config.units.temperature_unit

    def temperature(self, value):
        return round(from_f(value, self.unit), 3)

    def delta(self, value):
        return round(value * (5 / 9 if self.unit == "°C" else 1), 3)

    def number(self, minimum, maximum, step=.5, unit=None):
        return selector.NumberSelector(selector.NumberSelectorConfig(
            min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX,
            **({"unit_of_measurement": unit} if unit else {})))

    def choice(self, choices):
        return selector.SelectSelector(selector.SelectSelectorConfig(
            options=[{"value": str(key), "label": label} for key, label in choices],
            mode=selector.SelectSelectorMode.DROPDOWN))

    def room_choices(self, exclude=None):
        return [(key, value["name"]) for key, value in self.draft["rooms"].items() if key != exclude]

    def form(self, step, schema, errors=None, detail=None):
        return self.async_show_form(
            step_id=step, data_schema=vol.Schema(schema), errors=errors or {},
            description_placeholders={"detail": str(detail or "")})

    def profile_schema(self, data):
        result = {
            vol.Required("comfort_enabled", default=data.get("comfort_enabled", True)): bool,
            vol.Required("target_f", default=self.temperature(data.get("target_f", 70) + data.get("target_offset_f", 0))):
                self.number(self.temperature(self.draft.get("low_f", 66)), self.temperature(self.draft.get("high_f", 75)), .1, self.unit),
            vol.Required("half_band_f", default=self.delta(data.get("half_band_f", .5))):
                self.number(.1, self.delta(5), .1, self.unit),
            vol.Required("priority", default=data.get("priority", 1)): self.number(.1, 10, .1),
            vol.Required("fan_percentage", default=data.get("fan_percentage", 0)): self.number(0, 100, 1, "%"),
        }
        for name in ("low_f", "high_f"):
            marker = vol.Optional(name, default=self.temperature(data[name])) if data.get(name) is not None else vol.Optional(name)
            result[marker] = self.number(self.temperature(self.draft.get("low_f", 66)),
                                        self.temperature(self.draft.get("high_f", 75)), .1, self.unit)
        return result

    def profile_input(self, values):
        result = {key: values[key] for key in PROFILE_FIELDS if key in values}
        for key in ("target_f", "low_f", "high_f"):
            if key in result:
                result[key] = to_f(float(result[key]), self.unit)
        result["half_band_f"] = float(result["half_band_f"]) * (9 / 5 if self.unit == "°C" else 1)
        return result

    def climate_selector(self):
        registry = er.async_get(self.hass)
        excluded = [entry.entity_id for entry in registry.entities.values()
                    if entry.domain == "climate" and entry.platform == DOMAIN]
        return selector.EntitySelector(selector.EntitySelectorConfig(
            domain="climate", exclude_entities=excluded))

    def validate(self, candidate, live=False):
        cfg = Config.from_dict(candidate)
        cfg.validate(live=live)
        validate_actuators(self.hass, cfg)
        # HA's device registry identifies aliases and the auxiliary heater's own
        # probes/fan entity. Never give that physical device two independent owners.
        registry = er.async_get(self.hass)
        heater = registry.async_get(cfg.heater_entity) if cfg.heater_entity else None
        if heater and heater.device_id:
            owned = [*cfg.fans.values(), *(p.entity_id for p in cfg.zones[cfg.auxiliary_room].probes)]
            if any((entry := registry.async_get(entity)) and entry.device_id == heater.device_id for entity in owned):
                raise ValueError("The auxiliary heater must use external probes and its climate entity alone")
        return cfg

    def normalized(self, candidate):
        result = deepcopy(candidate)
        used = {room["head"] for room in result["rooms"].values()}
        result["heads"] = {key: entity for key, entity in result["heads"].items() if key in used}
        return result

    async def async_step_menu(self, user_input=None):
        return self.async_show_menu(step_id="menu", menu_options=[
            "add_room", "select_room", "system", "auxiliary", "import_file", "finish"])

    async def async_step_add_room(self, user_input=None):
        self.room_id = None
        return await self.async_step_room()

    async def async_step_select_room(self, user_input=None):
        if not self.draft["rooms"]:
            return await self.async_step_add_room()
        if user_input is not None:
            self.room_id = user_input["room"]
            return await self.async_step_room_menu()
        return self.form("select_room", {vol.Required("room"): self.choice(self.room_choices())})

    async def async_step_room_menu(self, user_input=None):
        return self.async_show_menu(step_id="room_menu", menu_options=[
            "room", "add_schedule", "select_schedule", "add_fan", "select_fan", "remove_room", "menu"],
            description_placeholders={"room": self.draft["rooms"][self.room_id]["name"]})

    async def async_step_room(self, user_input=None):
        room = self.draft["rooms"].get(self.room_id, {})
        errors, detail = {}, None
        if user_input is not None:
            candidate = deepcopy(self.draft)
            key = self.room_id or uuid4().hex
            entity = user_input["head"]
            head = next((k for k, v in candidate["heads"].items() if v == entity), entity)
            candidate["heads"][head] = entity
            registry = er.async_get(self.hass)
            old_probes = {p["entity_id"]: p for p in room.get("sensors", [])}
            probes = []
            for entity_id in user_input["sensors"]:
                entry = registry.async_get(entity_id)
                probes.append(old_probes.get(entity_id, {
                    "entity_id": entity_id, "source": entry.device_id if entry and entry.device_id else entity_id}))
            candidate["rooms"][key] = {
                **room, "name": user_input["name"].strip(), "head": head, "sensors": probes,
                "profile": self.profile_input(user_input),
            }
            candidate["rooms"][key].pop("area_id", None)
            if user_input.get("area"):
                candidate["rooms"][key]["area_id"] = user_input["area"]
            candidate = self.normalized(candidate)
            try:
                if not candidate["rooms"][key]["name"]:
                    raise ValueError("Give the room a name")
                self.validate(candidate)
            except (ValueError, KeyError, TypeError) as exc:
                errors, detail = {"base": "invalid_config"}, exc
            else:
                self.draft, self.room_id = candidate, key
                return await self.async_step_room_menu()
        schema = {
            vol.Required("name", default=room.get("name", "")): str,
            vol.Optional("area", **({"default": room["area_id"]} if room.get("area_id") else {})): selector.AreaSelector(),
            vol.Required("head", **({"default": self.draft["heads"][room["head"]]} if room else {})):
                self.climate_selector(),
            vol.Required("sensors", default=[p["entity_id"] for p in room.get("sensors", [])]):
                selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature", multiple=True)),
            **self.profile_schema(room.get("profile", {})),
        }
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(vol.Schema(schema), user_input).schema
        return self.form("room", schema, errors, detail)

    async def async_step_remove_room(self, user_input=None):
        if user_input is not None and user_input.get("confirm"):
            candidate = deepcopy(self.draft)
            del candidate["rooms"][self.room_id]
            candidate = self.normalized(candidate)
            try:
                self.validate(candidate)
            except (ValueError, KeyError, TypeError) as exc:
                return self.form("remove_room", {vol.Required("confirm", default=False): bool},
                                 {"base": "room_in_use"}, exc)
            self.draft, self.room_id = candidate, None
            return await self.async_step_menu()
        return self.form("remove_room", {vol.Required("confirm", default=False): bool})

    async def async_step_add_schedule(self, user_input=None):
        self.schedule_index = None
        return await self.async_step_schedule()

    async def async_step_select_schedule(self, user_input=None):
        schedules = self.draft["rooms"][self.room_id].get("schedules", [])
        if not schedules:
            return await self.async_step_add_schedule()
        if user_input is not None:
            self.schedule_index = int(user_input["schedule"])
            return await self.async_step_schedule()
        return self.form("select_schedule", {vol.Required("schedule"): self.choice([
            (i, p.get("name") or f'{p["start"]}–{p["end"]}') for i, p in enumerate(schedules)])})

    async def async_step_schedule(self, user_input=None):
        room = self.draft["rooms"][self.room_id]
        period = room["schedules"][self.schedule_index] if self.schedule_index is not None else room["profile"]
        errors, detail = {}, None
        if user_input is not None:
            candidate = deepcopy(self.draft)
            periods = candidate["rooms"][self.room_id].setdefault("schedules", [])
            value = {"name": user_input["name"], "start": user_input["start"], "end": user_input["end"],
                     **self.profile_input(user_input)}
            if self.schedule_index is None:
                periods.append(value)
            elif user_input.get("remove"):
                periods.pop(self.schedule_index)
            else:
                periods[self.schedule_index] = value
            try:
                self.validate(candidate)
            except (ValueError, KeyError, TypeError) as exc:
                errors, detail = {"base": "invalid_config"}, exc
            else:
                self.draft = candidate
                return await self.async_step_room_menu()
        return self.form("schedule", {
            vol.Required("name", default=period.get("name", "")): str,
            vol.Required("start", default=period.get("start", "22:00:00")): selector.TimeSelector(),
            vol.Required("end", default=period.get("end", "05:00:00")): selector.TimeSelector(),
            **self.profile_schema(period),
            vol.Required("remove", default=False): bool,
        }, errors, detail)

    async def async_step_add_fan(self, user_input=None):
        self.fan_index = None
        return await self.async_step_fan()

    async def async_step_select_fan(self, user_input=None):
        fans = self.draft["rooms"][self.room_id].get("fans", [])
        if not fans:
            return await self.async_step_add_fan()
        if user_input is not None:
            self.fan_index = int(user_input["fan"])
            return await self.async_step_fan()
        return self.form("select_fan", {vol.Required("fan"): self.choice([
            (i, p["entity_id"]) for i, p in enumerate(fans)])})

    async def async_step_fan(self, user_input=None):
        fans = self.draft["rooms"][self.room_id].get("fans", [])
        fan = fans[self.fan_index] if self.fan_index is not None else {}
        errors, detail = {}, None
        if user_input is not None:
            candidate = deepcopy(self.draft)
            rules = candidate["rooms"][self.room_id].setdefault("fans", [])
            value = {k: user_input[k] for k in ("entity_id", "strategy", "percentage", "source_room") if k in user_input}
            if value.get("strategy") != "transfer":
                value.pop("source_room", None)
            if self.fan_index is None:
                rules.append(value)
            elif user_input.get("remove"):
                rules.pop(self.fan_index)
            else:
                rules[self.fan_index] = value
            try:
                self.validate(candidate)
            except (ValueError, KeyError, TypeError) as exc:
                errors, detail = {"base": "invalid_config"}, exc
            else:
                self.draft = candidate
                return await self.async_step_room_menu()
        schema = {
            vol.Required("entity_id", **({"default": fan["entity_id"]} if fan else {})):
                selector.EntitySelector(selector.EntitySelectorConfig(domain="fan")),
            vol.Required("strategy", default=fan.get("strategy", "cooling")): self.choice([
                ("cooling", "During cooling demand"), ("conditioning", "During heating or cooling demand"),
                ("schedule", "Use the room profile's fan speed"), ("transfer", "Move useful air from another room"),
                ("always", "Continuous circulation")]),
            vol.Required("percentage", default=fan.get("percentage", 50)): self.number(0, 100, 1, "%"),
            vol.Required("remove", default=False): bool,
        }
        if choices := self.room_choices(exclude=self.room_id):
            schema[vol.Optional("source_room", **({"default": fan["source_room"]} if fan.get("source_room") else {}))] = self.choice(choices)
        return self.form("fan", schema, errors, detail)

    async def async_step_system(self, user_input=None):
        errors, detail = {}, None
        if user_input is not None:
            candidate = deepcopy(self.draft)
            candidate.update({key: value for key, value in user_input.items() if key != "live"})
            for key in ("low_f", "high_f"):
                candidate[key] = to_f(float(candidate[key]), self.unit)
            try:
                self.validate(candidate, live=user_input["live"])
            except (ValueError, KeyError, TypeError) as exc:
                errors, detail = {"base": "invalid_config"}, exc
            else:
                self.draft, self.live = candidate, user_input["live"]
                return await self.async_step_menu()
        return self.form("system", {
            vol.Required("low_f", default=self.temperature(self.draft.get("low_f", 66))): self.number(self.temperature(40), self.temperature(85), .1, self.unit),
            vol.Required("high_f", default=self.temperature(self.draft.get("high_f", 75))): self.number(self.temperature(45), self.temperature(95), .1, self.unit),
            vol.Required("off_settle_seconds", default=self.draft.get("off_settle_seconds", 180)): self.number(1, 3600, 1, "s"),
            vol.Required("mode_hold_seconds", default=self.draft.get("mode_hold_seconds", 1200)): self.number(1, 7200, 1, "s"),
            vol.Required("decision_interval_seconds", default=self.draft.get("decision_interval_seconds", 90)): self.number(60, 120, 1, "s"),
            vol.Required("head_minimum_run_seconds", default=self.draft.get("head_minimum_run_seconds", 300)): self.number(0, 3600, 1, "s"),
            vol.Required("head_minimum_off_seconds", default=self.draft.get("head_minimum_off_seconds", 180)): self.number(0, 3600, 1, "s"),
            vol.Required("poll_seconds", default=self.draft.get("poll_seconds", 15)): self.number(1, 30, 1, "s"),
            vol.Required("equipment_timing_confirmed", default=self.draft.get("equipment_timing_confirmed", False)): bool,
            vol.Required("live", default=self.live): bool,
        }, errors, detail)

    async def async_step_auxiliary(self, user_input=None):
        errors, detail = {}, None
        if user_input is not None:
            candidate = deepcopy(self.draft)
            for key in ("heater_entity", "auxiliary_room"):
                candidate.pop(key, None)
            if user_input["enabled"]:
                for key in ("heater_entity", "auxiliary_room"):
                    if key in user_input:
                        candidate[key] = user_input[key]
            candidate["auxiliary_fan_enabled"] = user_input["auxiliary_fan_enabled"]
            candidate["heater_cutoff_f"] = to_f(float(user_input["heater_cutoff_f"]), self.unit)
            candidate["heater_high_setpoint_f"] = to_f(float(user_input["heater_high_setpoint_f"]), self.unit)
            candidate["heater_setpoint_confirmed"] = user_input["heater_setpoint_confirmed"]
            try:
                if user_input["enabled"] and not all(candidate.get(k) for k in ("heater_entity", "auxiliary_room")):
                    raise ValueError("Choose the auxiliary heater and its room")
                self.validate(candidate)
            except (ValueError, KeyError, TypeError) as exc:
                errors, detail = {"base": "invalid_config"}, exc
            else:
                self.draft = candidate
                return await self.async_step_menu()
        schema = {
            vol.Required("enabled", default=bool(self.draft.get("heater_entity"))): bool,
            vol.Optional("heater_entity", **({"default": self.draft["heater_entity"]} if self.draft.get("heater_entity") else {})):
                self.climate_selector(),
            vol.Required("heater_high_setpoint_f", default=self.temperature(self.draft.get("heater_high_setpoint_f", 86))):
                self.number(self.temperature(70), self.temperature(110), .1, self.unit),
            vol.Required("heater_cutoff_f", default=self.temperature(self.draft.get("heater_cutoff_f", 70))):
                self.number(self.temperature(45), self.temperature(95), .1, self.unit),
            vol.Required("auxiliary_fan_enabled", default=self.draft.get("auxiliary_fan_enabled", True)): bool,
            vol.Required("heater_setpoint_confirmed", default=self.draft.get("heater_setpoint_confirmed", False)): bool,
        }
        if choices := self.room_choices():
            schema[vol.Optional("auxiliary_room", **({"default": self.draft["auxiliary_room"]} if self.draft.get("auxiliary_room") else {}))] = self.choice(choices)
        return self.form("auxiliary", schema, errors, detail)

    async def async_step_import_file(self, user_input=None):
        errors, detail = {}, None
        if user_input is not None:
            try:
                cfg = await self.hass.async_add_executor_job(
                    load_configuration, self.hass.config.config_dir, user_input["config_file"], False)
                states = {s.entity_id: {"state": s.state, "attributes": dict(s.attributes)} for s in self.hass.states.async_all()}
                configuration = cfg.to_dict(Settings.read(states, cfg))
                self.validate(configuration)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors, detail = {"base": "invalid_config"}, exc
            else:
                self._begin(configuration, live=False)
                return await self.async_step_menu()
        return self.form("import_file", {vol.Required("config_file", default="house_climate.toml"): str}, errors, detail)

    async def async_step_finish(self, user_input=None):
        errors, detail = {}, None
        try:
            cfg = self.validate(self.draft, self.live)
        except (ValueError, KeyError, TypeError) as exc:
            errors, detail = {"base": "invalid_config"}, exc
        else:
            if user_input is not None:
                return self.async_create_entry(title="House Climate", data={
                    "configuration": cfg.to_dict(), "live": self.live})
            detail = f"{len(cfg.zones)} rooms, {len(cfg.heads)} shared-system heads; " + ("live mode" if self.live else "shadow mode")
        return self.form("finish", {}, errors, detail)


class HouseClimateConfigFlow(RoomEditor, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        self._begin()
        return await self.async_step_menu()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HouseClimateOptionsFlow()


class HouseClimateOptionsFlow(RoomEditor, config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        values = {**self.config_entry.data, **self.config_entry.options}
        try:
            cfg = await self.hass.async_add_executor_job(
                entry_configuration, self.hass.config.config_dir, {**values, "live": False})
            states = {s.entity_id: {"state": s.state, "attributes": dict(s.attributes)} for s in self.hass.states.async_all()}
            configuration = cfg.to_dict(Settings.read(states, cfg))
        except (OSError, ValueError, KeyError, TypeError):
            self._begin()
            return await self.async_step_import_file()
        self._begin(configuration, live=False if cfg.legacy else values.get("live", False))
        return await self.async_step_menu()
