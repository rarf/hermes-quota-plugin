"""Desktop coverage for the Antigravity backend switch.

Runs the real `desktop/plugin.js` in the offline monitor renderer. Antigravity
is one pill inside the existing **Enabled providers** group, and that pill is
not a client-local hide flag: it reads and writes the same `antigravityEnabled`
setting the backend fetcher uses. These tests pin the placement, the state it
reads from `provider_switches`, the env override, the other providers' local
visibility behavior, and the CLI calls that persist and activate the switch.
They also pin the switch-visible regressions: off hides every Antigravity card
before the out-of-band refresh lands (without touching other providers), a
failed write names the backend error and reverts, and account-scoped stale hide
flags are cleared both at load and when the switch is turned on.
"""

import json
import unittest

from test_widget_monitor import run, snapshot
from widget_harness import nodes, text


def antigravity_status(enabled: bool, env=None) -> dict:
    env_override = env is not None
    source = "env" if env_override else ("config" if enabled else "default")
    return {
        "provider": "antigravity",
        "setting": "antigravityEnabled",
        "enabled": env if env_override else enabled,
        "config": enabled,
        "config_explicit": not env_override and enabled,
        "env": env,
        "env_override": env_override,
        "source": source,
        "warning": (
            "HERMES_QUOTA_ANTIGRAVITY_ENABLED overrides the Antigravity switch"
            if env_override else None
        ),
    }


def payload(enabled: bool, env=None, providers=None) -> dict:
    return {
        "fetched_at": "2030-01-01T00:00:00Z",
        "age_s": 4,
        "providers": providers if providers is not None else {
            "antigravity": {"label": "antigravity", "windows": []},
            "anthropic": {"label": "anthropic", "windows": []},
        },
        "provider_switches": {"antigravity": antigravity_status(enabled, env)},
    }


def providers_with_antigravity() -> dict:
    """Configured Antigravity rows, including a saved-account row."""
    windows = [{
        "label": "5h",
        "used_percent": 20,
        "reset_at": "2030-01-01T05:00:00Z",
    }]
    return {
        "antigravity": {"label": "antigravity", "windows": windows},
        "antigravity:work": {"label": "antigravity · Work", "windows": windows},
        "anthropic": {"label": "anthropic", "windows": windows},
    }


def provider_buttons(tree):
    return [
        node for node in nodes(tree)
        if node.get("type") == "button"
        and "data-provider-toggle" in node.get("props", {})
    ]


def button(tree, pid):
    for node in provider_buttons(tree):
        if node["props"]["data-provider-toggle"] == pid:
            return node
    raise AssertionError("provider button not found: %s" % pid)


def click_expression(pid: str) -> str:
    """Click one provider pill and return the recorded CLI argv calls."""
    return """(async () => {
      const tree = DisabledProvidersControl();
      const findButton = (node) => {
        if (!node || typeof node !== 'object') return null;
        if (node.type === 'button' && node.props &&
            node.props['data-provider-toggle'] === %s) return node;
        const kids = node.props && node.props.children;
        for (const child of Array.isArray(kids) ? kids : (kids == null ? [] : [kids])) {
          const hit = findButton(child);
          if (hit) return hit;
        }
        return null;
      };
      const control = findButton(tree);
      if (!control) return { error: 'provider button not found' };
      await control.props.onClick();
      return { pressed: control.props['aria-pressed'], title: control.props.title };
    })()""" % repr(pid)


