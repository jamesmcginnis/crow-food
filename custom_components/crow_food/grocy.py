"""Import from Grocy: locations, products (with barcodes, groups, minimums and pictures), what's in stock and the
shopping lists, through Grocy's own API (its address and an API key from Grocy → Manage API keys).

It only ever adds. What came from Grocy is remembered, so running it again brings in only what's new in Grocy
since; nothing in Crow Food is changed or deleted, and nothing in Grocy is changed at all. Something imported
and then deleted in Crow Food isn't brought back.
"""
from __future__ import annotations

import asyncio
import base64
import bisect
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import KIND_LIST, KIND_STORAGE, UNITS
from .store import CrowFood, FoodError, now_ms

_LOGGER = logging.getLogger(__name__)

NEVER = "2999-12-31"          # Grocy's best-before for food that doesn't go off
TABLES = ("locations", "products", "product_barcodes", "product_groups", "quantity_units", "stock",
          "shopping_list", "shopping_lists", "shopping_locations")
# Grocy's quantity unit names -> Crow Food's units
UNIT_WORDS = {"gram": "g", "grams": "g", "gr": "g", "kilogram": "kg", "kilograms": "kg", "kilo": "kg",
              "millilitre": "ml", "milliliter": "ml", "millilitres": "ml", "milliliters": "ml",
              "litre": "l", "liter": "l", "litres": "l", "liters": "l", "pieces": "piece", "pcs": "piece",
              "packs": "pack", "packet": "package", "packets": "package", "packages": "package", "cans": "can",
              "bottles": "bottle", "jars": "jar", "tins": "tin", "tubs": "tub", "bags": "bag", "boxes": "box",
              "loaves": "loaf", "bunches": "bunch", "rolls": "roll", "cartons": "carton", "pots": "pot"}


class GrocyError(FoodError):
    pass


def clean_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    url = re.sub(r"/api$", "", url)
    parts = urlsplit(url)
    if not parts.netloc:
        raise GrocyError("grocy_url", "That doesn’t look like Grocy’s address. It’s something like http://192.168.1.20:9192")
    return url


class Grocy:
    def __init__(self, hass, url: str, key: str) -> None:
        self.url = clean_url(url)
        self.key = (key or "").strip()
        if not self.key:
            raise GrocyError("grocy_key", "Add Grocy’s API key (in Grocy: the spanner menu → Manage API keys).")
        # Grocy is usually on the home network, often with its own certificate
        self.session = async_get_clientsession(hass, verify_ssl=False)

    async def _get(self, path: str, raw: bool = False) -> Any:
        try:
            async with asyncio.timeout(30):
                r = await self.session.get(f"{self.url}/api/{path}", headers={"GROCY-API-KEY": self.key, "Accept": "*/*" if raw else "application/json"})
                if r.status == 401:
                    raise GrocyError("grocy_key", "Grocy didn’t accept that API key.")
                if r.status == 404 and not raw:
                    raise GrocyError("grocy_url", "Grocy’s API isn’t at that address. Check the address and port (the Home Assistant add-on uses its own port, often 9192, when it’s switched on in the add-on’s Network settings).")
                if r.status != 200:
                    if raw:
                        return None
                    raise GrocyError("grocy_failed", f"Grocy answered HTTP {r.status}.")
                return await r.read() if raw else await r.json(content_type=None)
        except GrocyError:
            raise
        except Exception as err:  # noqa: BLE001 - offline, wrong address, not JSON
            if raw:
                return None
            raise GrocyError("grocy_offline", f"Couldn’t reach Grocy at {self.url} ({type(err).__name__}).") from err

    async def version(self) -> str:
        info = await self._get("system/info")
        v = (info or {}).get("grocy_version") or {}
        return str(v.get("Version") or v.get("version") or "") if isinstance(v, dict) else str(v)

    async def tables(self) -> dict[str, list[dict]]:
        out = {}
        for t in TABLES:
            try:
                rows = await self._get(f"objects/{t}")
            except GrocyError as err:
                if t in ("locations", "products", "stock"):
                    raise
                _LOGGER.debug("Crow Food: Grocy has no %s (%s)", t, err)
                rows = []
            out[t] = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        return out

    async def picture(self, name: str) -> bytes | None:
        if not name:
            return None
        enc = base64.b64encode(name.encode()).decode()
        return await self._get(f"files/productpictures/{enc}?force_serve_as=picture&best_fit_width=800&best_fit_height=800", raw=True)


