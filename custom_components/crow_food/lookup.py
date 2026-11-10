"""Look Up a Food: About This Food for anything, not only products you have.

* By name - the AI looks the food up the same way as About This Food (storage, how long it keeps, a typical
  serving, ideas), with a typical Nutri-Score and NOVA group for that kind of food. A picture of the food comes
  from Wikipedia. Answers are kept (the last 300), so the same food again is instant and costs nothing.
* By barcode - Open Food Facts gives the real product: its name, picture and real scores, whether or not you
  have it. A barcode you already have as a product just says which product.
* By photo - with no barcode, the AI says what food is in the photo, then it's looked up by name.
"""
from __future__ import annotations

import re

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import info as info_mod
from .const import DOMAIN
from .store import CrowFood, FoodError, now_ms

KEEP = 300
NUTRI = ("a", "b", "c", "d", "e")


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())[:120]


def _box(food: CrowFood) -> dict:
    return food.data.setdefault("lookups", {})


def _keep(food: CrowFood, key: str, entry: dict) -> None:
    box = _box(food)
    box[key] = entry
    if len(box) > KEEP:   # the ones looked at longest ago go
        for k in sorted(box, key=lambda k: box[k].get("seen", 0))[: len(box) - KEEP]:
            box.pop(k, None)
    food._save()


def _typical(out: dict) -> dict | None:
    nutri = str(out.get("nutriscore_typical") or "").strip().lower()[:1]
    try:
        nova = int(out.get("nova_typical"))
    except (TypeError, ValueError):
        nova = None
    g = {"nutri": nutri if nutri in NUTRI else None, "nova": nova if nova in (1, 2, 3, 4) else None}
    return g if g["nutri"] or g["nova"] else None


async def _picture(food: CrowFood, term: str) -> str | None:
    """A picture of the food from Wikipedia (only pages about a food), as its address."""
    if not term:
        return None
    try:
        found = await info_mod._wiki_candidates(async_get_clientsession(food.hass), term)
    except Exception:  # noqa: BLE001 - offline
        return None
    return found[0]["thumb"] if found else None


