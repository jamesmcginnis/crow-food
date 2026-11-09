"""HTTP views: product photos and photo uploads.

Photos are kept in /config/crow_food/photos - deliberately not in /config/www, which is public at
/local/ to anyone. Each photo's address carries a signature made with the household's own secret,
so only the addresses Crow Food hands to signed-in cards work (and they can be cached, so lists
of products with photos load quickly).
"""
from __future__ import annotations

import logging
import os
import re
import tempfile

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .store import PHOTO_SIDE, CrowFood, FoodError

_LOGGER = logging.getLogger(__name__)
MAX_PHOTO = 25 * 1024 * 1024


def _food(hass: HomeAssistant) -> CrowFood | None:
    return hass.data.get(DOMAIN)


class PhotoView(HomeAssistantView):
    """GET /api/crow_food/photo/{photo}/{sig}.webp (or {sig}_t.webp for the small copy; .jpg for older photos)."""

    url = "/api/crow_food/photo/{photo}/{sig}"
    name = "api:crow_food:photo"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, photo: str, sig: str) -> web.StreamResponse:
        food = _food(self.hass)
        if not food:
            return web.Response(status=503)
        sig = re.sub(r"\.(jpg|jpeg|webp)$", "", sig)
        thumb = sig.endswith("_t")
        sig = sig.removesuffix("_t")
        if not food.check_sig(photo, sig):
            return web.Response(status=404)
        path = food.photo_path(photo, thumb)
        if not await self.hass.async_add_executor_job(os.path.isfile, path):
            if not thumb:
                return web.Response(status=404)
            path = food.photo_path(photo)
            if not await self.hass.async_add_executor_job(os.path.isfile, path):
                return web.Response(status=404)
        return web.FileResponse(path, headers={
            "Content-Type": "image/webp" if path.suffix == ".webp" else "image/jpeg",
            # the address changes (?v=) when a photo is made smaller, so it can be kept a long time
            "Cache-Control": "private, max-age=2592000, immutable",
            "X-Content-Type-Options": "nosniff",
        })


class PersonPicView(HomeAssistantView):
    """GET /api/crow_food/person/{image}/{sig}: a person's picture from Home Assistant's own uploads
    (/config/image/<id>), signed the same way as product photos."""

    url = "/api/crow_food/person/{image}/{sig}"
    name = "api:crow_food:person"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, image: str, sig: str) -> web.StreamResponse:
        food = _food(self.hass)
        if not food or not re.fullmatch(r"[0-9a-f]{8,64}", image) or not food.check_sig("person:" + image, sig):
            return web.Response(status=404)
        folder = self.hass.config.path("image", image)

        def pick() -> tuple[str | None, bytes]:
            for name in ("512x512", "256x256", "original"):
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    with open(path, "rb") as fh:
                        head = fh.read(16)
                    return path, head
            return None, b""

        path, head = await self.hass.async_add_executor_job(pick)
        if not path:
            return web.Response(status=404)
        kind = ("image/png" if head.startswith(b"\x89PNG") else "image/gif" if head.startswith(b"GIF8")
                else "image/webp" if head[8:12] == b"WEBP" else "image/jpeg")
        return web.FileResponse(path, headers={"Content-Type": kind, "Cache-Control": "private, max-age=86400",
                                               "X-Content-Type-Options": "nosniff"})


async def _read_part(request: web.Request, field: str, limit: int, to_file: bool = False):
    """The named file from a multipart upload (into memory, or a temporary file for big ones)."""
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            return None
        if part.name != field:
            continue
        size = 0
        if to_file:
            fd, path = tempfile.mkstemp(suffix=".upload")
            try:
                with os.fdopen(fd, "wb") as fh:
                    while chunk := await part.read_chunk(1024 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise FoodError("too_big", "That file is too big.")
                        fh.write(chunk)
            except Exception:
                os.unlink(path)
                raise
            return path
        buf = bytearray()
        while chunk := await part.read_chunk(256 * 1024):
            size += len(chunk)
            if size > limit:
                raise FoodError("too_big", "That photo is too big.")
            buf.extend(chunk)
        return bytes(buf)


DOC_SIDE = 2200


class ReceiptFileView(HomeAssistantView):
    """GET /api/crow_food/receipt/{rid}/{n}/{sig}.{ext}: a saved receipt's file (signed; only administrators are
    given the addresses)."""

    url = "/api/crow_food/receipt/{rid}/{n}/{sig}"
    name = "api:crow_food:receipt"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, rid: str, n: str, sig: str) -> web.StreamResponse:
        from . import saved_receipts

        food = _food(self.hass)
        if not food or not re.fullmatch(r"[0-9a-f]{16}", rid) or not n.isdigit():
            return web.Response(status=404)
        sig = re.sub(r"\.[a-z]{3,4}$", "", sig)
        if not food.check_sig(f"receipt:{rid}:{n}", sig):
            return web.Response(status=404)
        rec = (food.data.get("receipts") or {}).get(rid) or {}
        f = next((x for x in rec.get("files") or [] if str(x["n"]) == n), None)
        if not f:
            return web.Response(status=404)
        path = saved_receipts.folder(food) / rid / f"{n}{f['ext']}"
        if not await self.hass.async_add_executor_job(path.is_file):
            return web.Response(status=404)
        return web.FileResponse(path, headers={
            "Content-Type": saved_receipts.SERVE.get(f["ext"], "application/octet-stream"),
            "Content-Disposition": f'inline; filename="receipt-{n}{f["ext"]}"',
            "Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


class ReceiptUploadView(HomeAssistantView):
    """POST multipart with one or more 'file' parts: a receipt being read, kept for now. Answers {receipt}."""

    url = "/api/crow_food/receipt_upload"
    name = "api:crow_food:receipt_upload"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        from . import saved_receipts

        food = _food(self.hass)
        if not food:
            return self.json_message("Crow Food is still starting.", 503)
        files, total = [], 0
        try:
            reader = await request.multipart()
            while (part := await reader.next()) is not None:
                if part.name != "file":
                    continue
                buf = bytearray()
                while chunk := await part.read_chunk(256 * 1024):
                    total += len(chunk)
                    if total > saved_receipts.MAX_TOTAL:
                        raise FoodError("too_big", "That receipt is too big to keep.")
                    buf.extend(chunk)
                files.append((part.filename or "", part.headers.get("Content-Type", ""), bytes(buf)))
            rid = await saved_receipts.async_upload(self.hass, food, request["hass_user"].id, files)
        except FoodError as err:
            return self.json_message(str(err), 400)
        return self.json({"receipt": rid})


class PhotoUploadView(HomeAssistantView):
    """POST multipart with a 'photo' file. Answers {photo, photo_url, thumb_url}."""

    url = "/api/crow_food/upload"
    name = "api:crow_food:upload"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        food = _food(self.hass)
        if not food:
            return self.json_message("Crow Food is still starting.", 503)
        try:
            raw = await _read_part(request, "photo", MAX_PHOTO)
            if not raw:
                return self.json_message("No photo was sent.", 400)
            # a page of a receipt or an order (?doc=1) keeps more detail, so the writing can be read
            pid = await food.async_save_photo(raw, DOC_SIDE if request.query.get("doc") else PHOTO_SIDE)
        except FoodError as err:
            return self.json_message(str(err), 400)
        return self.json({"photo": pid, "photo_url": food.photo_url(pid), "thumb_url": food.photo_url(pid, True)})