class AntigravitySwitchRenderTests(unittest.TestCase):
    def test_antigravity_is_inside_the_enabled_providers_group(self) -> None:
        out = run(expr="DisabledProvidersControl()", data=payload(False))
        body = text(out["value"])
        self.assertIn("Enabled providers", body)
        self.assertIn("Antigravity", body)
        self.assertIn("Anthropic", body)
        cards = [
            node for node in nodes(out["value"])
            if node.get("props", {}).get("data-settings-card") == "Enabled providers"
        ]
        self.assertEqual(len(cards), 1)
        self.assertIn(
            "antigravity",
            [node["props"]["data-provider-toggle"] for node in provider_buttons(cards[0])],
        )

    def test_switch_reflects_the_same_key_the_backend_reads(self) -> None:
        off = run(expr="DisabledProvidersControl()", data=payload(False))
        self.assertIs(button(off["value"], "antigravity")["props"]["aria-pressed"], False)

        on = run(expr="DisabledProvidersControl()", data=payload(True))
        control = button(on["value"], "antigravity")
        self.assertIs(control["props"]["aria-pressed"], True)
        self.assertFalse(control["props"].get("disabled"))

    def test_absent_switch_state_fails_closed_to_off(self) -> None:
        data = payload(False)
        del data["provider_switches"]
        out = run(expr="DisabledProvidersControl()", data=data)
        control = button(out["value"], "antigravity")
        self.assertIs(control["props"]["aria-pressed"], False)
        self.assertFalse(control["props"].get("disabled"))
        self.assertIn("Off by default", text(out["value"]))

    def test_switch_channel_alone_still_offers_antigravity(self) -> None:
        out = run(
            expr="DisabledProvidersControl()",
            data={"provider_switches": {"antigravity": antigravity_status(True)}},
        )
        self.assertIs(button(out["value"], "antigravity")["props"]["aria-pressed"], True)
        self.assertIn("Enabled providers", text(out["value"]))

    def test_local_hide_flag_does_not_override_the_backend_state(self) -> None:
        out = run(
            expr="(() => { disabledProvidersAtom.set(['antigravity']); return DisabledProvidersControl(); })()",
            data=payload(True),
        )
        self.assertIs(button(out["value"], "antigravity")["props"]["aria-pressed"], True)

    def test_one_antigravity_pill_even_with_account_rows(self) -> None:
        providers = {
            "antigravity": {"label": "antigravity", "windows": []},
            "antigravity:work": {"label": "antigravity · Work", "windows": []},
            "anthropic": {"label": "anthropic", "windows": []},
        }
        out = run(expr="DisabledProvidersControl()", data=payload(False, providers=providers))
        antigravity = [
            node for node in provider_buttons(out["value"])
            if node["props"]["data-provider-toggle"] == "antigravity"
        ]
        self.assertEqual(len(antigravity), 1)

    def test_env_override_locks_the_button_and_explains_itself(self) -> None:
        blocked = run(expr="DisabledProvidersControl()", data=payload(False, env=False))
        control = button(blocked["value"], "antigravity")
        self.assertIs(control["props"]["aria-pressed"], False)
        self.assertTrue(control["props"].get("disabled"))
        body = text(blocked["value"])
        self.assertIn("HERMES_QUOTA_ANTIGRAVITY_ENABLED=0", body)
        self.assertIn("Unset it to use this switch", body)

        forced_on = run(expr="DisabledProvidersControl()", data=payload(False, env=True))
        self.assertIs(button(forced_on["value"], "antigravity")["props"]["aria-pressed"], True)

    def test_off_payload_hides_antigravity_cards_but_keeps_other_providers(self) -> None:
        out = run(
            component="pane",
            data=payload(False, providers=providers_with_antigravity()),
            monitor=snapshot(),
        )
        body = text(out["tree"])
        self.assertNotIn("Antigravity", body)
        self.assertIn("Anthropic", body)

    def test_switch_state_alone_can_hide_stale_antigravity_rows(self) -> None:
        # An optimistic off flip must win over a cached payload that still
        # carries configured Antigravity rows; other cards stay untouched.
        out = run(
            expr=(
                "(() => { setAntigravitySwitchState("
                "normalizeQuotaScope(host.state.focusedSessionOwner.get()), false);"
                " return QuotaPane(); })()"
            ),
            data=payload(True, providers=providers_with_antigravity()),
            monitor=snapshot(),
        )
        body = text(out["value"])
        self.assertNotIn("Antigravity", body)
        self.assertIn("Anthropic", body)

    def test_switch_on_payload_still_renders_antigravity(self) -> None:
        out = run(
            component="pane",
            data=payload(True, providers=providers_with_antigravity()),
            monitor=snapshot(),
        )
        self.assertIn("Antigravity", text(out["tree"]))

    def test_settings_have_no_separate_antigravity_card(self) -> None:
        out = run(component="settings", data=payload(False))
        card_titles = [
            node["props"]["data-settings-card"] for node in nodes(out["tree"])
            if "data-settings-card" in node.get("props", {})
        ]
        self.assertNotIn("Antigravity", card_titles)
        self.assertEqual(card_titles.count("Enabled providers"), 1)
        self.assertEqual(
            [node["props"]["data-provider-toggle"] for node in provider_buttons(out["tree"])],
            ["antigravity", "anthropic"],
        )