async def async_lookup(hass, food: CrowFood, name: str, refresh: bool = False) -> dict:
    from . import ai

    key = _key(name)
    if not key:
        raise FoodError("no_name", "Type a food to look up.")
    have = _box(food).get(key)
    if have and have.get("v") == info_mod.SCHEMA and not refresh:
        have["seen"] = now_ms()
        return have
    if not ai.feature_on(food, "info"):
        raise FoodError("ai_off", ai.FRIENDLY["off"])
    country = hass.config.country or ""
    text = await ai._task(hass, food, "Crow Food: look up a food", (
        "You are a general food, nutrition and storage reference, like a food-label lookup tool. "
        f'For the food or dish "{name.strip()}"' + (f" (in {country})" if country else "")
        + ", return ONLY a JSON object (no markdown, no commentary) with fields: "
        '{"name":"...","generic_search_term":"...","category":"...","typical_serving":"...","approx_carbs_g":number_or_null,'
        '"gi_category":"low"|"medium"|"high"|"unknown","calories_kcal":number_or_null,"dietary_tags":["..."],'
        '"storage_tip":"...","where_to_keep":"cupboard"|"fridge"|"freezer","shelf_life_unopened":"..."_or_null,'
        '"shelf_life_opened":"..."_or_null,"opened_days":number_or_null,"shelf_life_frozen":"..."_or_null,'
        '"frozen_days":number_or_null,"keeps_days":number_or_null,"nutriscore_typical":"a"|"b"|"c"|"d"|"e"|null,'
        '"nova_typical":1|2|3|4|null,"fun_fact":"...","note":"...","ingredients":["..."]_or_null}. '
        "These are general estimates for a typical food of this kind, not a particular brand. "
        '"name" is the food\'s usual name, nicely written. '
        '"generic_search_term" is the single most common name for this food, 1 to 3 words, as its Wikipedia article '
        'would be titled; no brands or sizes. '
        '"approx_carbs_g" and "calories_kcal" are for the "typical_serving" amount, not per 100 g. '
        '"storage_tip" is one or two short sentences on keeping it well (for something cooked, how to cool, keep and '
        'reheat it safely). "shelf_life_*" are short phrases like "18 months" or "3 to 5 days": "shelf_life_unopened" '
        'only for something bought sealed (null otherwise), "shelf_life_opened" is once opened, or once cooked or made '
        'for a dish or leftovers. "opened_days" is how many days it keeps once opened or cooked, "frozen_days" how many '
        'days it keeps well frozen (null if it shouldn\'t be frozen). "keeps_days" is how many days it keeps from '
        'today when you have it at home without a date on it (made at home, cooked, leftovers, loose fruit and veg, '
        'bakery bread); null for something bought in a sealed packet with its own date. '
        '"nutriscore_typical" is the Nutri-Score a typical product of this kind would get, and "nova_typical" its '
        'usual NOVA processing group; null when it varies too much to say. '
        '"dietary_tags" are short, only when clearly true (vegetarian, vegan, gluten-free, dairy-free, contains nuts). '
        '"ingredients" are 4 to 8 typical ingredients, only for prepared foods and dishes (null for simple foods). '
        '"note" is one short sentence about how much brands or recipes vary. If it isn\'t a food, still give your best '
        "guess for name and category and leave the rest null."))
    out = ai._json(text)
    if not isinstance(out, dict) or not out:
        raise FoodError("ai_failed", ai.FRIENDLY["garbled"])
    entry = info_mod._clean(out)
    entry["asked"] = name.strip()[:120]
    entry["typical"] = _typical(out)
    entry["keeps_days"] = info_mod._days(out.get("keeps_days"))
    entry["picture"] = await _picture(food, entry.get("generic") or entry.get("name") or name)
    if have and not refresh:
        entry.update({k: v for k, v in have.items() if k.startswith("ideas_")})
    entry["seen"] = now_ms()
    _keep(food, key, entry)
    return entry


async def async_ideas(hass, food: CrowFood, name: str, kind: str) -> list[str]:
    """Serving or cooking ideas for a food looked up (kept with its answer)."""
    from . import ai

    if kind not in ("serving", "cooking"):
        raise FoodError("bad", "Unknown kind of idea.")
    key = _key(name)
    entry = _box(food).get(key) or {}
    if entry.get(f"ideas_{kind}"):
        return entry[f"ideas_{kind}"]
    if not ai.feature_on(food, "info"):
        raise FoodError("ai_off", ai.FRIENDLY["off"])
    food_name = entry.get("name") or name
    cat = entry.get("category") or ""
    ask = ("suggest 3 or 4 short, appetising ways to serve or pair it: sides, sauces or occasions. Each under 12 words."
           if kind == "serving" else
           "suggest 3 or 4 short tips or variations for preparing it beyond the basic method: seasoning, flavour "
           "pairings or ways to make it better. Each under 14 words.")
    text = await ai._task(hass, food, f"Crow Food: {kind} ideas", (
        f'For the food "{food_name}"' + (f" (category: {cat})" if cat else "") + f", {ask} "
        'Return ONLY a JSON array of short strings (no markdown, no numbering), e.g. ["...", "..."].'))
    out = ai._json(text)
    if isinstance(out, dict):
        out = next((v for v in out.values() if isinstance(v, list)), [])
    ideas = info_mod._strs(out, 5, 120)
    if not ideas:
        raise FoodError("ai_failed", "Nothing to suggest for this one.")
    if entry:
        entry[f"ideas_{kind}"] = ideas
        food._save()
    return ideas


