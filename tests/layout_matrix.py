"""Layout A real-module matrix: the actual desktop/plugin.js in a synthetic host.

Renders every surface through tests/widget_monitor_ssr.cjs (real React SSR of
the real module, synthetic payloads only), wraps each in a synthetic host page
(theme tokens for light/dark, no external resources, strict CSP) and drives a
local headless Chromium for DOM layout metrics and screenshots.

This is NOT the installed/native Hermes Desktop: host components (Popover,
Tip, SegmentedControl, icons) are harness stand-ins and the host's Tailwind
utility CSS is approximated. It verifies the renderer's own markup/inline
layout at 300/320/400/full widths.

Usage: python tests/layout_matrix.py OUT_DIR [--no-screens]
Writes OUT_DIR/coverage.json, OUT_DIR/pages/*.html, OUT_DIR/screens/*.png.
"""
import html as htmllib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_multicard_pane import pool_data  # noqa: E402
from test_widget_accounts import accounts_payload  # noqa: E402
from test_widget_monitor import NOTIFY_STORAGE_DEFAULTS, iso, quota_payload, snapshot  # noqa: E402

SSR = HERE / 'widget_monitor_ssr.cjs'
WIDTHS = {'300': 300, '320': 320, '400': 400, 'full': 1280}
THEMES = ('light', 'dark')
STATES = ('normal', 'many', 'long', 'unknown', 'exhausted', 'stale', 'partial', 'error')
ROUTE_VIEWS = ['quota', 'live', 'health', 'usage', 'kanban', 'accounts']
DOCK_VIEWS = ROUTE_VIEWS + ['settings']
POPOVERS = ['popQuota', 'popAgents', 'popContext', 'popCost']

TOKENS = {
    'light': {'--ui-bg-editor': '#ffffff', '--ui-bg-elevated': '#f5f5f6', '--ui-bg-tertiary': '#ebebed',
              '--ui-text-primary': '#1d1d1f', '--ui-text-secondary': '#45454a', '--ui-text-tertiary': '#6e6e73',
              '--ui-text-quaternary': '#8e8e93', '--ui-stroke-secondary': '#d2d2d7', '--ui-stroke-tertiary': '#e5e5ea',
              '--ui-accent': '#0a66d8', '--ui-green': '#1f9d55', '--ui-yellow': '#b7791f', '--ui-orange': '#c05621',
              '--ui-red': '#c53030', '--ui-blue': '#2b6cb0', '--ui-cyan': '#0987a0', '--ui-purple': '#6b46c1',
              '--dt-midground-foreground': '#1d1d1f', '--chrome-action-hover': '#0000000f'},
    'dark': {'--ui-bg-editor': '#1e1e1e', '--ui-bg-elevated': '#262628', '--ui-bg-tertiary': '#2f2f32',
             '--ui-text-primary': '#ececec', '--ui-text-secondary': '#c4c4c6', '--ui-text-tertiary': '#98989d',
             '--ui-text-quaternary': '#6e6e73', '--ui-stroke-secondary': '#3a3a3c', '--ui-stroke-tertiary': '#2c2c2e',
             '--ui-accent': '#4c9aff', '--ui-green': '#48bb78', '--ui-yellow': '#ecc94b', '--ui-orange': '#ed8936',
             '--ui-red': '#fc8181', '--ui-blue': '#63b3ed', '--ui-cyan': '#4fd1c5', '--ui-purple': '#b794f4',
             '--dt-midground-foreground': '#ececec', '--chrome-action-hover': '#ffffff14'},
}

