"""A calendar of best-before dates: each day shows what goes off then (what's in storage, not on lists), so it
shows in Home Assistant's Calendar and can be used in automations. Storages kept for only some people are left
out, as everyone can see the calendar."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, KIND_STORAGE, SIGNAL_UPDATE
from .sensor import device_info
from .store import CrowFood


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([DatesCalendar(hass.data[DOMAIN])])


def _fmt(n: float) -> str:
    return f"{n:g}"


class DatesCalendar(CalendarEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Best before dates"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, food: CrowFood) -> None:
        self.food = food
        self._attr_unique_id = f"{DOMAIN}_dates"
        self._attr_device_info = device_info()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self, kind: str) -> None:
        if kind in ("items", "products", "storages", "all", "day"):
            self.async_write_ha_state()

    def _events(self, start: date, end: date) -> list[CalendarEvent]:
        """One event a day for each product (two cartons of milk in the fridge going off on Friday: one event)."""
        food = self.food
        storages = food.data["storages"]
        days: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for it in food.data["items"].values():
            st = storages.get(it.get("storage")) or {}
            day = it.get("expires")
            if st.get("kind") != KIND_STORAGE or st.get("users") or not day or not it.get("product"):
                continue
            if start.isoformat() <= day < end.isoformat():
                days[(day, it["product"])].append(it)
        out = []
        name_of = lambda pid: ((food.data["products"].get(pid) or {}).get("name") or "").lower()  # noqa: E731
        for (day, pid), items in sorted(days.items(), key=lambda kv: (kv[0][0], name_of(kv[0][1]))):
            d = date.fromisoformat(day)
            name = (food.data["products"].get(pid) or {}).get("name") or "Something"
            qty = sum(float(i.get("qty") or 0) for i in items)
            where = sorted({storages[i["storage"]].get("name") or "" for i in items})
            opened = sum(1 for i in items if i.get("opened"))
            desc = [f"Best before {d.day} {d:%B %Y}", "In " + ", ".join(w for w in where if w)]
            if opened:
                desc.append("Opened" if opened == len(items) else f"{opened} opened")
            out.append(CalendarEvent(start=d, end=d + timedelta(days=1),
                                     summary=name + (f" ({_fmt(qty)})" if qty > 1 + 1e-9 else ""),
                                     description="\n".join(x for x in desc if x and x != "In "),
                                     uid=f"{DOMAIN}-{day}-{pid}"))
        return out

    @property
    def event(self) -> CalendarEvent | None:
        """The next thing to go off, from today (what the calendar's state and automations go by)."""
        today = dt_util.now().date()
        ev = self._events(today, today + timedelta(days=400))
        return ev[0] if ev else None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        # all-day events: a day is in when any of it is (the end is exclusive)
        start = dt_util.as_local(start_date).date() if isinstance(start_date, datetime) else start_date
        if isinstance(end_date, datetime):
            end = (dt_util.as_local(end_date) - timedelta(microseconds=1)).date() + timedelta(days=1)
        else:
            end = end_date
        return self._events(start, end)