async def async_barcode(hass, food: CrowFood, code: str) -> dict:
    """A barcode: the product you have with it, or the product on Open Food Facts with its real scores."""
    from .extras import _grade

    code = re.sub(r"\D", "", str(code or ""))
    if not 8 <= len(code) <= 14:
        raise FoodError("bad", "That isn’t a barcode.")
    for p in food.data["products"].values():
        if str(p.get("barcode") or "") == code:
            return {"product": p["id"], "code": code}
    if not food.settings.get("openfoodfacts", True):
        raise FoodError("off_off", "Open Food Facts is switched off, so barcodes of things you don’t have can’t be looked up.")
    session = async_get_clientsession(hass)
    try:
        data = await info_mod.get_json(session, "off_barcode", f"https://world.openfoodfacts.org/api/v2/product/{code}.json",
                                       {"fields": "product_name,product_name_en,generic_name,brands,quantity,image_front_url,"
                                                  "image_front_small_url,nutriscore_grade,ecoscore_grade,"
                                                  "environmental_score_grade,nova_group"}, tries=2, timeout=15)
    except info_mod.ServiceError as err:
        if err.status == 404:
            raise FoodError("not_found", "That barcode isn’t on Open Food Facts yet.") from err
        raise FoodError("offline", "Couldn’t reach Open Food Facts. Try again in a moment.") from err
    prod = (data or {}).get("product") or {}
    title = prod.get("product_name_en") or prod.get("product_name") or ""
    if not title:
        raise FoodError("not_found", "That barcode isn’t on Open Food Facts yet.")
    brand = str(prod.get("brands") or "").split(",")[0].strip()
    name = " ".join(x for x in (brand if brand.lower() not in title.lower() else "", title, prod.get("quantity") or "") if x).strip()
    nova = prod.get("nova_group")
    return {"code": code, "name": name[:160], "generic": str(prod.get("generic_name") or "")[:80] or None,
            "picture": prod.get("image_front_small_url") or prod.get("image_front_url"),
            "grades": {"nutri": _grade(prod.get("nutriscore_grade")),
                       "eco": _grade(prod.get("environmental_score_grade") or prod.get("ecoscore_grade")),
                       "nova": nova if nova in (1, 2, 3, 4) else None}}


async def async_photo(hass, food: CrowFood, photo: str) -> dict:
    """What's in a photo: a barcode the AI can read, or the food it shows."""
    from . import ai

    pid = str(photo or "")
    if not pid or not await hass.async_add_executor_job(food.photo_path(pid).is_file):
        raise FoodError("bad", "Take a photo first.")
    if not ai.feature_on(food, "info"):
        raise FoodError("ai_off", ai.FRIENDLY["off"])
    if not ai.supports_photos(hass, ai.task_entity(hass, food)):
        raise FoodError("ai_failed", ai.FRIENDLY["no_photos"])
    text = await ai._task(hass, food, "Crow Food: what food is this", (
        "This is a photo of a food: a packet, a tin or bottle, loose fruit or vegetables, bakery food, or a meal or "
        "leftovers on a plate or in a box.\n"
        "If a barcode's number can be read clearly (the digits under the bars), give it. Otherwise say what the food "
        "is: for a packet, its product name as the shop would write it; for anything else, its usual name, like "
        '"Granny Smith apple", "Chicken tikka masala" or "Sourdough loaf". If it isn\'t a food, leave name empty.\n'
        'Answer with JSON only: {"barcode": "digits or empty", "name": "..."}'),
        attachments=[{"media_content_id": f"media-source://{DOMAIN}/{pid}", "media_content_type": food.photo_mime(pid)}])
    out = ai._json(text)
    if not isinstance(out, dict):
        raise FoodError("ai_failed", ai.FRIENDLY["garbled"])
    code = re.sub(r"\D", "", str(out.get("barcode") or ""))
    name = str(out.get("name") or "").strip()[:120]
    if code and ai.gtin_ok(code):
        return {"barcode": code, "name": name}
    if not name:
        raise FoodError("not_food", "Couldn’t see a food in that photo. Try another, closer and in good light.")
    return {"name": name}
