"""Saved receipts: the photos, PDF or text a receipt or order email was read from, kept once something from it is
added to stock, linked to what it added. Only administrators can see them.

Files are kept in /config/crow_food/receipts/<id>/ (not /config/www, which anyone can open). Each file's address
is signed like product photos, and only administrators are given the addresses.

Every night at 1:15 am pictures are made smaller (WebP, still sharp enough to read the small print), receipts
older than the chosen months are deleted, and any left behind by a receipt that was read but never added are
cleared away.
"""
from __future__ import annotations

import io
import logging
import re
import shutil
import time
from pathlib import Path

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .store import CrowFood, FoodError, new_id, now_ms

_LOGGER = logging.getLogger(__name__)

MAX_FILES = 12
MAX_TOTAL = 60 * 1024 * 1024
PENDING_FOR = 12 * 3600 * 1000       # read but never added: cleared after this
WIDTH = 1500                          # a tall receipt: this wide at most (its length follows)
SIDE = 2200                           # anything else: its longest side
QUALITY = 74
TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heic",
         "application/pdf": ".pdf", "text/plain": ".txt"}
SERVE = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic", ".pdf": "application/pdf",
         ".txt": "text/plain; charset=utf-8"}


def folder(food: CrowFood) -> Path:
    return food.base / "receipts"


def _kind(name: str, ctype: str, head: bytes) -> str:
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\xff\xd8"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[8:12] == b"WEBP":
        return ".webp"
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return ".heic"
    ext = TYPES.get((ctype or "").split(";")[0].strip().lower())
    if ext:
        return ext
    m = re.search(r"\.(jpe?g|png|webp|heic|pdf|txt)$", name or "", re.I)
    return {"jpeg": ".jpg"}.get(m.group(1).lower(), "." + m.group(1).lower()) if m else ""


def on(food: CrowFood) -> bool:
    return (food.settings.get("receipts") or {}).get("keep", True) is not False


async def async_upload(hass: HomeAssistant, food: CrowFood, uid: str, files: list[tuple[str, str, bytes]]) -> str:
    """A receipt being read: its files kept for now (it's only kept for good once something is added from it)."""
    if not on(food):
        raise FoodError("off", "Keeping receipts is switched off.")
    files = [f for f in files if f[2]][:MAX_FILES]
    if not files:
        raise FoodError("empty", "No receipt was sent.")
    if sum(len(f[2]) for f in files) > MAX_TOTAL:
        raise FoodError("too_big", "That receipt is too big to keep.")
    rid = new_id()
    out = []
    for n, (name, ctype, raw) in enumerate(files):
        ext = _kind(name, ctype, raw[:16])
        if not ext:
            continue
        out.append({"n": n, "ext": ext, "size": len(raw), "orig": len(raw)})

    def write() -> None:
        d = folder(food) / rid
        d.mkdir(parents=True, exist_ok=True)
        for f in out:
            (d / f"{f['n']}{f['ext']}").write_bytes(files[f["n"]][2])

    if not out:
        raise FoodError("type", "That isn't a picture or a PDF.")
    await hass.async_add_executor_job(write)
    food.data.setdefault("receipts", {})[rid] = {"id": rid, "at": now_ms(), "by": uid, "pending": True, "files": out}
    food._save()
    return rid


def confirm(food: CrowFood, rid: str | None, shop: str | None, total: float, items: list[dict], log_ids: list[str]) -> None:
    """Something was added from it: kept, with the shop, the total and what it added."""
    r = (food.data.get("receipts") or {}).get(rid or "")
    if not r:
        return
    r.pop("pending", None)
    r["shop"] = shop
    if total:
        r["total"] = round(float(total), 2)
    r["items"] = list(dict.fromkeys([*(r.get("items") or []), *[i["id"] for i in items]]))[:400]
    r["logs"] = list(dict.fromkeys([*(r.get("logs") or []), *log_ids]))[:400]
    r["products"] = list(dict.fromkeys([*(r.get("products") or []), *[i["product"] for i in items if i.get("product")]]))[:400]
    r["count"] = len(r["items"])
    want = set(log_ids)
    for log in food.data.get("logs", [])[-(len(log_ids) * 2 + 20):]:
        if log.get("id") in want:
            log["receipt"] = rid
    for it in items:
        row = food.data["items"].get(it["id"])
        if row is not None:
            row["receipt"] = rid
    food._save()


async def async_delete(hass: HomeAssistant, food: CrowFood, rid: str) -> None:
    rec = (food.data.get("receipts") or {}).pop(rid, None)
    if rec is None:
        return
    for it in food.data["items"].values():
        if it.get("receipt") == rid:
            it.pop("receipt", None)
    await hass.async_add_executor_job(shutil.rmtree, folder(food) / rid, True)
    food._save()


