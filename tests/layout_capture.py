"""Capture selected real-module surfaces as individual screenshots.

Usage: python tests/layout_capture.py OUT_DIR [--state normal] [--theme dark]
       [--width 400] [--surfaces route_live,route_health,...]

Writes OUT_DIR/<surface>-<state>-<theme>-<width>.png plus a JSON index.
Synthetic host, synthetic data; not the installed Hermes Desktop.
"""
import argparse
import json
import subprocess
from pathlib import Path

from layout_matrix import THEMES, chrome_args, chromium, page, surfaces_for


def trim(path, pad=12):
    """Crop the empty canvas below the content (headless window height is fixed)."""
    try:
        from PIL import Image
    except ImportError:
        return
    im = Image.open(path).convert('RGB')
    w, h = im.size
    px = im.load()
    bg = px[w - 2, h - 2]
    last = 0
    for y in range(h - 1, -1, -1):
        if any(px[x, y] != bg for x in range(0, w, 3)):
            last = y
            break
    if last and last + pad < h:
        im.crop((0, 0, w, last + pad)).save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('--state', default='normal')
    ap.add_argument('--theme', default='dark', choices=THEMES)
    ap.add_argument('--width', type=int, default=400)
    ap.add_argument('--surfaces', default='route_live,route_health,route_usage,route_kanban,route_settings')
    args = ap.parse_args()
    if not chromium():
        raise SystemExit('BLOCKED: no chromium/google-chrome binary on PATH')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    surfaces, errors = surfaces_for(args.state)
    index = {'state': args.state, 'theme': args.theme, 'width': args.width, 'render_errors': errors, 'shots': []}
    for name in args.surfaces.split(','):
        markup = surfaces.get(name)
        stem = f'{name}-{args.state}-{args.theme}-{args.width}'
        html_path = out / f'{stem}.html'
        html_path.write_text(page(args.theme, args.width, {name: markup}), encoding='utf-8')
        dom = subprocess.run(chrome_args(args.width, 2000) + ['--dump-dom', '--virtual-time-budget=3000',
                                                             html_path.as_uri()],
                             capture_output=True, text=True, timeout=120).stdout
        start = dom.find('<pre id="metrics" hidden="">')
        raw = dom[start:].split('>', 1)[1].split('</pre>', 1)[0] if start >= 0 else ''
        height = 900
        if raw:
            import html as htmllib
            height = max(400, min(json.loads(htmllib.unescape(raw))['pageHeight'] + 16, 7800))
        shot = out / f'{stem}.png'
        subprocess.run(chrome_args(args.width, height) + [f'--screenshot={shot}', html_path.as_uri()],
                       capture_output=True, timeout=180)
        if shot.exists():
            trim(shot)
        index['shots'].append({'surface': name, 'empty': not markup, 'png': shot.name if shot.exists() else None,
                               'height': height})
        print(stem, 'ok' if shot.exists() else 'MISSING', height, flush=True)
    (out / 'index.json').write_text(json.dumps(index, indent=1), encoding='utf-8')


if __name__ == '__main__':
    main()