# A small approximation of the host's utility classes used by the plugin.
UTIL_CSS = r"""
.flex{display:flex}.inline-flex{display:inline-flex}.inline-block{display:inline-block}.flex-col{flex-direction:column}
.items-center{align-items:center}.h-full{height:100%}.w-full{width:100%}.gap-1{gap:4px}.gap-1\.5{gap:6px}.gap-2{gap:8px}
.px-1\.5{padding-inline:6px}.px-2{padding-inline:8px}.px-3{padding-inline:12px}.py-0\.5{padding-block:2px}.py-2{padding-block:8px}
.rounded{border-radius:4px}.rounded-md{border-radius:6px}.rounded-lg{border-radius:8px}.rounded-full{border-radius:999px}
.whitespace-nowrap{white-space:nowrap}.overflow-hidden{overflow:hidden}.tabular-nums{font-variant-numeric:tabular-nums}
.text-xs{font-size:12px}.text-\[0\.6875rem\]{font-size:11px}.text-\[0\.875rem\]{font-size:14px}.font-medium{font-weight:500}
.h-1\.5{height:6px}.w-1\.5{width:6px}.h-3\.5{height:14px}.w-3\.5{width:14px}.h-6{height:24px}.border{border:1px solid var(--ui-stroke-secondary)}
.border-dashed{border-style:dashed}.opacity-60{opacity:.6}.text-right{text-align:right}.select-none{user-select:none}
.text-\(--ui-text-tertiary\){color:var(--ui-text-tertiary)}.text-\(--ui-text-secondary\){color:var(--ui-text-secondary)}
/* Harness stand-ins for host Switch / SegmentedControl (tests/widget_monitor_ssr.cjs):
   approximate only, so owner screenshots show state; not the host's real styling. */
button[aria-checked]{width:26px;height:15px;border-radius:999px;background:var(--ui-stroke-secondary)!important;position:relative;flex-shrink:0}
button[aria-checked]::after{content:"";position:absolute;top:2px;inset-inline-start:2px;width:11px;height:11px;border-radius:999px;background:var(--ui-bg-editor)}
button[aria-checked="true"]{background:var(--ui-accent)!important}
button[aria-checked="true"]::after{inset-inline-start:13px}
[role=tablist]:has(> button[aria-pressed]){display:inline-flex;gap:1px;padding:1px;border-radius:6px;background:var(--ui-bg-tertiary)}
[role=tablist] > button[aria-pressed]{font-size:11px;padding:2px 7px;border-radius:5px;color:var(--ui-text-secondary)}
[role=tablist] > button[aria-pressed="true"]{background:var(--ui-bg-elevated);color:var(--ui-text-primary);font-weight:600}
input{font:inherit;font-size:12px;color:var(--ui-text-primary);background:var(--ui-bg-tertiary);border:1px solid var(--ui-stroke-secondary);border-radius:5px;padding:1px 6px;width:64px;text-align:end}
summary::-webkit-details-marker{display:none}
summary{list-style:none}.qm-sb-chev{display:inline-block}details[open]>summary .qm-sb-chev{transform:rotate(90deg)}
"""

METRICS_JS = r"""
(() => {
  const out = [];
  for (const box of document.querySelectorAll('[data-matrix-surface]')) {
    const r = box.getBoundingClientRect();
    let spill = 0;
    for (const el of box.querySelectorAll('*')) {
      const e = el.getBoundingClientRect();
      if (!e.width) continue;
      let clipped = false;
      for (let p = el.parentElement; p && p !== box; p = p.parentElement) {
        const o = getComputedStyle(p).overflowX;
        if (o !== 'visible') { clipped = true; break; }
      }
      if (!clipped && e.right > r.right + 1) spill++;
    }
    const q = s => box.querySelectorAll(s).length;
    out.push({surface: box.dataset.matrixSurface, width: Math.round(r.width), height: Math.round(r.height),
      overflowX: box.scrollWidth > box.clientWidth + 1, spill, accounts: q('[data-account]'),
      windowBars: q('[data-window-bar]'), cardWindows: q('[data-card-window]'), accountWindows: q('[data-account-window]'),
      chipMore: q('[data-chip-more]'), unavailable: q('[data-card-unavailable]'),
      textLen: (box.textContent || '').length});
  }
  document.getElementById('metrics').textContent = JSON.stringify({pageHeight: document.documentElement.scrollHeight, surfaces: out});
})();
"""


def merge(*payloads, **over):
    providers = {}
    for p in payloads:
        providers.update(p['providers'])
    base = {'fetched_at': iso(-30), 'age_s': 30, 'providers': providers}
    base.update(over)
    return base


def win(label, used, reset=7200):
    return {'label': label, 'used_percent': used, 'reset_at': iso(reset)}


