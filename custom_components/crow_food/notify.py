"""Phone notifications (Home Assistant Companion app): a daily note about food that's expiring,
expired or running low, sent to the people chosen in the card editor.

Each Companion app install is a mobile_app config entry that records the user who signed in on it,
which is how a note for a person reaches only their phones.

Notes carry their own buttons (shown when you press and hold on an iPhone, or under the note on Android):
* Add Something to Shopping… - type something into the note and it goes on the Shopping list (every note;
  called Add Something Else… on a note with its own add button)
* Freeze them - moves what should be frozen today into the freezer (the daily note, when there's something)
* Meal ideas - opens Use It Up on the card (the daily note, when Use It Up is on and things need using)
"""
from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import Event, HomeAssistant
from homeassistant.util import slugify

from .const import DOMAIN, NAME
from .store import CrowFood

_LOGGER = logging.getLogger(__name__)

ACTION_ADD = "CROW_FOOD_ADD"
ACTION_FREEZE = "CROW_FOOD_FREEZE"
ACTION_ADD_LOW = "CROW_FOOD_ADD_LOW"
ACTION_ADD_LOW_DAILY = "CROW_FOOD_ADD_LOW_DAILY"


def low_unlisted(food: CrowFood) -> list[dict]:
    """Running low and not on a shopping list yet (what's on one already isn't said again), with how many to buy."""
    import math
    from .const import PURPOSE_BUY
    listed: dict[str, float] = {}
    for it in food.data["items"].values():
        if it.get("product") and not it.get("bought") and food.purpose(it.get("storage")) == PURPOSE_BUY \
                and (food.data["storages"].get(it.get("storage")) or {}).get("kind") != "storage":
            listed[it["product"]] = listed.get(it["product"], 0) + float(it.get("qty") or 0)
    out = []
    for x in food.smart(None)["low"]:
        need = max(1, math.ceil(float(x["min"]) - float(x["stock"]) - 1e-9))
        if listed.get(x["product"], 0) < need - 1e-9:
            out.append({**x, "need": need - math.floor(listed.get(x["product"], 0))})
    return out


def mobile_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    out = []
    for entry in hass.config_entries.async_entries("mobile_app"):
        data = entry.data
        name = data.get("device_name") or entry.title or ""
        service = f"mobile_app_{slugify(name)}"
        os_name = str(data.get("os_name") or "").lower()
        platform = "ios" if os_name in ("ios", "ipados", "macos", "watchos", "visionos") else "android"
        out.append({"service": service, "name": name, "user_id": data.get("user_id"), "platform": platform,
                    "available": hass.services.has_service("notify", service)})
    return out


def dashboard_path(food: CrowFood, uid: str) -> str:
    """The dashboard chosen in the editor, or - when it's left on Automatic - wherever this person last
    opened the card."""
    chosen = str(food.settings.get("notify", {}).get("path") or "").split("?")[0].strip().strip("/")
    if chosen:
        return f"/{chosen}"
    return food.data.get("auto_paths", {}).get(uid) or "/lovelace/0"


def _shopping(food: CrowFood) -> dict | None:
    return next((s for s in food.data["storages"].values() if s.get("builtin") == "shopping"), None)


def add_action(food: CrowFood, other: bool = False) -> dict | None:
    """Type something into the note and it goes on the Shopping list ("Add Something Else…" on a note that has
    its own add button, so the two can't be confused)."""
    shop = _shopping(food)
    if not shop:
        return None
    title = "Add Something Else…" if other else f"Add Something to {shop.get('name') or 'Shopping'}…"
    return {"action": ACTION_ADD, "title": title, "behavior": "textInput",
            "textInputButtonTitle": "Add", "textInputPlaceholder": "Milk, bread"}