class AntigravitySwitchActionTests(unittest.TestCase):
    def _calls(self, pid: str, data):
        out = run(expr=click_expression(pid), data=data)
        return out.get("cli") or [], out

    def test_switching_on_writes_the_key_then_refreshes(self) -> None:
        calls, _out = self._calls("antigravity", payload(False))
        self.assertGreaterEqual(len(calls), 2, calls)
        self.assertEqual(
            calls[0]["argv"],
            ["--profile", "work", "quota", "switch", "antigravity", "on", "--json"],
        )
        self.assertEqual(calls[1]["argv"], ["--profile", "work", "quota", "refresh"])

    def test_switching_off_uses_the_same_backend_path(self) -> None:
        calls, _out = self._calls("antigravity", payload(True))
        self.assertGreaterEqual(len(calls), 2, calls)
        self.assertEqual(
            calls[0]["argv"],
            ["--profile", "work", "quota", "switch", "antigravity", "off", "--json"],
        )
        self.assertEqual(calls[1]["argv"], ["--profile", "work", "quota", "refresh"])

    def test_switching_on_clears_a_stale_local_hide_flag(self) -> None:
        clean = run(expr=click_expression("antigravity"), data=payload(False))
        self.assertNotIn("disabledProviders", clean["storage"])
        self.assertEqual(
            clean["cli"][0]["argv"],
            ["--profile", "work", "quota", "switch", "antigravity", "on", "--json"],
        )

        stale = run(
            expr="""(async () => {
              disabledProvidersAtom.set(['antigravity', 'cursor']);
              const tree = DisabledProvidersControl();
              const findButton = (node) => {
                if (!node || typeof node !== 'object') return null;
                if (node.type === 'button' && node.props &&
                    node.props['data-provider-toggle'] === 'antigravity') return node;
                const kids = node.props && node.props.children;
                for (const child of Array.isArray(kids) ? kids : (kids == null ? [] : [kids])) {
                  const hit = findButton(child);
                  if (hit) return hit;
                }
                return null;
              };
              await findButton(tree).props.onClick();
              return {
                atom: disabledProvidersAtom.get(),
                stored: CTX.storage.get('disabledProviders'),
              };
            })()""",
            data=payload(False),
        )["value"]
        self.assertEqual(stale["atom"], ["cursor"])
        self.assertEqual(stale["stored"], ["cursor"])

    def test_env_override_blocks_a_programmatic_click(self) -> None:
        calls, _out = self._calls("antigravity", payload(False, env=False))
        self.assertEqual(calls, [])

    def test_other_providers_keep_the_local_visibility_toggle(self) -> None:
        calls, out = self._calls("anthropic", payload(False))
        self.assertEqual(calls, [])
        self.assertEqual(out["storage"]["disabledProviders"], ["anthropic"])
        # The Antigravity backend write never entered the local hide list.
        self.assertNotIn("antigravity", out["storage"]["disabledProviders"])

    def test_switching_off_hides_the_card_before_the_refresh_returns(self) -> None:
        out = run(
            expr="""(async () => {
              const tree = DisabledProvidersControl();
              const findButton = (node) => {
                if (!node || typeof node !== 'object') return null;
                if (node.type === 'button' && node.props &&
                    node.props['data-provider-toggle'] === 'antigravity') return node;
                const kids = node.props && node.props.children;
                for (const child of Array.isArray(kids) ? kids : (kids == null ? [] : [kids])) {
                  const hit = findButton(child);
                  if (hit) return hit;
                }
                return null;
              };
              const scope = normalizeQuotaScope(host.state.focusedSessionOwner.get());
              const pending = findButton(tree).props.onClick();
              const immediate = {
                atom: antigravitySwitchState(scope),
                pane: QuotaPane(),
              };
              await pending;
              return {
                immediate,
                finalAtom: antigravitySwitchState(scope),
                finalPane: QuotaPane(),
              };
            })()""",
            data=payload(True, providers=providers_with_antigravity()),
            monitor=snapshot(),
        )["value"]
        self.assertIs(out["immediate"]["atom"], False)
        immediate_body = text(out["immediate"]["pane"])
        self.assertNotIn("Antigravity", immediate_body)
        self.assertIn("Anthropic", immediate_body)
        self.assertIs(out["finalAtom"], False)
        final_body = text(out["finalPane"])
        self.assertNotIn("Antigravity", final_body)
        self.assertIn("Anthropic", final_body)

    def test_switching_on_clears_account_scoped_stale_hide_flags(self) -> None:
        out = run(
            expr="""(async () => {
              disabledProvidersAtom.set(['antigravity', 'antigravity:work', 'cursor']);
              const tree = DisabledProvidersControl();
              const findButton = (node) => {
                if (!node || typeof node !== 'object') return null;
                if (node.type === 'button' && node.props &&
                    node.props['data-provider-toggle'] === 'antigravity') return node;
                const kids = node.props && node.props.children;
                for (const child of Array.isArray(kids) ? kids : (kids == null ? [] : [kids])) {
                  const hit = findButton(child);
                  if (hit) return hit;
                }
                return null;
              };
              await findButton(tree).props.onClick();
              return {
                atom: disabledProvidersAtom.get(),
                stored: CTX.storage.get('disabledProviders'),
              };
            })()""",
            data=payload(False),
        )["value"]
        self.assertEqual(out["atom"], ["cursor"])
        self.assertEqual(out["stored"], ["cursor"])

    def test_stale_account_scoped_flags_are_cleaned_at_load(self) -> None:
        out = run(
            expr="(() => ({ atom: disabledProvidersAtom.get(), stored: CTX.storage.get('disabledProviders') }))()",
            data=payload(False),
            storage={"disabledProviders": ["antigravity", "antigravity:work", "cursor"]},
        )["value"]
        self.assertEqual(out["atom"], ["cursor"])
        self.assertEqual(out["stored"], ["cursor"])

    def test_switch_error_names_the_cli_failure_and_reverts(self) -> None:
        out = run(
            expr="""(async () => {
              const tree = DisabledProvidersControl();
              const findButton = (node) => {
                if (!node || typeof node !== 'object') return null;
                if (node.type === 'button' && node.props &&
                    node.props['data-provider-toggle'] === 'antigravity') return node;
                const kids = node.props && node.props.children;
                for (const child of Array.isArray(kids) ? kids : (kids == null ? [] : [kids])) {
                  const hit = findButton(child);
                  if (hit) return hit;
                }
                return null;
              };
              await findButton(tree).props.onClick();
              const scope = normalizeQuotaScope(host.state.focusedSessionOwner.get());
              return {
                atom: antigravitySwitchState(scope),
                error: antigravitySwitchError(scope),
                settings: DisabledProvidersControl(),
              };
            })()""",
            data=payload(True),
            cliResults=[{
                "code": 1,
                "output": "gateway: reconnecting\n" + json.dumps({
                    "ok": False,
                    "error": "managed install: config is read-only",
                }),
            }],
        )["value"]
        self.assertIs(out["atom"], True)
        self.assertEqual(out["error"], "managed install: config is read-only")
        body = text(out["settings"])
        self.assertIn("Could not save the Antigravity switch", body)
        self.assertIn("managed install: config is read-only", body)



