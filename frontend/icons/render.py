"""Рисует PNG-иконки из logo.svg (запуск: python3 frontend/icons/render.py)."""
import pathlib
from playwright.sync_api import sync_playwright
here = pathlib.Path(__file__).parent
svg = (here / "logo.svg").read_text(encoding="utf-8")
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, size, maskable in (("icon-192.png", 192, False), ("icon-512.png", 512, False), ("apple-touch-icon.png", 180, False), ("favicon-32.png", 32, False), ("icon-maskable-512.png", 512, True)):
        pg = b.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
        full = svg.replace('rx="116"', 'rx="0"') if name == "apple-touch-icon.png" else svg   # iOS сам скругляет углы
        inner = f'<div style="width:{size}px;height:{size}px">{full.replace("<svg ", "<svg width=%d height=%d " % (size, size), 1)}</div>'
        if maskable:   # иконка с запасом по краям: круглая/скруглённая маска не обрежет рисунок
            inner = f'<div style="width:{size}px;height:{size}px;background:#6672f0;display:grid;place-items:center"><div style="width:{int(size*.72)}px;height:{int(size*.72)}px">{svg.replace("<svg ", "<svg width=%d height=%d " % (int(size*.72), int(size*.72)), 1)}</div></div>'
        pg.set_content(f'<body style="margin:0;background:transparent">{inner}</body>')
        pg.screenshot(path=str(here / name), omit_background=not maskable, clip={"x": 0, "y": 0, "width": size, "height": size})
        pg.close()
    b.close()