async def async_send(hass: HomeAssistant, uids: list[str], title: str, message: str, tag: str,
                     food: CrowFood | None = None, open_view: str | None = None, actions: list[dict] | None = None) -> int:
    sent = 0
    for d in mobile_devices(hass):
        if d["user_id"] not in uids or not d["available"]:
            continue
        data: dict[str, Any] = {"tag": f"{DOMAIN}_{tag}", "group": DOMAIN}
        if food:
            # tapping the note opens the chosen dashboard (or this person's own), on the card's page when there is one
            link = dashboard_path(food, d['user_id']) + (f"?crow_food={open_view}" if open_view else "")
            data["url"] = link
            data["clickAction"] = link
        if actions:
            # buttons; one that opens a page of the card ("open") gets this person's dashboard
            out = []
            for a in actions[:3]:
                a = dict(a)
                if food and a.get("open"):
                    a = {"action": "URI", "title": a["title"], "uri": f"{dashboard_path(food, d['user_id'])}?crow_food={a.pop('open')}"}
                out.append(a)
            data["actions"] = out
        if d["platform"] == "ios":
            data["push"] = {"interruption-level": "active", "thread-id": DOMAIN}
        else:
            data["channel"] = NAME
        try:
            await hass.services.async_call("notify", d["service"], {"title": title, "message": message, "data": data},
                                           blocking=False)
            sent += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Crow Food couldn't notify %s: %s", d["service"], err)
    return sent


def _names(food: CrowFood, items: list[dict], limit: int = 4) -> str:
    names = []
    for it in items:
        n = food.name_of(it)
        if n not in names:
            names.append(n)
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def daily_message(food: CrowFood) -> tuple[str, str] | None:
    """The day's note, or None when there's nothing to say."""
    opts = food.settings.get("notify", {})
    smart = food.smart(None)
    lines = []
    if opts.get("expired", True) and smart["expired"]:
        n = len(smart["expired"])
        lines.append(f"{n} {'thing has' if n == 1 else 'things have'} expired: {_names(food, smart['expired'])}.")
    if opts.get("expiring", True) and smart["soon"]:
        n = len(smart["soon"])
        lines.append(f"{n} {'thing expires' if n == 1 else 'things expire'} soon: {_names(food, smart['soon'])}.")
    if opts.get("freeze", True):
        fz = food.freeze_today()
        if fz:
            lines.append(f"Freeze today? {_names(food, fz)} {'goes' if len(fz) == 1 else 'go'} off soon and freeze{'s' if len(fz) == 1 else ''} well.")
    low = low_unlisted(food) if opts.get("low_stock") else []
    if low:
        names = [x["name"] for x in low]
        more = f" and {len(names) - 4} more" if len(names) > 4 else ""
        lines.append(f"Running low, not on a list yet: {', '.join(names[:4])}{more}.")
    if not lines:
        return None
    title = "Use these soon" if (smart["soon"] or smart["expired"]) and (opts.get("expiring", True) or opts.get("expired", True)) else NAME
    return title, " ".join(lines)


async def async_daily(hass: HomeAssistant, food: CrowFood) -> int:
    uids = food.settings.get("notify", {}).get("users") or []
    if not uids:
        return 0
    msg = daily_message(food)
    if not msg:
        return 0
    return await async_send(hass, uids, msg[0], msg[1], "daily", food, daily_view(food), daily_actions(food))


def daily_view(food: CrowFood) -> str:
    """The page tapping the daily note opens: Use Soon, Gone Off or Running Low, whichever it talks about first."""
    opts = food.settings.get("notify", {})
    smart = food.smart(None)
    if opts.get("expiring", True) and smart["soon"]:
        return "smart:soon"
    if opts.get("expired", True) and smart["expired"]:
        return "smart:expired"
    if opts.get("low_stock") and low_unlisted(food):
        return "smart:low"
    return "smart:soon"


def daily_actions(food: CrowFood) -> list[dict]:
    """The daily note's buttons: Freeze them (when something should be frozen today), Meal ideas (when Use It Up
    is on and things need using), Add All N Low to Shopping and Add Something Else…"""
    from .ai import feature_on
    opts = food.settings.get("notify", {})
    out: list[dict] = []
    if opts.get("freeze", True) and food.freeze_today():
        # it changes what's at home, so an iPhone asks to be unlocked first
        out.append({"action": ACTION_FREEZE, "title": "Freeze them", "authenticationRequired": True})
    smart = food.smart(None)
    if feature_on(food, "use_up") and (smart["soon"] or smart["expired"]):
        out.append({"open": "useup", "title": "Meal ideas"})
    # what's running low and not on a list: onto Shopping in one go
    low = low_unlisted(food) if opts.get("low_stock") and _shopping(food) else []
    if low:
        name = (_shopping(food) or {}).get("name") or "Shopping"
        out.append({"action": ACTION_ADD_LOW_DAILY, "title": f"Add {'the Low One' if len(low) == 1 else f'All {len(low)} Low'} to {name}", "authenticationRequired": True})
    add = add_action(food, other=bool(low))
    if add and len(out) < 3:
        out.append(add)
    return out