def _n(v: Any, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _id(v: Any) -> str:
    return "" if v in (None, "") else str(v).split(".")[0]


def _day(v: Any) -> str | None:
    s = str(v or "")[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) and s != NEVER and not s.startswith("2999") else None


def _unit(name: str) -> str:
    w = (name or "").strip().lower()
    w = UNIT_WORDS.get(w, w)
    return w if w in UNITS else "package"


def counts(t: dict[str, list[dict]]) -> dict:
    return {"locations": len(t["locations"]), "products": sum(1 for p in t["products"] if str(p.get("active", 1)) != "0"),
            "stock": len(t["stock"]), "shopping": sum(1 for s in t["shopping_list"] if str(s.get("done", 0)) in ("0", "", "None", "False"))}


async def async_check(hass, url: str, key: str) -> dict:
    g = Grocy(hass, url, key)
    ver = await g.version()
    return {"version": ver, "url": g.url, **counts(await g.tables())}


async def async_import(hass, food: CrowFood, uid: str, url: str, key: str, photos: bool = True) -> dict:
    """Brings in what's new in Grocy. Returns how many of each were added."""
    g = Grocy(hass, url, key)
    ver = await g.version()
    t = await g.tables()
    st = food.data.setdefault("grocy", {})
    st.update({"url": g.url, "key": g.key, "version": ver})
    m = st.setdefault("map", {})
    for k in ("locations", "lists", "groups", "shops", "products", "stock", "shopping"):
        m.setdefault(k, {})
    added = {"storages": 0, "lists": 0, "products": 0, "items": 0, "shopping": 0, "photos": 0, "categories": 0, "shops": 0}
    storages, products = food.data["storages"], food.data["products"]
    units = {_id(q.get("id")): q.get("name") or "" for q in t["quantity_units"]}

    # categories (Grocy's product groups) and shops (its stores): by name, so nothing is doubled up
    for grp in t["product_groups"]:
        gid = _id(grp.get("id"))
        if gid in m["groups"] or not grp.get("name"):
            continue
        before = len(food.data["categories"])
        m["groups"][gid] = (await food.async_save_simple("categories", {"name": str(grp["name"])}))["id"]
        added["categories"] += len(food.data["categories"]) - before
    for shop in t["shopping_locations"]:
        gid = _id(shop.get("id"))
        if gid in m["shops"] or not shop.get("name"):
            continue
        before = len(food.data["shops"])
        m["shops"][gid] = (await food.async_save_simple("shops", {"name": str(shop["name"])}))["id"]
        added["shops"] += len(food.data["shops"]) - before

    # locations -> storages (one with the same name is used instead of making another)
    def by_name(kind: str) -> dict[str, dict]:
        return {s["name"].strip().lower(): s for s in storages.values() if s.get("kind") == kind}
    for loc in t["locations"]:
        gid = _id(loc.get("id"))
        if gid in m["locations"] or not loc.get("name"):
            continue
        name = str(loc["name"]).strip()[:60]
        same = by_name(KIND_STORAGE).get(name.lower())
        if not same:
            freezer = str(loc.get("is_freezer", 0)) in ("1", "True", "true")
            same = await food.async_save_storage(uid, {"name": name, "kind": KIND_STORAGE,
                                                       "icon": "mdi:snowflake" if freezer else "mdi:archive-outline",
                                                       "color": "#5AC8FA" if freezer else "#8E8E93"})
            if freezer:
                same["freezer"] = True
            added["storages"] += 1
        m["locations"][gid] = same["id"]

    # shopping lists: Grocy's first list is Crow Food's Shopping list, others become lists of their own
    shopping = next((s for s in storages.values() if s.get("builtin") == "shopping"), None)
    for sl in t["shopping_lists"] or [{"id": 1, "name": "Shopping list"}]:
        gid = _id(sl.get("id"))
        if gid in m["lists"] and m["lists"][gid] in storages:
            continue
        if gid == "1" and shopping:
            m["lists"][gid] = shopping["id"]
            continue
        name = str(sl.get("name") or "Shopping").strip()[:60]
        same = by_name(KIND_LIST).get(name.lower())
        if not same:
            same = await food.async_save_storage(uid, {"name": name, "kind": KIND_LIST, "icon": "mdi:cart-outline", "color": "#5BC864"})
            added["lists"] += 1
        m["lists"][gid] = same["id"]

    # products (with every barcode), matched to one already in Crow Food by barcode, then by name
    codes: dict[str, list[dict]] = {}
    for b in t["product_barcodes"]:
        code = re.sub(r"\s", "", str(b.get("barcode") or ""))
        if code:
            codes.setdefault(_id(b.get("product_id")), []).append({**b, "barcode": code})
    last_price: dict[str, tuple[str, float, str]] = {}
    for s in t["stock"]:
        if _n(s.get("price")) > 0:
            pid, day = _id(s.get("product_id")), str(s.get("purchased_date") or "")
            if pid not in last_price or day > last_price[pid][0]:
                last_price[pid] = (day, round(_n(s.get("price")), 2), _id(s.get("shopping_location_id")))
    names = {p["name"].strip().lower(): p for p in products.values()}
    pictures: list[tuple[str, str]] = []
    for gp in t["products"]:
        gid = _id(gp.get("id"))
        if gid in m["products"] or str(gp.get("active", 1)) == "0" or not gp.get("name"):
            continue
        bcs = [b["barcode"] for b in codes.get(gid, [])]
        same = next((food.find_barcode(c) for c in bcs if food.find_barcode(c)), None) or names.get(str(gp["name"]).strip().lower())
        if same:
            m["products"][gid] = same["id"]
            continue
        lp = last_price.get(gid)
        shop = m["shops"].get(lp[2]) if lp and lp[2] else None
        row = await food.async_save_product(uid, {
            "name": str(gp["name"]).strip(), "barcode": bcs[0] if bcs else "",
            "category": m["groups"].get(_id(gp.get("product_group_id"))),
            "unit": _unit(units.get(_id(gp.get("qu_id_stock")), "")),
            "min_qty": max(0.0, _n(gp.get("min_stock_amount"))), "price": lp[1] if lp else 0, "shop": shop,
            "note": re.sub(r"<[^>]+>", " ", str(gp.get("description") or "")).strip()[:1000],
            "home": m["locations"].get(_id(gp.get("location_id"))),
            "no_expiry": _n(gp.get("default_best_before_days")) < 0,
            "source": "import"})
        if len(bcs) > 1:
            row["barcodes"] = bcs[1:20]
        row["grocy"] = gid
        m["products"][gid] = row["id"]
        names[row["name"].lower()] = row
        added["products"] += 1
        if photos and gp.get("picture_file_name"):
            pictures.append((row["id"], str(gp["picture_file_name"])))

    # what's in stock: each Grocy stock entry becomes an item, with its dates, price and whether it's open
    for s in t["stock"]:
        sid = _id(s.get("id")) or f"{_id(s.get('product_id'))}:{s.get('stock_id')}"
        if sid in m["stock"]:
            continue
        pid = m["products"].get(_id(s.get("product_id")))
        storage = m["locations"].get(_id(s.get("location_id")))
        if not storage:
            gp = next((p for p in t["products"] if _id(p.get("id")) == _id(s.get("product_id"))), {})
            storage = m["locations"].get(_id(gp.get("location_id")))
        if not pid or pid not in products or not storage or storage not in storages or _n(s.get("amount")) <= 0:
            continue
        opened = str(s.get("open", 0)) in ("1", "True", "true")
        item = await food.async_save_item(uid, {
            "product": pid, "storage": storage, "qty": round(_n(s.get("amount"), 1), 4),
            "unit": products[pid].get("unit") or "package", "expires": _day(s.get("best_before_date")),
            "stored": _day(s.get("purchased_date")), "opened": (_day(s.get("opened_date")) or _day(datetime.now().isoformat())) if opened else None,
            "price": round(_n(s.get("price")), 2), "note": str(s.get("note") or "")[:200],
            "shop": m["shops"].get(_id(s.get("shopping_location_id"))), "separate": True})
        _backdate(food, item, s.get("purchased_date"))
        m["stock"][sid] = item["id"]
        added["items"] += 1

    # shopping lists: what's not done yet (a line that's only a note becomes a product of that name)
    for sl in t["shopping_list"]:
        gid = _id(sl.get("id"))
        if gid in m["shopping"] or str(sl.get("done", 0)) in ("1", "True", "true"):
            continue
        list_id = m["lists"].get(_id(sl.get("shopping_list_id")) or "1") or (shopping or {}).get("id")
        pid = m["products"].get(_id(sl.get("product_id")))
        if not pid:
            note = re.sub(r"<[^>]+>", " ", str(sl.get("note") or "")).strip()
            if not note:
                continue
            same = names.get(note.lower())
            if not same:
                same = await food.async_save_product(uid, {"name": note[:160], "source": "import"})
                names[note.lower()] = same
                added["products"] += 1
            pid = same["id"]
        if not list_id or list_id not in storages or pid not in products:
            continue
        item = await food.async_save_item(uid, {"product": pid, "storage": list_id, "qty": max(1.0, round(_n(sl.get("amount"), 1), 4)),
                                                "unit": products[pid].get("unit") or "package",
                                                "note": "" if not _id(sl.get("product_id")) else str(sl.get("note") or "")[:200]})
        m["shopping"][gid] = item["id"]
        added["shopping"] += 1

    st["last"] = now_ms()
    st["added"] = added
    food._save()
    food._save_history()
    food.resync()

    # pictures last (they're the slow part), a few at a time
    for pid, name in pictures:
        p = products.get(pid)
        if not p or p.get("photo"):
            continue
        raw = await g.picture(name)
        if not raw:
            continue
        try:
            p["photo"] = await food.async_save_photo(raw)
        except FoodError:
            continue
        food._changed("products", [p])
        added["photos"] += 1
    if added["photos"]:
        food._save()
    return {"added": added, "version": ver, "url": g.url}


def _backdate(food: CrowFood, item: dict, purchased: Any) -> None:
    """The Added to Stock log for an imported item goes on the day it was bought in Grocy, not today
    (moved to its place in the history, which is kept in time order)."""
    logs = food.data.get("logs", [])
    day = _day(purchased)
    for i in range(len(logs) - 1, max(-1, len(logs) - 6), -1):
        if logs[i].get("item") != item["id"]:
            continue
        log = logs[i]
        log["via"] = "grocy"
        if day:
            try:
                ts = int(datetime.fromisoformat(day + "T12:00:00").timestamp() * 1000)
            except ValueError:
                break
            if ts < log["ts"]:
                del logs[i]
                log["ts"] = ts
                logs.insert(bisect.bisect_right([x.get("ts", 0) for x in logs], ts), log)
        break


def status(food: CrowFood) -> dict:
    st = food.data.get("grocy") or {}
    return {"url": st.get("url") or "", "has_key": bool(st.get("key")), "last": st.get("last"), "added": st.get("added"),
            "version": st.get("version") or "", "known": {k: len(v) for k, v in (st.get("map") or {}).items()}}