FIND_PILL = """
  const findPill = (node) => {
    if (!node || typeof node !== 'object') return null;
    if (node.type === 'button' && node.props &&
        node.props['data-provider-toggle'] === 'antigravity') return node;
    const kids = node.props && node.props.children;
    for (const child of Array.isArray(kids) ? kids : (kids == null ? [] : [kids])) {
      const hit = findPill(child);
      if (hit) return hit;
    }
    return null;
  };
"""
SCOPE = "normalizeQuotaScope(host.state.focusedSessionOwner.get())"


def switch_calls(out):
    return [c for c in out.get("cli") or [] if c["argv"][-4:-2] == ["switch", "antigravity"]]


class AntigravitySwitchSurfaceRaceTests(unittest.TestCase):
    """Two mounted controls (route page + docked pane) share one write slot."""

    def test_two_mounted_surfaces_never_issue_competing_writes(self) -> None:
        # The bug: `busy` was per-component `useState`, so after the route page
        # flipped the switch off, the docked pane (repainted with the optimistic
        # value) still accepted a click and sent the opposite write while the
        # first one was in flight. The backend then kept whichever landed last.
        out = run(
            expr="""(async () => {
              %s
              const route = DisabledProvidersControl();
              const first = findPill(route).props.onClick();
              const workScope = %s;
              const busyWhileFirst = antigravitySwitchBusy(workScope);
              // The other surface repaints with the optimistic value while the
              // write is still pending, exactly as a subscriber would.
              const dock = DisabledProvidersControl();
              const dockPill = findPill(dock);
              const dockSawOptimisticOff = dockPill.props['aria-pressed'] === false;
              const dockDisabled = Boolean(dockPill.props.disabled);
              const second = dockPill.props.onClick();
              const busyAfterSecondClick = antigravitySwitchBusy(workScope);
              await Promise.all([first, second]);
              const after = {
                state: antigravitySwitchState(workScope),
                busy: antigravitySwitchBusy(workScope),
              };
              // Once the write settles the slot is free again: one more click
              // is a real flip, not a permanently locked control.
              const settledDock = DisabledProvidersControl();
              await findPill(settledDock).props.onClick();
              return {
                busyWhileFirst,
                dockSawOptimisticOff,
                dockDisabled,
                busyAfterSecondClick,
                after,
                finalState: antigravitySwitchState(workScope),
              };
            })()""" % (FIND_PILL, SCOPE),
            data=payload(True, providers=providers_with_antigravity()),
        )
        value = out["value"]
        self.assertTrue(value["busyWhileFirst"])
        self.assertTrue(value["dockSawOptimisticOff"])
        self.assertTrue(value["dockDisabled"])
        self.assertTrue(value["busyAfterSecondClick"])
        self.assertIs(value["after"]["state"], False)
        self.assertFalse(value["after"]["busy"])
        self.assertIs(value["finalState"], True)

        switches = switch_calls(out)
        self.assertEqual(
            [call["argv"] for call in switches],
            [
                ["--profile", "work", "quota", "switch", "antigravity", "off", "--json"],
                ["--profile", "work", "quota", "switch", "antigravity", "on", "--json"],
            ],
            switches,
        )

    def test_one_write_slot_per_profile(self) -> None:
        # The lock is per profile: a pending write in one profile must not
        # freeze the switch of another, and its busy flag must not paint there.
        out = run(
            expr="""(() => {
              const workScope = {connectionId: 'local', profile: 'work'};
              const homeScope = {connectionId: 'local', profile: 'home'};
              const claimed = beginAntigravitySwitchWrite(workScope);
              const result = {
                work: antigravitySwitchBusy(workScope),
                home: antigravitySwitchBusy(homeScope),
                secondClaim: beginAntigravitySwitchWrite(workScope),
                homeClaim: beginAntigravitySwitchWrite(homeScope),
              };
              endAntigravitySwitchWrite(workScope);
              endAntigravitySwitchWrite(homeScope);
              return {...result, claimed, workAfter: antigravitySwitchBusy(workScope)};
            })()""",
            data=payload(True),
        )["value"]
        self.assertTrue(out["claimed"])
        self.assertTrue(out["work"])
        self.assertFalse(out["home"])
        self.assertFalse(out["secondClaim"])
        self.assertTrue(out["homeClaim"])
        self.assertFalse(out["workAfter"])


