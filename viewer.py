"""liteview — ultra-lightweight image viewer (Windows 10+).

Usage:
    uv run viewer.py [image_path]
    run.bat [image_path]

Controls:
    Wheel          zoom in / out (toward cursor)
    Left-drag      pan when zoomed
    Double-click   fit / 100%
    Space / ->     next image in folder
    <-             previous image in folder
    Esc / q        quit
"""

from __future__ import annotations

import math
import queue
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import pygame
from PIL import Image, ImageFile
from pygame.constants import DOUBLEBUF, RESIZABLE

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 200_000_000

ZOOM_STEP = 1.25
MIN_SCALE = 0.02
MAX_SCALE = 32.0
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".pcx"}
MIP_MAX_SIDE = 2048
BG = (17, 17, 17)
CLICK_MS = 400
CLICK_DIST = 6
MIP_DEFER_MS = 120

# Prefetch ±N neighbors. CACHE_MAX must cover current + 2*N + slack.
# Parallel decode is the big win: 4-8 JPEG workers ≈ 3-4x wall-clock vs serial.
PREFETCH_RADIUS = 5
CACHE_MAX = 12
PREFETCH_THREADS = 6


def enable_win10_dpi_awareness() -> None:
    """Keep window 1:1 on scaled displays (Win10+)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


@dataclass
class CachedImage:
    # True pixel size of the photo (never the preview size).
    native_width: int
    native_height: int
    complete: bool = False  # False = low-res preview only
    surface: pygame.Surface | None = None
    mips: list[pygame.Surface] | None = None
    level_bytes: list[bytes] | None = field(default=None, repr=False)
    level_sizes: list[tuple[int, int]] | None = None

    @property
    def is_ready(self) -> bool:
        return self.surface is not None

    @property
    def is_prefetched(self) -> bool:
        return self.surface is not None or self.level_bytes is not None


class ImageCache:
    """LRU cache of decoded images. Thread-safe. Max CACHE_MAX entries.

    The currently displayed path is pinned and never evicted.
    """

    def __init__(self, max_size: int = CACHE_MAX) -> None:
        self.max_size = max_size
        self._lock = threading.Lock()
        self._data: OrderedDict[str, CachedImage] = OrderedDict()
        self._pinned: str | None = None

    @staticmethod
    def _key(path: Path) -> str:
        return str(path)

    def pin(self, path: Path) -> None:
        with self._lock:
            self._pinned = self._key(path)

    def get(self, path: Path) -> CachedImage | None:
        k = self._key(path)
        with self._lock:
            e = self._data.get(k)
            if e is not None:
                self._data.move_to_end(k)
            return e

    def put_surface(self, path: Path, surface: pygame.Surface, mips: list[pygame.Surface] | None, complete: bool = True) -> None:
        k = self._key(path)
        with self._lock:
            self._data[k] = CachedImage(
                native_width=surface.get_width(),
                native_height=surface.get_height(),
                complete=complete,
                surface=surface,
                mips=mips,
            )
            self._data.move_to_end(k)
            self._evict_locked()

    def put_preview(self, path: Path, native_w: int, native_h: int, data: bytes, size: tuple[int, int]) -> None:
        """Store a fast low-res preview. Does not overwrite a complete entry."""
        k = self._key(path)
        with self._lock:
            existing = self._data.get(k)
            if existing is not None and existing.complete:
                self._data.move_to_end(k)
                return
            self._data[k] = CachedImage(
                native_width=native_w,
                native_height=native_h,
                complete=False,
                level_bytes=[data],
                level_sizes=[size],
            )
            self._data.move_to_end(k)
            self._evict_locked()

    def put_prefetched(self, path: Path, width: int, height: int, level_bytes: list[bytes], level_sizes: list[tuple[int, int]]) -> None:
        k = self._key(path)
        with self._lock:
            self._data[k] = CachedImage(
                native_width=width,
                native_height=height,
                complete=True,
                level_bytes=level_bytes,
                level_sizes=level_sizes,
            )
            self._data.move_to_end(k)
            self._evict_locked()

    def _evict_locked(self) -> None:
        while len(self._data) > self.max_size:
            for k in list(self._data.keys()):
                if k != self._pinned:
                    del self._data[k]
                    break
            else:
                break

    def materialize(self, path: Path) -> CachedImage | None:
        """Turn prefetch bytes into pygame surfaces (main thread).

        Surfaces share the raw level buffers via frombuffer — no extra
        full-size copy, and nearly free compared to a JPEG decode.
        """
        k = self._key(path)
        with self._lock:
            e = self._data.get(k)
            if e is None:
                return None
            if e.surface is not None:
                self._data.move_to_end(k)
                return e
            payload = e.level_bytes
            sizes = e.level_sizes
            if not payload or not sizes:
                return e

        mips = [pygame.image.frombuffer(b, sz, "RGB") for b, sz in zip(payload, sizes)]
        surf = mips[0]

        with self._lock:
            e = self._data.get(k)
            if e is None:
                return None
            e.surface = surf
            e.mips = mips
            # Keep level_bytes: frombuffer surfaces alias those buffers.
            self._data.move_to_end(k)
        return e

    def has_raw_or_surface(self, path: Path) -> bool:
        e = self.get(path)
        return e is not None and e.is_prefetched

    def has_complete(self, path: Path) -> bool:
        e = self.get(path)
        return e is not None and e.complete and e.is_prefetched


def _find_turbojpeg():
    """Load libjpeg-turbo if present (faster, especially in parallel)."""
    try:
        from turbojpeg import TurboJPEG
    except Exception:
        return None
    candidates = [
        Path(__file__).resolve().parent / "libjpeg-turbo" / "bin" / "turbojpeg.dll",
        Path(__file__).resolve().parent / "turbojpeg.dll",
    ]
    for p in candidates:
        if p.is_file():
            try:
                return TurboJPEG(str(p))
            except Exception:
                pass
    try:
        return TurboJPEG()
    except Exception:
        return None


_TURBO = _find_turbojpeg()
_TJPF_RGB = 0
if _TURBO is not None:
    try:
        from turbojpeg import TJPF_RGB as _TJPF_RGB
    except Exception:
        _TJPF_RGB = 0


def _levels_from_rgb_bytes(data: bytes, width: int, height: int) -> tuple[list[bytes], list[tuple[int, int]]]:
    """Build power-of-two mip raw RGB levels from a full RGB buffer.

    Level 0 aliases `data` (no extra full-size copy).
    """
    img = Image.frombytes("RGB", (width, height), data)
    level_bytes: list[bytes] = [data]
    level_sizes: list[tuple[int, int]] = [(width, height)]
    cur = img
    while max(cur.size) > MIP_MAX_SIDE:
        cur = cur.reduce(2)
        level_bytes.append(cur.tobytes())
        level_sizes.append(cur.size)
    return level_bytes, level_sizes


def _native_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size


def _pick_decode_den(nw: int, nh: int, view_w: int, view_h: int) -> int:
    """Choose JPEG IDCT scale 1/den so the result still fills the window.

    den=1 means full resolution. Small/medium photos are never downsampled
    below window size — only very large ones use 1/2 or 1/4.
    """
    if nw <= 0 or nh <= 0:
        return 1
    need = int(max(view_w, view_h, 1) * 1.5)  # slight zoom headroom
    long_side = max(nw, nh)
    for den in (8, 4, 2, 1):
        if long_side / den >= need:
            return den
    return 1


def _decode_sized(path: Path, den: int = 4) -> tuple[int, int, bytes, tuple[int, int]]:
    """One-shot decode at 1/den scale (libjpeg IDCT scaling).

    Returns (native_w, native_h, rgb_bytes, (w, h)).
    """
    nw, nh = _native_size(path)
    den = max(1, min(8, den))
    if den == 1:
        # Full-res path still uses the same return shape; mips are built
        # separately by the caller when needed.
        pass
    if _TURBO is not None and path.suffix.lower() in {".jpg", ".jpeg"}:
        raw = path.read_bytes()
        arr = _TURBO.decode(raw, pixel_format=_TJPF_RGB, scaling_factor=(1, den))
        ph, pw, _ = arr.shape
        return nw, nh, arr.tobytes(), (pw, ph)

    with Image.open(path) as img:
        if den > 1:
            img.draft("RGB", (max(1, nw // den), max(1, nh // den)))
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        pw, ph = img.size
        return nw, nh, img.tobytes(), (pw, ph)


def _decode_full(path: Path) -> tuple[int, int, list[bytes], list[tuple[int, int]]]:
    """Full-resolution decode + mip levels (worker thread)."""
    if _TURBO is not None and path.suffix.lower() in {".jpg", ".jpeg"}:
        raw = path.read_bytes()
        arr = _TURBO.decode(raw, pixel_format=_TJPF_RGB)
        height, width, _ = arr.shape
        data = arr.tobytes()
        levels, sizes = _levels_from_rgb_bytes(data, width, height)
        return width, height, levels, sizes

    with Image.open(path) as img:
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        width, height = img.size
        data = img.tobytes()
        levels, sizes = _levels_from_rgb_bytes(data, width, height)
        return width, height, levels, sizes


def _decode_file(path: Path) -> tuple[int, int, list[bytes], list[tuple[int, int]]]:
    return _decode_full(path)


class Prefetcher:
    """Parallel decode workers + directional priority queue."""

    def __init__(self, cache: ImageCache) -> None:
        self.cache = cache
        self._lock = threading.Lock()
        self._direction = 1  # last navigation step sign
        self._q: queue.PriorityQueue[tuple[int, int, Path, bool]] = queue.PriorityQueue()
        self._seq = 0
        self._inflight: dict[str, threading.Event] = {}
        self._queued: set[str] = set()
        # Display size used to pick decode resolution (updated by Viewer).
        self.view_w = 1200
        self.view_h = 800
        self._threads = [
            threading.Thread(target=self._run, name=f"liteview-prefetch-{i}", daemon=True)
            for i in range(PREFETCH_THREADS)
        ]
        for t in self._threads:
            t.start()

    def set_view_size(self, w: int, h: int) -> None:
        self.view_w = max(1, w)
        self.view_h = max(1, h)

    def set_direction(self, step: int) -> None:
        if step:
            self._direction = 1 if step > 0 else -1

    def prioritize(self, path: Path, full: bool = False) -> None:
        """Queue this file at the front. full=True requests a full-res decode."""
        if full and self.cache.has_complete(path):
            return
        if not full and self.cache.has_raw_or_surface(path):
            return
        key = str(path)
        with self._lock:
            if key in self._inflight:
                return
            self._seq += 1
            seq = self._seq
            self._queued.add(key)
        self._q.put((0, seq, path, full))

    def schedule_neighbors(self, current: Path) -> None:
        """Prefetch ±N neighbors at DISPLAY scale only (one cheap decode each).

        Full-res is decoded only for the photo the user actually opens/zooms.
        """
        folder = current.parent
        try:
            files = sorted(
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            )
        except OSError:
            return
        if not files:
            return
        try:
            idx = files.index(current)
        except ValueError:
            return

        with self._lock:
            direction = self._direction

        jobs: list[tuple[int, Path]] = []
        for d in range(1, PREFETCH_RADIUS + 1):
            for sign in (1, -1):
                prio = d * 10 + (0 if sign == direction else 5)
                jobs.append((prio, files[(idx + sign * d) % len(files)]))
        jobs.sort(key=lambda x: x[0])

        for prio, p in jobs:
            if not self.cache.has_raw_or_surface(p):
                with self._lock:
                    self._seq += 1
                    seq = self._seq
                    self._queued.add(str(p))
                self._q.put((prio, seq, p, False))

    def wait_if_decoding(self, path: Path, timeout: float = 1.0) -> bool:
        """If this file is queued or being decoded, wait for that worker.

        Avoids a second cold decode on the main thread when the user
        arrows into an in-flight prefetch job.
        """
        key = str(path)
        if self.cache.has_raw_or_surface(path):
            return True
        with self._lock:
            pending = key in self._queued or key in self._inflight
        if not pending:
            return False

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cache.has_raw_or_surface(path):
                return True
            with self._lock:
                ev = self._inflight.get(key)
                still = key in self._queued or key in self._inflight
            if ev is not None:
                ev.wait(max(0.0, deadline - time.monotonic()))
                return self.cache.has_raw_or_surface(path)
            if not still:
                return self.cache.has_raw_or_surface(path)
            time.sleep(0.02)
        return self.cache.has_raw_or_surface(path)

    def _run(self) -> None:
        while True:
            _prio, _seq, path, want_full = self._q.get()
            key = str(path)
            with self._lock:
                self._queued.discard(key)
                if key in self._inflight:
                    continue
                if want_full and self.cache.has_complete(path):
                    continue
                if not want_full and self.cache.has_raw_or_surface(path):
                    continue
                ev = threading.Event()
                self._inflight[key] = ev
            try:
                if want_full:
                    if not self.cache.has_complete(path):
                        w, h, levels, sizes = _decode_full(path)
                        self.cache.put_prefetched(path, w, h, levels, sizes)
                else:
                    # One pass at a size that still fills the window.
                    if not self.cache.has_raw_or_surface(path):
                        nw, nh = _native_size(path)
                        den = _pick_decode_den(nw, nh, self.view_w, self.view_h)
                        if den == 1:
                            w, h, levels, sizes = _decode_full(path)
                            self.cache.put_prefetched(path, w, h, levels, sizes)
                        else:
                            nw, nh, data, size = _decode_sized(path, den=den)
                            self.cache.put_preview(path, nw, nh, data, size)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                    self._queued.discard(key)
                ev.set()


class Viewer:
    def __init__(self) -> None:
        pygame.display.set_caption("liteview")
        self.screen = pygame.display.set_mode((1200, 800), RESIZABLE | DOUBLEBUF)
        self.clock = pygame.time.Clock()

        self.cache = ImageCache()
        self.prefetcher = Prefetcher(self.cache)
        self.prefetcher.set_view_size(1200, 800)

        self.img: pygame.Surface | None = None
        self.mips: list[pygame.Surface] = []
        self.path: Path | None = None

        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0

        self._dragging = False
        self._drag_start = (0, 0, 0.0, 0.0)
        self._last_click = 0
        self._last_click_pos = (0, 0)
        self._dirty = True
        self._need_mips = False
        self._mips_at = 0
        self._pending: Path | None = None
        self.native_w = 1
        self.native_h = 1
        self._display_complete = True
        self._level_scales: list[float] = [1.0]

    # ---------- loading ----------

    def _show_entry(self, p: Path, entry: CachedImage | None) -> bool:
        if entry is None or not entry.is_prefetched:
            return False
        if not entry.is_ready:
            entry = self.cache.materialize(p)
        if entry is not None and entry.surface is not None:
            self._pending = None
            self._adopt(p, entry)
            return True
        return False

    def open_path(self, path: Path, keep_current: bool = False, async_ok: bool = False) -> bool:
        p = path.expanduser().resolve()
        if not p.is_file():
            self._status(f"not found: {p}")
            return False

        entry = self.cache.get(p)

        # Any cached payload is enough to display (full or display-sized).
        if entry is not None and entry.is_prefetched:
            return self._show_entry(p, entry)

        # Browse path: don't freeze the UI; swap in when the worker lands.
        if async_ok and keep_current and self.img is not None:
            self._pending = p
            self.prefetcher.prioritize(p, full=False)
            self._status(f"→ {p.name}")
            return False

        # First open: ONE decode at a size that fills this window.
        # Small photos are decoded full-res; only huge ones are scaled down.
        self._status(f"loading {p.name} ...")
        if not keep_current or self.img is None:
            self._draw_status_only()
        try:
            cw, ch = self.screen.get_size()
            self.prefetcher.set_view_size(cw, ch)
            nw, nh = _native_size(p)
            den = _pick_decode_den(nw, nh, cw, ch)
            if den == 1:
                w, h, levels, sizes = _decode_full(p)
                self.cache.put_prefetched(p, w, h, levels, sizes)
            else:
                nw, nh, data, size = _decode_sized(p, den=den)
                self.cache.put_preview(p, nw, nh, data, size)
        except Exception as exc:  # noqa: BLE001
            self._status(f"open failed: {exc}")
            return False

        entry = self.cache.get(p)
        return self._show_entry(p, entry)

    def _adopt(self, path: Path, entry: CachedImage | None, mips: list[pygame.Surface] | None = None) -> None:
        if entry is None or entry.surface is None:
            return

        same_photo = self.path == path and self.img is not None
        prev_scale = self.scale
        prev_ox = self.offset_x
        prev_oy = self.offset_y

        self.img = entry.surface
        self.path = path
        self.native_w = entry.native_width
        self.native_h = entry.native_height
        self._display_complete = entry.complete

        if mips is not None:
            self.mips = mips
            self._need_mips = True
            self._mips_at = pygame.time.get_ticks() + MIP_DEFER_MS
        elif entry.mips:
            self.mips = entry.mips
            self._need_mips = False
        else:
            self.mips = []
            self._need_mips = True
            self._mips_at = pygame.time.get_ticks() + MIP_DEFER_MS

        # Level scale = native_pixels / level_pixels (1.0, 2.0, 4.0, ...).
        if entry.level_sizes:
            self._level_scales = [entry.native_width / max(1, sz[0]) for sz in entry.level_sizes]
        elif self.mips:
            self._level_scales = [entry.native_width / max(1, m.get_width()) for m in self.mips]
        else:
            self._level_scales = [entry.native_width / max(1, entry.surface.get_width())]

        self.cache.pin(path)
        tag = "" if entry.complete else "  [fit-res]"
        pygame.display.set_caption(
            f"liteview — {path.name}  ({entry.native_width}x{entry.native_height}){tag}"
        )

        # Keep the framing when a preview is upgraded to full-res.
        if same_photo:
            self.scale = prev_scale
            self.offset_x = prev_ox
            self.offset_y = prev_oy
        else:
            self._fit_to_window()
        self._dirty = True
        self.prefetcher.schedule_neighbors(path)

    def _build_mips(self) -> None:
        if self.img is None or not self._need_mips:
            return
        surf = self.img
        mips = [surf]
        cur = surf
        while max(cur.get_width(), cur.get_height()) > MIP_MAX_SIDE:
            w = max(1, cur.get_width() // 2)
            h = max(1, cur.get_height() // 2)
            cur = pygame.transform.smoothscale(cur, (w, h))
            mips.append(cur)
        self.mips = mips
        self._need_mips = False
        self._level_scales = [self.native_w / max(1, m.get_width()) for m in mips]
        # Keep cache in sync so a later revisit reuses the pyramid.
        if self.path is not None:
            entry = self.cache.get(self.path)
            if entry is not None and entry.surface is self.img:
                entry.mips = mips

    def _status(self, msg: str) -> None:
        pygame.display.set_caption(f"liteview — {msg}")

    def _draw_status_only(self) -> None:
        self.screen.fill(BG)
        pygame.display.flip()

    # ---------- view math ----------

    def _fit_to_window(self) -> None:
        if self.img is None:
            return
        cw, ch = self.screen.get_size()
        self.scale = max(MIN_SCALE, min(MAX_SCALE, min(cw / self.native_w, ch / self.native_h)))
        self.offset_x = self.native_w / 2
        self.offset_y = self.native_h / 2

    def _reset_100(self) -> None:
        if self.img is None:
            return
        self.scale = 1.0
        self.offset_x = self.native_w / 2
        self.offset_y = self.native_h / 2
        self._dirty = True

    def _pick_mip(self, scale: float) -> tuple[int, float]:
        """Pick a level for scale (screen px / native px).

        Returns (index, native_px_per_level_px).
        """
        if not self._level_scales:
            return 0, 1.0
        best = 0
        best_ls = self._level_scales[0]
        for i, ls in enumerate(self._level_scales):
            if scale * ls <= 1.0:
                best, best_ls = i, ls
            else:
                break
        return best, best_ls

    # ---------- draw ----------

    def _draw(self) -> None:
        self.screen.fill(BG)
        if self.img is not None:
            cw, ch = self.screen.get_size()
            s = self.scale

            if self.mips:
                mip_idx, mip_scale = self._pick_mip(s)
                mip = self.mips[mip_idx]
            else:
                mip = self.img
                mip_scale = self._level_scales[0] if self._level_scales else 1.0

            mw, mh = mip.get_size()
            s_draw = s * mip_scale

            half_w = cw / (2 * s)
            half_h = ch / (2 * s)

            sx0 = max(0, int((self.offset_x - half_w) / mip_scale))
            sy0 = max(0, int((self.offset_y - half_h) / mip_scale))
            sx1 = min(mw, int((self.offset_x + half_w) / mip_scale) + 1)
            sy1 = min(mh, int((self.offset_y + half_h) / mip_scale) + 1)

            if sx0 < sx1 and sy0 < sy1:
                draw_w = max(1, int(round((sx1 - sx0) * s_draw)))
                draw_h = max(1, int(round((sy1 - sy0) * s_draw)))
                if draw_w > cw * 3 or draw_h > ch * 3:
                    draw_w = min(draw_w, cw * 3)
                    draw_h = min(draw_h, ch * 3)

                sub = mip.subsurface((sx0, sy0, sx1 - sx0, sy1 - sy0))
                if sub.get_size() == (draw_w, draw_h):
                    tile = sub
                else:
                    tile = pygame.transform.smoothscale(sub, (draw_w, draw_h))

                px = int(round((sx0 * mip_scale - self.offset_x) * s + cw / 2))
                py = int(round((sy0 * mip_scale - self.offset_y) * s + ch / 2))
                self.screen.blit(tile, (px, py))

        pygame.display.flip()
        self._dirty = False

    # ---------- input ----------

    def _zoom_at(self, factor: float, mx: int, my: int) -> None:
        if self.img is None:
            return
        new_scale = self.scale * factor
        if new_scale < MIN_SCALE or new_scale > MAX_SCALE:
            return
        cw, ch = self.screen.get_size()
        img_x = self.offset_x + (mx - cw / 2) / self.scale
        img_y = self.offset_y + (my - ch / 2) / self.scale
        self.scale = new_scale
        self.offset_x = img_x - (mx - cw / 2) / self.scale
        self.offset_y = img_y - (my - ch / 2) / self.scale
        self._dirty = True
        if self._need_mips and self.scale < 0.5:
            self._build_mips()
        self._request_full_if_needed()

    def _toggle_fit_or_100(self) -> None:
        if self.img is None:
            return
        cw, ch = self.screen.get_size()
        fit = min(cw / self.native_w, ch / self.native_h)
        if abs(self.scale - fit) < 0.02:
            self._reset_100()
        else:
            self._fit_to_window()
            self._dirty = True
        self._request_full_if_needed()

    def _navigate(self, step: int) -> None:
        if self.path is None:
            return
        folder = self.path.parent
        try:
            files = sorted(
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            )
        except OSError:
            return
        if not files:
            return
        try:
            idx = files.index(self.path)
        except ValueError:
            idx = 0
        self.prefetcher.set_direction(step)
        self.open_path(files[(idx + step) % len(files)], keep_current=True, async_ok=True)

    def _try_pending(self) -> None:
        """Swap in a browsed image once its display-sized buffer is ready."""
        # If full res landed for the on-screen photo (e.g. after zoom), adopt it.
        if self.path is not None and not self._display_complete:
            entry = self.cache.get(self.path)
            if entry is not None and entry.complete:
                entry = self.cache.materialize(self.path)
                if entry is not None and entry.surface is not None:
                    self._adopt(self.path, entry)
                return

        if self._pending is None:
            return
        p = self._pending
        entry = self.cache.get(p)
        if entry is None or not entry.is_prefetched:
            self.prefetcher.prioritize(p, full=False)
            self.prefetcher.schedule_neighbors(p)
            return
        entry = self.cache.materialize(p)
        if entry is not None and entry.surface is not None:
            self._pending = None
            self._adopt(p, entry)
        else:
            self.prefetcher.prioritize(p, full=False)

    def _request_full_if_needed(self) -> None:
        """Decode full res only when the user zooms past the current buffer."""
        if self.path is None or self._display_complete:
            return
        idx, ls = self._pick_mip(self.scale)
        # If we would upscale the best available level, we need full pixels.
        if self.scale * ls > 0.95:
            self.prefetcher.prioritize(self.path, full=True)

    # ---------- idle work ----------

    def _idle_work(self) -> None:
        """Build mips / materialize neighbor caches when the user pauses."""
        self._try_pending()

        now = pygame.time.get_ticks()
        if self._need_mips and now >= self._mips_at:
            self._build_mips()
            self._dirty = True
            return

        if self.path is None:
            return
        folder = self.path.parent
        try:
            files = sorted(
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            )
        except OSError:
            return
        if not files:
            return
        try:
            idx = files.index(self.path)
        except ValueError:
            return
        for d in range(1, PREFETCH_RADIUS + 1):
            for sign in (1, -1):
                cand = files[(idx + sign * d) % len(files)]
                entry = self.cache.get(cand)
                if entry is not None and entry.level_bytes is not None:
                    self.cache.materialize(cand)
                    return  # one per frame; stay responsive

    # ---------- main loop ----------

    def run(self) -> None:
        while True:
            for event in pygame.event.get():
                et = event.type

                if et == pygame.QUIT:
                    pygame.quit()
                    return

                if et == pygame.VIDEORESIZE:
                    w, h = max(200, event.w), max(200, event.h)
                    self.screen = pygame.display.set_mode((w, h), RESIZABLE | DOUBLEBUF)
                    self.prefetcher.set_view_size(w, h)
                    if self.img is not None:
                        self._fit_to_window()
                    self._dirty = True

                elif et == pygame.MOUSEWHEEL:
                    mx, my = pygame.mouse.get_pos()
                    self._zoom_at(ZOOM_STEP if event.y > 0 else 1 / ZOOM_STEP, mx, my)

                elif et == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    now = pygame.time.get_ticks()
                    dx = event.pos[0] - self._last_click_pos[0]
                    dy = event.pos[1] - self._last_click_pos[1]
                    if (
                        now - self._last_click <= CLICK_MS
                        and (dx * dx + dy * dy) <= CLICK_DIST * CLICK_DIST
                    ):
                        self._dragging = False
                        self._toggle_fit_or_100()
                    else:
                        self._dragging = True
                        self._drag_start = (
                            event.pos[0],
                            event.pos[1],
                            self.offset_x,
                            self.offset_y,
                        )
                    self._last_click = now
                    self._last_click_pos = event.pos

                elif et == pygame.MOUSEBUTTONUP and event.button == 1:
                    self._dragging = False

                elif et == pygame.MOUSEMOTION and self._dragging and self.img is not None:
                    x0, y0, ox, oy = self._drag_start
                    self.offset_x = ox - (event.pos[0] - x0) / self.scale
                    self.offset_y = oy - (event.pos[1] - y0) / self.scale
                    self._dirty = True

                elif et == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        pygame.quit()
                        return
                    if event.key in (pygame.K_SPACE, pygame.K_RIGHT, pygame.K_DOWN):
                        self._navigate(1)
                    elif event.key in (pygame.K_LEFT, pygame.K_UP):
                        self._navigate(-1)
                    elif event.key in (pygame.K_0, pygame.K_KP_0):
                        self._toggle_fit_or_100()

            if self._dirty:
                self._draw()

            self._try_pending()
            self._idle_work()
            self.clock.tick(60)


def main() -> None:
    enable_win10_dpi_awareness()
    pygame.init()
    pygame.display.init()

    viewer = Viewer()
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg and Path(arg).exists():
        viewer.open_path(Path(arg))
    else:
        found = next(
            (
                p
                for p in sorted(Path.cwd().iterdir())
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            ),
            None,
        )
        if found is not None:
            viewer.open_path(found)
        else:
            viewer._status("usage: viewer.py <image>")

    viewer.run()


if __name__ == "__main__":
    main()