def file_url(food: CrowFood, rid: str, f: dict) -> str:
    key = f"receipt:{rid}:{f['n']}"
    return f"/api/{DOMAIN}/receipt/{rid}/{f['n']}/{food._sig(key)}{f['ext']}?v={f.get('v', 0)}"


def row_out(food: CrowFood, r: dict) -> dict:
    shop = food.data["shops"].get(r.get("shop") or "") or {}
    names = [food.data["products"][p]["name"] for p in r.get("products") or [] if p in food.data["products"]]
    return {"id": r["id"], "at": r["at"], "by": r.get("by"), "shop": r.get("shop"), "shop_name": shop.get("name") or "",
            "total": r.get("total"), "count": r.get("count", 0), "names": names[:60], "items": r.get("items") or [],
            "products": [{"id": p, "name": food.data["products"][p]["name"]} for p in r.get("products") or [] if p in food.data["products"]][:60],
            "logs": r.get("logs") or [],
            "files": [{"n": f["n"], "ext": f["ext"], "size": f["size"], "url": file_url(food, r["id"], f),
                       "kind": "pdf" if f["ext"] == ".pdf" else "text" if f["ext"] == ".txt" else "image"} for f in r["files"]]}


def listing(food: CrowFood) -> dict:
    recs = sorted((r for r in (food.data.get("receipts") or {}).values() if not r.get("pending")), key=lambda r: -r["at"])
    return {"receipts": [row_out(food, r) for r in recs], "keep": on(food),
            "months": int((food.settings.get("receipts") or {}).get("months", 12) or 0)}


def stats(food: CrowFood) -> dict:
    recs = [r for r in (food.data.get("receipts") or {}).values() if not r.get("pending")]
    files = [f for r in recs for f in r["files"]]
    return {"on": on(food), "count": len(recs), "size": sum(f["size"] for f in files),
            "waiting": sum(1 for f in files if f["ext"] in (".jpg", ".png", ".heic") and not f.get("opt")),
            "last_run": food.data.get("receipts_ran")}


# ── Overnight: smaller pictures, old receipts deleted ───────────────────────────
def _shrink(src: Path, dst: Path) -> int | None:
    """A receipt picture as WebP, still readable: None when it can't be opened (or isn't any smaller)."""
    from PIL import Image, ImageOps

    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            w, h = im.size
            k = min(1.0, WIDTH / w, 16000 / h) if h >= w * 1.4 else min(1.0, SIDE / max(w, h))
            if k < 1:
                im = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=QUALITY, method=4)
    except Exception:  # noqa: BLE001 - HEIC without support, a broken file
        return None
    data = buf.getvalue()
    if len(data) >= src.stat().st_size:
        return None
    dst.write_bytes(data)
    return len(data)


async def async_nightly(hass: HomeAssistant, food: CrowFood, deadline: float | None = None) -> dict:
    recs = food.data.setdefault("receipts", {})
    done = {"smaller": 0, "deleted": 0, "saved": 0}
    # left behind by a receipt that was read but nothing added
    for rid in [k for k, r in recs.items() if r.get("pending") and now_ms() - r["at"] > PENDING_FOR]:
        await async_delete(hass, food, rid)
    # older than the chosen months (0: kept)
    months = int((food.settings.get("receipts") or {}).get("months", 12) or 0)
    if months:
        cutoff = now_ms() - months * 30.44 * 86400000
        for rid in [k for k, r in recs.items() if r["at"] < cutoff]:
            await async_delete(hass, food, rid)
            done["deleted"] += 1
    # pictures made smaller, one at a time
    for r in list(recs.values()):
        if r.get("pending"):
            continue
        for f in r["files"]:
            if deadline and time.time() > deadline:
                break
            if f["ext"] not in (".jpg", ".png", ".heic") or f.get("opt"):
                continue
            d = folder(food) / r["id"]
            src, dst = d / f"{f['n']}{f['ext']}", d / f"{f['n']}.webp"
            size = await hass.async_add_executor_job(_shrink, src, dst) if src.exists() else None
            f["opt"] = True
            if size:
                await hass.async_add_executor_job(src.unlink)
                done["saved"] += f["size"] - size
                f.update({"ext": ".webp", "size": size, "v": f.get("v", 0) + 1})
                done["smaller"] += 1
            food._save()
    food.data["receipts_ran"] = now_ms()
    food._save()
    if done["smaller"] or done["deleted"]:
        _LOGGER.debug("Crow Food receipts overnight: %s", done)
    return done


def start(hass: HomeAssistant, food: CrowFood, entry) -> None:
    from homeassistant.helpers.event import async_track_time_change

    @callback
    def _night(_now) -> None:
        # until 1:28, before the product photos start at 1:30
        hass.async_create_background_task(async_nightly(hass, food, time.time() + 13 * 60), f"{DOMAIN} receipts")

    entry.async_on_unload(async_track_time_change(hass, _night, hour=1, minute=15, second=0))
