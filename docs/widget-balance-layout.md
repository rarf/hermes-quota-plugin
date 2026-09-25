# Balance widget and footer regression tests

The pane has one bounded scroll region for provider cards and notices. The header
and checked-time footer do not shrink. Card sizing and overflow rules are inline
because plugin-only Tailwind classes may not exist in the host's compiled CSS.

## Offline rendering

```sh
python -m unittest discover -s tests -p 'test_widget_balances.py'
node --check desktop/plugin.js
node --check tests/widget_renderer.cjs
```

Node.js 22 is used in CI. The renderer uses only Node built-ins, evaluates the
real widget, and stubs SDK/React hooks. It does not run effects, call providers,
read credentials or write settings. Tests fix the locale to English and timezone
to UTC so they do not depend on the developer's machine settings. They cover
balance headlines/chips, zero balances, explicit failures, currency separation,
percentage priority, full checked timestamps and scroll sizing contracts.

## Optional Chromium geometry

Prerequisites: Chromium with CDP enabled, the `browser-harness-js` CLI connected
to it, and the compiled Desktop host and SDK stylesheets. This test creates and
closes only its own blank tab. It uses synthetic data and never loads an account.

```sh
browser-harness-js 'await session.connect()'
QUOTA_WIDGET_CSS='/path/to/index.css:/path/to/sdk.css' \
  python tests/test_widget_layout.py
```

The CSS list uses the platform path separator (`:` on POSIX, `;` on Windows).
Without `QUOTA_WIDGET_CSS`, the suite skips this optional test. If explicitly
set, missing CSS or browser tooling fails rather than silently skipping.

The test renders clean and dense modes at widths 180, 240, 300 and 480 pixels,
with heights 100, 160, 240 and 600 pixels (32 cases). It checks actual geometry:
scrolling exists, the scroll region does not overlap the footer, cards contain
their text, and there is no horizontal overflow. Long and unbroken detail lines
exercise wrapping rather than relying on short idealized labels.

Optional environment variables:

- `QUOTA_WIDGET_SOURCE`: widget source to test instead of `desktop/plugin.js`.
- `QUOTA_WIDGET_SCREENSHOT`: output PNG path for the final layout case.

The geometry test is a host-CSS integration check, not a full React interaction
or end-to-end Desktop refresh test.
