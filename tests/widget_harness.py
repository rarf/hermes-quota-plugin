"""Shared offline widget renderer for tests.

Runs the real `desktop/plugin.js` in Node through `tests/widget_renderer.cjs`
with the SDK/hook boundaries stubbed, so every helper the components call is
the shipped code, not a per-test shim.
"""
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / 'widget_renderer.cjs'


def render(**options):
    p = subprocess.run(
        ['node', str(HARNESS)], input=json.dumps(options), text=True,
        capture_output=True, timeout=20,
        env={**os.environ, 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'TZ': 'UTC'},
    )
    if p.returncode:
        raise AssertionError(p.stderr)
    return json.loads(p.stdout)


def nodes(tree):
    if isinstance(tree, dict):
        yield tree
        yield from nodes(tree.get('props', {}).get('children'))
    elif isinstance(tree, list):
        for item in tree:
            yield from nodes(item)


def tooltip_text(tree):
    """SDK Tip labels only: never confuse a native title with a working tip."""
    return '\n'.join(text(n['props']['label']) for n in nodes(tree) if n.get('type') == 'Tip')


def text(tree):
    if isinstance(tree, dict):
        return text(tree.get('props', {}).get('children'))
    if isinstance(tree, list):
        return ' '.join(text(v) for v in tree)
    return '' if tree is None or isinstance(tree, bool) else str(tree)