def _split_names(text: str) -> list[str]:
    """ "milk, bread and 2 onions" -> the three things (a number at the front is left on the name) """
    parts = re.split(r"\s*(?:,|;|\n|\band\b|&|\+)\s*", str(text or ""), flags=re.I)
    return [p.strip(" .") for p in parts if p.strip(" .")][:10]


def _list_words(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


async def async_action(hass: HomeAssistant, food: CrowFood, event: Event) -> None:
    """A button pressed on one of Crow Food's notes (the Companion app sends it as an event)."""
    action = event.data.get("action")
    if action not in (ACTION_ADD, ACTION_FREEZE, ACTION_ADD_LOW, ACTION_ADD_LOW_DAILY):
        return
    users = food.settings.get("notify", {}).get("users") or []
    # the phone's own person (the Companion app sends its button presses as them); otherwise whoever gets the notes
    uid = event.context.user_id if event.context and event.context.user_id else (users[0] if users else None)
    if not uid:
        return
    if action in (ACTION_ADD_LOW, ACTION_ADD_LOW_DAILY):
        await _add_low(hass, food, uid, soon=action == ACTION_ADD_LOW)
        return
    if action == ACTION_ADD:
        names = _split_names(event.data.get("reply_text") or event.data.get("textInput") or "")
        if not names:
            return
        from .assist import add_to_list
        shop = _shopping(food)
        added, list_name = [], (shop or {}).get("name")
        for n in names:
            r = await add_to_list(food, uid, n, list_name, 1)
            if r.get("added"):
                added.append(r["added"])
                list_name = r.get("list") or list_name
        if added:
            await async_send(hass, [uid], f"Added to {list_name}", f"{_list_words(added)} {'is' if len(added) == 1 else 'are'} on the {list_name} list.",
                             "added", food, f"storage:{shop['id']}" if shop else None, [a for a in [add_action(food)] if a])
        return
    # Freeze them: what should be frozen today, into the first freezer
    items = food.freeze_today(uid)
    vis = food.visible_storages(uid)
    freezers = sorted((s for s in vis.values() if s.get("kind") == "storage" and food._is_freezer(s["id"])), key=lambda s: s.get("sort", 0))
    if not items or not freezers:
        await async_send(hass, [uid], NAME, "Nothing needs freezing now.", "frozen", food)
        return
    target, moved = freezers[0], []
    for it in items:
        try:
            await food.async_move(uid, it["id"], target["id"])
            moved.append(food.name_of(it))
        except Exception as err:  # noqa: BLE001 - changed or used since the note
            _LOGGER.debug("Crow Food couldn't freeze %s: %s", it.get("id"), err)
    if moved:
        names = list(dict.fromkeys(moved))
        await async_send(hass, [uid], "Frozen", f"{_list_words(names)} {'is' if len(names) == 1 else 'are'} in the {target['name']} now.",
                         "frozen", food, f"storage:{target['id']}")


def _week(food: CrowFood, uid: str) -> dict:
    from .reports import _Ctx, r_week
    return r_week(_Ctx(food, uid, None))


def weekly_message(food: CrowFood, w: dict) -> tuple[str, str] | None:
    """This Week's note, or None when there's nothing to say."""
    low, soon = w["low"], w["soon"]
    if not low and not soon:
        return None
    bits = []
    if low:
        bits.append(f"{len(low)} running low")
    if soon:
        bits.append(f"{len(soon)} running out this week")
    names = [r["name"] for r in low + soon]
    shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    msg = f"{' and '.join(bits).capitalize()}, not on a shopping list yet: {shown}."
    if w.get("listed", {}).get("count"):
        n = w["listed"]["count"]
        msg += f" {n} {'thing is' if n == 1 else 'things are'} on the lists already."
    return "This week", msg


async def async_weekly(hass: HomeAssistant, food: CrowFood, uids: list[str] | None = None, always: bool = False) -> int:
    """This Week to the people chosen for it (each sees only their own storages). always: say so when there's
    nothing (Send Now, so it can be seen to work)."""
    uids = uids if uids is not None else ((food.settings.get("notify", {}).get("weekly") or {}).get("users") or [])
    sent = 0
    for uid in uids:
        w = _week(food, uid)
        msg = weekly_message(food, w)
        if not msg:
            if not always:
                continue
            msg = ("This week", "Nothing’s running low or running out this week that isn’t on a shopping list already.")
        actions = []
        n = len(w["low"]) + len(w["soon"])
        shop = (_shopping(food) or {}).get("name") or "Shopping"
        if n:
            # everything at once, or open This Week to pick (swipe a row for any list)
            actions.append({"action": ACTION_ADD_LOW, "title": f"Add {'It' if n == 1 else f'All {n}'} to {shop}", "authenticationRequired": True})
            actions.append({"open": "week", "title": "Choose What to Add"})
        add = add_action(food, other=bool(n))
        if add:
            actions.append(add)
        sent += await async_send(hass, [uid], msg[0], msg[1], "weekly", food, "week", actions)
    return sent


async def _add_low(hass: HomeAssistant, food: CrowFood, uid: str, soon: bool = True) -> None:
    """Add them to Shopping: what This Week says is needed (or, from the daily note, what's running low and not on
    a list) goes on the Shopping list."""
    shop = _shopping(food)
    if soon:
        w = _week(food, uid)
        rows = w["low"] + w["soon"]
    else:
        rows = [{"product": x["product"], "name": x["name"], "need": x["need"]} for x in low_unlisted(food)]
    if not shop or not rows:
        await async_send(hass, [uid], NAME, "Nothing needs adding now.", "weekly", food)
        return
    added = []
    for r in rows:
        try:
            await food.async_add_to_list(uid, r["product"], shop["id"], r["need"])
            added.append(r["name"])
        except Exception as err:  # noqa: BLE001 - a product deleted since
            _LOGGER.debug("Crow Food couldn't add %s: %s", r["name"], err)
    if added:
        n = len(added)
        await async_send(hass, [uid], f"Added to {shop.get('name') or 'Shopping'}",
                         f"{n} {'thing is' if n == 1 else 'things are'} on the {shop.get('name') or 'Shopping'} list: {_list_words(added[:6])}{' and more' if n > 6 else ''}.",
                         "weekly", food, f"storage:{shop['id']}")


def _months(n: int) -> str:
    return {12: "a year", 24: "two years"}.get(n, f"{n} months" if n != 1 else "a month")


async def async_stale(hass: HomeAssistant, food: CrowFood) -> int:
    """A note when more products reach Not Bought Lately (not every day: only when there are new ones)."""
    opts = food.settings.get("notify", {})
    stale = food.stale()
    ids = [p["id"] for p in stale]
    told = set(food.data.get("stale_notified") or [])
    new = [i for i in ids if i not in told]
    # products that left the list (bought again, kept or deleted) can be told about again later
    food.data["stale_notified"] = [i for i in ids if i in told or i in new]
    food._save()
    uids = opts.get("users") or []
    if not new or not uids or opts.get("stale", True) is False:
        return 0
    n = len(ids)
    months = int(food.settings.get("stale_months", 6) or 6)
    names = ", ".join(p["name"] for p in stale[:3]) + (f" and {n - 3} more" if n > 3 else "")
    msg = (f"{n} {'product hasn’t' if n == 1 else 'products haven’t'} been bought for {_months(months)}: {names}. "
           "Tap to review them.")
    return await async_send(hass, uids, "Not bought lately", msg, "stale", food, "review", [a for a in [add_action(food)] if a])


async def async_year(hass: HomeAssistant, food: CrowFood) -> int:
    """On 1 January: last year's Year in Food is ready (tapping it opens the report)."""
    opts = food.settings.get("notify", {})
    uids = opts.get("users") or []
    if not uids or opts.get("year", True) is False:
        return 0
    from homeassistant.util import dt as dt_util
    from .reports import _Ctx, r_year
    year = dt_util.now().year - 1
    sent = 0
    for uid in uids:
        y = r_year(_Ctx(food, uid, None), year)
        if not (y["used"] or y["thrown"] or y["bought"]):
            continue
        bits = [f"{y['used']} things used", f"{y['waste_share']:g}% thrown away"]
        if y.get("top_used"):
            bits.append(f"top of the list: {y['top_used'][0]['name']}")
        sent += await async_send(hass, [uid], f"Your {year} in Food", "; ".join(bits).capitalize() + ". Tap to see your year.",
                                 "year", food, "year", [a for a in [add_action(food)] if a])
    return sent
