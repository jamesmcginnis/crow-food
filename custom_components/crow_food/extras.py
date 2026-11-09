"""Things Crow Food looks up for the household, with no account or key needed:

* Recall alerts: the Food Standards Agency's product recalls and allergy alerts (UK), checked against what's at
  home every few hours. A match shows on the card and (once) as a note to the people chosen for reminders.
* Nutri-Score and Eco-Score: from Open Food Facts, for products with a barcode (filled in slowly in the background,
  so Open Food Facts isn't asked for hundreds at once).
* Price watch: what other people paid for the same barcode, from Open Prices (Open Food Facts' price database).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import DOMAIN, KIND_STORAGE
from .info import ServiceError, get_json
from .store import CrowFood, now_ms

_LOGGER = logging.getLogger(__name__)

FSA_URL = "https://data.food.gov.uk/food-alerts/id"
FSA_PARAMS = {"_limit": "50", "_sort": "-created", "_view": "full"}
OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
PRICES_URL = "https://prices.openfoodfacts.org/api/v1/prices"
RECALL_DAYS = 45          # alerts older than this are left alone
GRADES_PER_RUN = 100       # products looked up per run (one a second)
GRADES_AGAIN = 30 * 86400000   # a product Open Food Facts didn't score is asked again after a month
PRICES_KEEP = 86400       # prices remembered for a day
_prices: dict[str, tuple[float, list]] = {}
RECALLS_EVERY = timedelta(hours=6)
_alerts: list[tuple[int, list[dict]]] = []   # the latest alerts fetched (when, alerts), to match new stock against
NEW_STOCK_SCORES = 20        # scores looked up at once for new stock (more than that waits for the next run)
SCORES_EVERY = timedelta(hours=8)
# what's running now and when each job runs next (for the editor's Background Updates)
JOBS: dict[str, dict] = {"recalls": {"running": False, "next": None}, "scores": {"running": False, "next": None}}

# words that say nothing about which product it is
_SKIP = {"the", "and", "with", "of", "in", "a", "an", "for", "pack", "packs", "x", "g", "kg", "ml", "l", "cl", "pcs", "pieces",
         "recall", "recalls", "product", "products", "because", "may", "contain", "undeclared", "due", "to", "presence"}


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", str(text or "").lower().replace("’", "").replace("'", "")):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if len(w) > 1 and w not in _SKIP:
            out.add(w)
    return out


def _text(v: Any) -> str:
    """A field that may be a string, a list of them, or {"@value": ...} / {"label": ...}."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return " ".join(_text(x) for x in v)
    if isinstance(v, dict):
        return _text(v.get("@value") or v.get("label") or v.get("commonName") or v.get("prefLabel") or v.get("@id") or "")
    return ""


def _url(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get("@id") or v.get("url") or "")
    if isinstance(v, list) and v:
        return _url(v[0])
    return ""


# ── Recall alerts ─────────────────────────────────────────────────────────────
def _problem(v: Any) -> str:
    """What's wrong, in a sentence (the risk statement, or the allergen's name)."""
    first = v[0] if isinstance(v, list) and v else v
    if isinstance(first, dict):
        return (_text(first.get("riskStatement")) or _text(first.get("allergen")))[:300]
    return _text(first)[:300]

def parse_alerts(j: Any) -> list[dict]:
    """The FSA's answer as plain alerts: id, title, date, kind, link, the products named and the business."""
    items = (j or {}).get("items") if isinstance(j, dict) else None
    out = []
    for a in items or []:
        if not isinstance(a, dict):
            continue
        kinds = _text(a.get("type")).upper()
        kind = "allergy" if re.search(r"\bAA\b|ALLERGY", kinds) else "recall"
        prods = a.get("productDetails") or []
        if isinstance(prods, dict):
            prods = [prods]
        names = []
        for pd in prods:
            if not isinstance(pd, dict):
                continue
            nm = _text(pd.get("productName"))
            size = _text(pd.get("packSizeDescription"))
            batches = pd.get("batchDescription") or []
            if isinstance(batches, dict):
                batches = [batches]
            when = "; ".join(filter(None, (" ".join(filter(None, (_text(b.get("batchCode")), _text(b.get("bestBeforeDescription")) or _text(b.get("useByDescription")) or _text(b.get("bestBeforeDate")) or _text(b.get("useByDate"))))) for b in batches if isinstance(b, dict))))
            if nm:
                names.append({"name": nm[:160], "size": size[:60], "batch": when[:200]})
        business = _text(a.get("reportingBusiness"))[:80]
        out.append({"id": str(a.get("notation") or a.get("@id") or a.get("id") or "")[:120],
                    "title": _text(a.get("title") or a.get("shortTitle"))[:240],
                    "created": str(a.get("created") or a.get("modified") or "")[:10],
                    "kind": kind, "url": _url(a.get("alertURL")) or _url(a.get("@id")) or _url(a.get("id")),
                    "products": names, "business": business, "problem": _problem(a.get("problem"))})
    return [a for a in out if a["id"] and a["title"]]


