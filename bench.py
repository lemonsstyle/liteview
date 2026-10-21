"""Benchmarks for liteview open/draw/prefetch path."""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

import pygame

import viewer
from viewer import Viewer


def neighbor_paths(v: Viewer) -> list[Path]:
    assert v.path is not None
    files = sorted(p for p in v.path.parent.iterdir() if p.suffix.lower() in viewer.IMAGE_EXTS)
    idx = files.index(v.path)
    out = []
    for d in range(1, viewer.PREFETCH_RADIUS + 1):
        out.append(files[(idx + d) % len(files)])
        out.append(files[(idx - d) % len(files)])
    return out


def wait_prefetch(v: Viewer, timeout: float = 15.0) -> float:
    want = neighbor_paths(v)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if all(v.cache.has_raw_or_surface(p) for p in want):
            return time.perf_counter() - t0
        time.sleep(0.05)
    missing = sum(1 for p in want if not v.cache.has_raw_or_surface(p))
    print(f"  prefetch timeout, missing={missing}/{len(want)}")
    return time.perf_counter() - t0


def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("test")
    files = sorted(folder.glob("*.jpg"))
    print(f"folder={folder} total={len(files)}")
    print(f"config: radius={viewer.PREFETCH_RADIUS} threads={viewer.PREFETCH_THREADS} "
          f"cache_max={viewer.CACHE_MAX} turbo={viewer._TURBO is not None}")

    viewer.enable_win10_dpi_awareness()
    pygame.init()
    pygame.display.init()
    v = Viewer()

    t0 = time.perf_counter()
    v.open_path(files[0])
    v._draw()
    print(f"cold open: {time.perf_counter()-t0:.3f}s -> {v.path.name}")

    dt = wait_prefetch(v)
    n_cached = sum(1 for p in neighbor_paths(v) if v.cache.has_raw_or_surface(p))
    print(f"prefetch ±{viewer.PREFETCH_RADIUS} ready: {dt:.3f}s  ready={n_cached}  cache={len(v.cache._data)}")

    t0 = time.perf_counter()
    for _ in range(20):
        v._idle_work()
    print(f"idle materialize: {time.perf_counter()-t0:.3f}s")

    print("--- rapid next x8 (async: UI never freezes) ---")
    key_times, show_times = [], []
    for i in range(8):
        t0 = time.perf_counter()
        v._navigate(1)
        v._draw()
        dt_key = time.perf_counter() - t0
        # wait until pending image is on screen
        t1 = time.perf_counter()
        while v._pending is not None and time.perf_counter() - t1 < 5.0:
            v._try_pending()
            v._idle_work()
            time.sleep(0.01)
        dt_show = time.perf_counter() - t0
        key_times.append(dt_key)
        show_times.append(dt_show)
        mark = "instant" if dt_show < 0.2 else f"wait {dt_show-dt_key:.2f}s"
        print(f"  {v.path.name}: key={dt_key:.3f}s show={dt_show:.3f}s ({mark})")
    print(f"avg key response: {statistics.mean(key_times):.3f}s  avg show: {statistics.mean(show_times):.3f}s")
    print(f"show<0.2s: {sum(1 for x in show_times if x<0.2)}/8")

    print("--- after pause, rapid next x8 again ---")
    wait_prefetch(v, timeout=8)
    for _ in range(20):
        v._idle_work()
    key_times, show_times = [], []
    for i in range(8):
        t0 = time.perf_counter()
        v._navigate(1)
        v._draw()
        dt_key = time.perf_counter() - t0
        t1 = time.perf_counter()
        while v._pending is not None and time.perf_counter() - t1 < 5.0:
            v._try_pending()
            v._idle_work()
            time.sleep(0.01)
        dt_show = time.perf_counter() - t0
        key_times.append(dt_key)
        show_times.append(dt_show)
        mark = "instant" if dt_show < 0.2 else f"wait {dt_show-dt_key:.2f}s"
        print(f"  {v.path.name}: key={dt_key:.3f}s show={dt_show:.3f}s ({mark})")
    print(f"avg key response: {statistics.mean(key_times):.3f}s  avg show: {statistics.mean(show_times):.3f}s")
    print(f"show<0.2s: {sum(1 for x in show_times if x<0.2)}/8")

    total = 0
    for e in v.cache._data.values():
        if e.level_bytes:
            total += sum(len(b) for b in e.level_bytes)
        elif e.surface is not None:
            w, h = e.surface.get_size()
            total += w * h * 3
    print(f"cache={len(v.cache._data)}/{viewer.CACHE_MAX}  buffers≈{total/1024/1024:.0f}MB")
    assert len(v.cache._data) <= viewer.CACHE_MAX

    pygame.quit()
    print("OK")


if __name__ == "__main__":
    main()