class AntigravitySwitchProfileIsolationTests(unittest.TestCase):
    """Optimistic and failure state belongs to the profile that produced it."""

    def test_optimistic_flip_does_not_cross_profiles(self) -> None:
        out = run(
            expr="""(() => {
              const owners = host.state.focusedSessionOwner;
              const workScope = %s;
              setAntigravitySwitchState(workScope, false);
              owners.set({connectionId: 'local', profile: 'home'});
              const homeScope = %s;
              return {
                work: antigravitySwitchState(workScope),
                home: antigravitySwitchState(homeScope),
                pane: QuotaPane(),
                pill: DisabledProvidersControl(),
              };
            })()""" % (SCOPE, SCOPE),
            dataByProfile={
                "work": payload(True, providers=providers_with_antigravity()),
                "home": payload(True, providers=providers_with_antigravity()),
            },
        )["value"]
        self.assertIs(out["work"], False)
        self.assertIsNone(out["home"])
        # Home still renders its own Antigravity rows and an "on" pill even
        # though work was just optimistically switched off.
        self.assertIn("Antigravity", text(out["pane"]))
        self.assertIs(button(out["pill"], "antigravity")["props"]["aria-pressed"], True)

    def test_profile_switch_does_not_adopt_the_previous_profiles_value(self) -> None:
        # A scope change is not a backend change: the previous profile's payload
        # must not be written into the new profile's slot.
        out = run(
            expr="""(() => {
              const owners = host.state.focusedSessionOwner;
              const workPill = DisabledProvidersControl();
              const workScope = %s;
              owners.set({connectionId: 'local', profile: 'home'});
              const homeControl = DisabledProvidersControl();
              const homeScope = %s;
              return {
                work: antigravitySwitchState(workScope),
                home: antigravitySwitchState(homeScope),
                workPill,
                homePill: homeControl,
              };
            })()""" % (SCOPE, SCOPE),
            dataByProfile={"work": payload(True), "home": payload(False)},
            runEffects=True,
        )["value"]
        self.assertIsNone(out["work"])
        self.assertIsNone(out["home"])
        self.assertIs(button(out["workPill"], "antigravity")["props"]["aria-pressed"], True)
        self.assertIs(button(out["homePill"], "antigravity")["props"]["aria-pressed"], False)

    def test_a_flip_writes_only_the_focused_profiles_switch(self) -> None:
        out = run(
            expr="""(async () => {
              %s
              const owners = host.state.focusedSessionOwner;
              const workScope = %s;
              owners.set({connectionId: 'local', profile: 'home'});
              const homeScope = %s;
              await findPill(DisabledProvidersControl()).props.onClick();
              return {
                work: antigravitySwitchState(workScope),
                home: antigravitySwitchState(homeScope),
              };
            })()""" % (FIND_PILL, SCOPE, SCOPE),
            dataByProfile={"work": payload(True), "home": payload(False)},
        )
        self.assertEqual(
            out["cli"][0]["argv"],
            ["--profile", "home", "quota", "switch", "antigravity", "on", "--json"],
        )
        self.assertIsNone(out["value"]["work"])
        self.assertIs(out["value"]["home"], True)

    def test_failure_stays_with_the_profile_that_failed(self) -> None:
        out = run(
            expr="""(async () => {
              %s
              const owners = host.state.focusedSessionOwner;
              const workScope = %s;
              await findPill(DisabledProvidersControl()).props.onClick();
              const workError = antigravitySwitchError(workScope);
              owners.set({connectionId: 'local', profile: 'home'});
              const homeScope = %s;
              return {
                workError,
                homeError: antigravitySwitchError(homeScope),
                homeState: antigravitySwitchState(homeScope),
                homePill: DisabledProvidersControl(),
              };
            })()""" % (FIND_PILL, SCOPE, SCOPE),
            dataByProfile={"work": payload(True), "home": payload(True)},
            cliResults=[{
                "code": 1,
                "output": json.dumps({
                    "ok": False,
                    "error": "managed install: config is read-only",
                }),
            }],
        )["value"]
        self.assertEqual(out["workError"], "managed install: config is read-only")
        self.assertEqual(out["homeError"], "")
        self.assertIsNone(out["homeState"])
        self.assertIs(button(out["homePill"], "antigravity")["props"]["aria-pressed"], True)



if __name__ == "__main__":
    unittest.main(verbosity=2)