def state_options(state):
    pool4 = pool_data((50.0, 70.0, 90.0, 20.0))
    opts = {'data': merge(quota_payload(), pool4), 'monitor': snapshot(), 'accounts': accounts_payload(), 'locale': 'en'}
    if state == 'many':
        labels = tuple(f'acct{i}.synthetic' for i in range(1, 9))
        opts['data'] = merge(quota_payload(), pool_data((5.0, 20, 35, 50, 65, 80, 95, 10), labels))
    elif state == 'long':
        labels = ('synthetic.account.with.a.really.long.name@example.invalid', 'beta', 'gamma', 'delta')
        d = merge(quota_payload(), pool_data((50.0, 70.0, 90.0, 20.0), labels))
        d['providers']['anthropic']['windows'].append(win('Weekly · every model incl. a very long window label', 33))
        opts.update(data=d, locale='de')
    elif state == 'unknown':
        d = merge(quota_payload())
        d['providers']['anthropic']['windows'] = [win('Current session', None), win('Weekly', 40)]
        d['providers']['anthropic:home']['windows'] = [win('Current session', None)]
        opts['data'] = d
    elif state == 'exhausted':
        d = merge(quota_payload(), pool_data((100.0, 100.0, 99.0, 100.0)))
        d['providers']['anthropic']['windows'] = [win('Current session', 100), win('Weekly', 100)]
        opts['data'] = d
    elif state == 'stale':
        opts['data'] = merge(quota_payload(), pool4, fetched_at=iso(-3600), age_s=3600)
    elif state == 'partial':
        opts['data'] = merge(quota_payload(), pool_data((50.0, None, 90.0, None)))
        opts['monitor'] = snapshot(errors={'health': 'collector timed out', 'kanban': 'board unreadable'})
    elif state == 'error':
        opts['quotaError'] = 'gateway unreachable'
    return opts


def ssr(opts, mode):
    storage = {'monitorEnabled': True, 'budgetDaily': 10, 'budgetMonthly': 40, 'statusbarMode': mode,
               **NOTIFY_STORAGE_DEFAULTS}
    payload = {**opts, 'storage': storage, 'views': ROUTE_VIEWS, 'surfaces': True, 'dockViews': DOCK_VIEWS}
    # Optional: render another plugin.js (e.g. the base commit) with the same harness.
    if os.environ.get('LAYOUT_SOURCE'):
        payload['source'] = os.environ['LAYOUT_SOURCE']
    p = subprocess.run(['node', str(SSR)], input=json.dumps(payload), text=True, capture_output=True, timeout=120,
                       env={**os.environ, 'TZ': 'UTC', 'LANG': 'en_US.UTF-8'})
    if p.returncode:
        raise RuntimeError(p.stderr[-2000:])
    return json.loads(p.stdout)


def surfaces_for(state):
    opts = state_options(state)
    all_run, worst_run = ssr(opts, 'all'), ssr(opts, 'worst')
    h = all_run['html']
    out = {f'route_{v}': h.get(v) for v in ROUTE_VIEWS}
    out['route_settings'] = h.get('settings')
    out.update({f'dock_{v}': h.get(f'dock_{v}') for v in DOCK_VIEWS})
    # The pane's own padding wraps Settings in the product (QuotaPane); the SSR
    # harness renders QuotaSettings bare, so add the same inset here for review.
    for key, pad in (('route_settings', '14px 20px 10px'), ('dock_settings', '10px 12px 8px')):
        if out.get(key):
            out[key] = f'<div style="padding:{pad};box-sizing:border-box">{out[key]}</div>'
    out['statusbar_all'] = h.get('statusbar')
    out['statusbar_worst'] = worst_run['html'].get('statusbar')
    out.update({p: h.get(p) for p in POPOVERS})
    errors = [e for e in all_run['errors'] + worst_run['errors'] if 'is being spread into JSX' not in e]
    return out, errors