def match_alerts(food: CrowFood, alerts: list[dict]) -> list[dict]:
    """Alerts that name something at home: the business's name (or the brand) and most of the product's words."""
    cutoff = (dt_util.now().date() - timedelta(days=RECALL_DAYS)).isoformat()
    home: dict[str, list[str]] = {}
    for it in food.data["items"].values():
        st = food.data["storages"].get(it.get("storage")) or {}
        if st.get("kind") == KIND_STORAGE:
            home.setdefault(it["product"], []).append(it["id"])
    prods = [(pid, food.data["products"].get(pid) or {}) for pid in home]
    out = []
    for a in alerts:
        if a["created"] and a["created"] < cutoff:
            continue
        brand = _words(a["business"])
        hits = []
        for pd in a["products"]:
            want = _words(pd["name"])
            if not want:
                continue
            for pid, p in prods:
                have = _words(p.get("name", ""))
                if not have:
                    continue
                same = want & have
                # the brand (or the business) in the product's name, and most of what the alert calls it
                branded = bool(brand & have) or bool(brand & want & have)
                if len(same) >= 2 and len(same) >= 0.6 * len(want) and (branded or len(same) >= 0.8 * len(want) + 0.01):
                    if pid not in [h["product"] for h in hits]:
                        hits.append({"product": pid, "name": p.get("name", ""), "items": home[pid], "alert_product": pd["name"],
                                     "size": pd["size"], "batch": pd["batch"]})
        if hits:
            out.append({**a, "matches": hits})
    return out


async def async_recalls(hass, food: CrowFood, notify_new: bool = True) -> list[dict]:
    """Asks the FSA for its latest alerts and keeps the ones that match something at home (and tells people once)."""
    if food.settings.get("recalls", True) is False:
        food.data["recalls"] = []
        return []
    session = async_get_clientsession(hass)
    try:
        j = await get_json(session, "fsa", FSA_URL, FSA_PARAMS)
    except ServiceError as err:
        _LOGGER.debug("Crow Food couldn't get the FSA alerts: %s", err)
        return food.data.get("recalls") or []
    _alerts[:] = [(now_ms(), parse_alerts(j))]
    food.data["recalls_checked"] = now_ms()
    return await _apply_alerts(hass, food, _alerts[0][1], notify_new)


async def _apply_alerts(hass, food: CrowFood, alerts: list[dict], notify_new: bool) -> list[dict]:
    """Keeps the alerts that match something at home now, and tells people about new ones once."""
    dismissed = set(food.data.get("recalls_dismissed") or [])
    found = [a for a in match_alerts(food, alerts) if a["id"] not in dismissed]
    changed = [a["id"] for a in found] != [a["id"] for a in food.data.get("recalls") or []]
    food.data["recalls"] = found
    food._save()
    if changed:
        food.broadcast_recalls()
    told = set(food.data.get("recalls_told") or [])
    new = [a for a in found if a["id"] not in told]
    if new and notify_new:
        from . import notify
        opts = food.settings.get("notify", {})
        users = opts.get("users") or []
        if users and opts.get("recalls", True) is not False:
            for a in new:
                names = ", ".join(dict.fromkeys(m["name"] for m in a["matches"]))
                what = "allergy alert" if a["kind"] == "allergy" else "recall"
                await notify.async_send(hass, users, f"Possible food {what}", f"{names}: {a['title']} Check the pack before eating it.",
                                        f"recall_{len(told)}", food, "recalls",
                                        [{"action": "URI", "title": "Read the alert", "uri": a["url"]}] if a["url"] else None)
        food.data["recalls_told"] = list(told | {a["id"] for a in new})[-200:]
        food._save()
    return found


async def async_dismiss_recall(food: CrowFood, alert_id: str) -> None:
    food.data["recalls_dismissed"] = list(dict.fromkeys([*(food.data.get("recalls_dismissed") or []), alert_id]))[-300:]
    food.data["recalls"] = [a for a in food.data.get("recalls") or [] if a["id"] != alert_id]
    food._save()
    food.broadcast_recalls()


# ── Nutri-Score and Eco-Score ─────────────────────────────────────────────────
def _grade(v: Any) -> str | None:
    g = str(v or "").strip().lower()
    return g if g in ("a", "b", "c", "d", "e") else None


async def async_fill_grades(hass, food: CrowFood) -> int:
    """Products with a barcode and no scores yet: asked of Open Food Facts, a few at a time."""
    if food.settings.get("openfoodfacts", True) is False:
        return 0
    session = async_get_clientsession(hass)
    todo = [p for p in food.data["products"].values() if re.fullmatch(r"\d{8,14}", p.get("barcode") or "")
            and (not p.get("grades") or (not p["grades"].get("nutri") and not p["grades"].get("eco") and now_ms() - p["grades"].get("at", 0) > GRADES_AGAIN))]
    done = 0
    for p in todo[:GRADES_PER_RUN]:
        try:
            j = await get_json(session, "off_barcode", OFF_URL.format(code=p["barcode"]),
                               {"fields": "nutriscore_grade,ecoscore_grade,environmental_score_grade,nova_group"})
        except ServiceError as err:
            if err.status == 404:
                p["grades"] = {"at": now_ms()}
                continue
            break   # Open Food Facts is busy: next time
        prod = (j or {}).get("product") or {}
        p["grades"] = {"nutri": _grade(prod.get("nutriscore_grade")),
                       "eco": _grade(prod.get("environmental_score_grade") or prod.get("ecoscore_grade")),
                       "nova": prod.get("nova_group") if prod.get("nova_group") in (1, 2, 3, 4) else None, "at": now_ms()}
        food._changed("products", [p])
        done += 1
        await asyncio.sleep(1)
    if done:
        food._save()
    return done


async def async_grade_one(hass, food: CrowFood, pid: str) -> bool:
    """One product's scores, now (it was just put into stock or given a barcode)."""
    p = food.data["products"].get(pid)
    if not p or food.settings.get("openfoodfacts", True) is False or not re.fullmatch(r"\d{8,14}", p.get("barcode") or "") or p.get("grades"):
        return False
    try:
        j = await get_json(async_get_clientsession(hass), "off_barcode", OFF_URL.format(code=p["barcode"]),
                           {"fields": "nutriscore_grade,ecoscore_grade,environmental_score_grade,nova_group"})
    except ServiceError as err:
        if err.status == 404:
            p["grades"] = {"at": now_ms()}
            food._changed("products", [p])
        return False
    prod = (j or {}).get("product") or {}
    p["grades"] = {"nutri": _grade(prod.get("nutriscore_grade")),
                   "eco": _grade(prod.get("environmental_score_grade") or prod.get("ecoscore_grade")),
                   "nova": prod.get("nova_group") if prod.get("nova_group") in (1, 2, 3, 4) else None, "at": now_ms()}
    food._changed("products", [p])
    return True


async def async_new_stock(hass, food: CrowFood, pids: list[str], recalls: bool) -> None:
    """Something just went into stock: its scores, and whether it matches a recall alert (the latest alerts
    fetched are used; if there are none yet, or they're over an hour old, they're fetched first)."""
    for pid in pids[:NEW_STOCK_SCORES]:
        if await async_grade_one(hass, food, pid):
            await asyncio.sleep(1)
    if not recalls or food.settings.get("recalls", True) is False:
        return
    if _alerts and now_ms() - _alerts[0][0] < 3600000:
        await _apply_alerts(hass, food, _alerts[0][1], True)
    elif not JOBS["recalls"]["running"]:
        await run_job(hass, food, "recalls", scheduled=False, notify=True)


# ── Price watch ───────────────────────────────────────────────────────────────
async def async_prices(hass, food: CrowFood, code: str, currency: str) -> list[dict]:
    """What people paid for this barcode lately (the last year), the newest at each shop, cheapest first."""
    code = re.sub(r"\D", "", code or "")
    if not code or food.settings.get("open_prices", False) is not True:
        return []
    key = f"{code}|{currency}"
    hit = _prices.get(key)
    if hit and time.time() - hit[0] < PRICES_KEEP:
        return hit[1]
    j = await get_json(async_get_clientsession(hass), "open_prices", PRICES_URL,
                       {"product_code": code, "order_by": "-date", "size": "100", "currency": currency})
    since = (dt_util.now().date() - timedelta(days=365)).isoformat()
    shops: dict[str, dict] = {}
    for x in (j or {}).get("items") or []:
        if not isinstance(x, dict) or (currency and str(x.get("currency") or "").upper() != currency.upper()):
            continue
        if x.get("product_code") and str(x["product_code"]) != code:
            continue
        try:
            price = float(x.get("price"))
        except (TypeError, ValueError):
            continue
        day = str(x.get("date") or "")[:10]
        if not price or (day and day < since) or x.get("price_per") not in (None, "", "UNIT"):
            continue
        loc = x.get("location") or {}
        shop = str(loc.get("osm_name") or loc.get("website_url") or "").strip() or "A shop"
        place = str(loc.get("osm_address_city") or "").strip()
        k = f"{shop}|{place}".lower()
        if k not in shops or day > shops[k]["date"]:
            shops[k] = {"shop": shop[:60], "place": place[:60], "price": round(price, 2), "date": day,
                        "offer": bool(x.get("price_is_discounted"))}
    out = sorted(shops.values(), key=lambda s: (s["price"], s["date"]))[:8]
    _prices[key] = (time.time(), out)
    return out