def page(theme, width, surfaces):
    tokens = ';'.join(f'{k}:{v}' for k, v in TOKENS[theme].items())
    blocks = []
    for name, markup in surfaces.items():
        blocks.append(f'<section><h6>{htmllib.escape(name)}</h6><div data-matrix-surface="{name}" '
                      f'style="width:{width}px;background:var(--ui-bg-editor)">{markup or ""}</div></section>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<style>:root{{{tokens};color-scheme:{theme}}}body{{margin:8px;background:var(--ui-bg-tertiary);color:var(--ui-text-primary);
font:13px/1.4 system-ui,sans-serif}}section{{margin:0 0 14px}}h6{{margin:0 0 3px;font:600 10px monospace;color:var(--ui-text-quaternary)}}
button{{font:inherit;color:inherit;background:none;border:0}}svg[width="1em"]{{width:1em;height:1em}}{UTIL_CSS}</style></head>
<body>{''.join(blocks)}<pre id="metrics" hidden></pre><script>{METRICS_JS}</script></body></html>"""


def chromium():
    return shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome')


def chrome_args(width, height):
    return [chromium(), '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
            '--disable-extensions', '--disable-background-networking', f'--window-size={width + 16},{height}']


def main():
    out = Path(sys.argv[1]).resolve()
    screens = '--no-screens' not in sys.argv
    (out / 'pages').mkdir(parents=True, exist_ok=True)
    (out / 'screens').mkdir(parents=True, exist_ok=True)
    if not chromium():
        raise SystemExit('BLOCKED: no chromium/google-chrome binary on PATH')
    report = {'harness': 'synthetic host + real desktop/plugin.js (React SSR) + headless Chromium',
              'native_desktop': False, 'widths': list(WIDTHS), 'themes': list(THEMES), 'states': list(STATES),
              'cells': [], 'render_errors': {}}
    for state in STATES:
        surfaces, errors = surfaces_for(state)
        report['render_errors'][state] = errors
        for theme in THEMES:
            for wname, width in WIDTHS.items():
                stem = f'{state}-{theme}-{wname}'
                pg = out / 'pages' / f'{stem}.html'
                pg.write_text(page(theme, width, surfaces), encoding='utf-8')
                dom = subprocess.run(chrome_args(width, 2000) + ['--dump-dom', '--virtual-time-budget=3000', pg.as_uri()],
                                     capture_output=True, text=True, timeout=120).stdout
                start = dom.find('<pre id="metrics" hidden="">')
                raw = dom[start:].split('>', 1)[1].split('</pre>', 1)[0] if start >= 0 else ''
                metrics = json.loads(htmllib.unescape(raw)) if raw else {'pageHeight': 0, 'surfaces': []}
                shot = None
                if screens:
                    shot = out / 'screens' / f'{stem}.png'
                    height = max(800, min(int(metrics['pageHeight']) + 20, 30000))
                    subprocess.run(chrome_args(width, height) + [f'--screenshot={shot}', pg.as_uri()],
                                   capture_output=True, timeout=180)
                    shot = shot.name if shot.exists() else None
                for m in metrics['surfaces']:
                    m.update(state=state, theme=theme, widthName=wname, empty=not surfaces.get(m['surface']))
                    report['cells'].append(m)
                report.setdefault('screens', []).append(shot)
                print(f'{stem}: {len(metrics["surfaces"])} surfaces, h={metrics["pageHeight"]}, shot={bool(shot)}', flush=True)
    cells = report['cells']
    report['counts'] = {
        'cells': len(cells), 'surfaces': len({c['surface'] for c in cells}),
        'screenshots': sum(1 for s in report['screens'] if s),
        'empty_surfaces': sorted({f"{c['state']}:{c['surface']}" for c in cells if c['empty']}),
        'overflow_cells': sorted({f"{c['state']}-{c['theme']}-{c['widthName']}:{c['surface']}" for c in cells
                                  if (c['overflowX'] or c['spill']) and not c['surface'].startswith('statusbar')}),
        # The status bar is the host window's horizontal footer strip, not a
        # dock column; its clipping/scrolling at narrow windows is native.
        'statusbar_wider_than_viewport': sorted({f"{c['state']}-{c['theme']}-{c['widthName']}:{c['surface']}" for c in cells
                                                 if (c['overflowX'] or c['spill']) and c['surface'].startswith('statusbar')}),
        'render_error_count': sum(len(v) for v in report['render_errors'].values()),
    }
    (out / 'coverage.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    print(json.dumps(report['counts'], indent=1))


if __name__ == '__main__':
    main()