async def run_job(hass, food: CrowFood, job: str, scheduled: bool = True, notify: bool | None = None) -> Any:
    """Runs recalls or scores once (unless it's already running), noting when it runs next."""
    st = JOBS[job]
    if scheduled:
        st["next"] = now_ms() + int((RECALLS_EVERY if job == "recalls" else SCORES_EVERY).total_seconds() * 1000)
    if st["running"]:
        return None
    st["running"] = True
    try:
        if job == "recalls":
            return await async_recalls(hass, food, notify_new=scheduled if notify is None else notify)
        done = await async_fill_grades(hass, food)
        food.data["grades_ran"] = now_ms()
        food._save()
        return done
    finally:
        st["running"] = False


def saved_stats(food: CrowFood) -> dict:
    from .saved_receipts import stats
    return stats(food)


def status(hass, food: CrowFood) -> dict:
    """How far each background job has got, for the editor's Background Updates and the card's progress notes."""
    s = food.settings
    coded = [p for p in food.data["products"].values() if re.fullmatch(r"\d{8,14}", p.get("barcode") or "")]
    checked = [p for p in coded if p.get("grades")]
    scored = [p for p in checked if p["grades"].get("nutri") or p["grades"].get("eco")]
    keeper = hass.data.get(f"{DOMAIN}_photos")
    meta = food.data.get("photos") or {}
    used = {p["photo"] for p in food.data["products"].values() if p.get("photo")}
    pstats = food.data.get("photo_stats") or {}
    now = time.time()
    return {
        "scores": {"on": s.get("openfoodfacts", True) is not False, "total": len(coded), "checked": len(checked),
                   "scored": len(scored), "waiting": len(coded) - len(checked), "per_run": GRADES_PER_RUN,
                   "running": JOBS["scores"]["running"], "last_run": food.data.get("grades_ran"), "next": JOBS["scores"]["next"]},
        "recalls": {"on": s.get("recalls", True) is not False, "checked": food.data.get("recalls_checked"),
                    "matches": len(food.data.get("recalls") or []), "running": JOBS["recalls"]["running"], "next": JOBS["recalls"]["next"]},
        "photos": {"photos": len(used), "waiting": sum(1 for k, m in meta.items() if k in used and (m.get("opt") not in ("webp", "kept") or m.get("text") != ".webp")),
                   "running": bool(keeper and keeper.running), "progress": (keeper.progress if keeper and keeper.running else {}),
                   "last_run": pstats.get("last_run")},
        "receipts": saved_stats(food),
        "prices": {"on": s.get("open_prices", False) is True, "remembered": sum(1 for t, _ in _prices.values() if now - t < PRICES_KEEP)},
    }


def start(hass, food: CrowFood, entry) -> None:
    """Recall checks every six hours (and soon after starting); scores filled in every eight hours and soon after starting."""
    from homeassistant.helpers.event import async_call_later, async_track_time_interval

    def later(job):
        # marked @callback so Home Assistant runs it on its own loop (a plain lambda is run in a worker thread,
        # where starting a task fails and the job never runs)
        @callback
        def _go(_now=None) -> None:
            hass.async_create_background_task(run_job(hass, food, job), f"{DOMAIN} {job}")
        return _go

    # new stock: checked a few seconds after it's added (so a whole shop put away is one check)
    pending: dict[str, bool] = {}
    timer: list = []

    @callback
    def _flush(_now=None) -> None:
        timer.clear()
        pids, recalls = list(pending), any(pending.values())
        pending.clear()
        hass.async_create_background_task(async_new_stock(hass, food, pids, recalls), f"{DOMAIN} new stock")

    @callback
    def _stock_up(pids: list[str], scores_only: bool = False) -> None:
        for pid in pids:
            pending[pid] = pending.get(pid, False) or not scores_only
        if not timer:
            timer.append(async_call_later(hass, 5, _flush))

    food.on_stock_up = _stock_up

    @callback
    def _stop() -> None:
        food.on_stock_up = None
        if timer:
            timer[0]()
    entry.async_on_unload(_stop)

    JOBS["recalls"]["next"] = now_ms() + 120000
    JOBS["scores"]["next"] = now_ms() + 300000
    entry.async_on_unload(async_call_later(hass, 120, later("recalls")))
    entry.async_on_unload(async_track_time_interval(hass, later("recalls"), RECALLS_EVERY))
    entry.async_on_unload(async_call_later(hass, 300, later("scores")))
    entry.async_on_unload(async_track_time_interval(hass, later("scores"), SCORES_EVERY))
