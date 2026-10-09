"""Offline Codex contracts; identities and credentials below are synthetic only."""
import base64
import dataclasses
import json
from io import BytesIO
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quota_providers import builtin, codex


def token(account, subject="synthetic-subject", exp=4102444800, nonce="one", email=None, **auth_claims):
    claims = {"sub": subject, "exp": exp, "nonce": nonce,
              "https://api.openai.com/auth": {"chatgpt_account_id": account, **auth_claims}}
    if email:
        claims["email"] = email
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "synthetic." + body + ".signature"


def auth_modules(rows, state=None):
    import tempfile
    constants = types.ModuleType("hermes_constants")
    import atexit
    constants.temp = tempfile.TemporaryDirectory()
    atexit.register(constants.temp.cleanup)
    home = Path(constants.temp.name)
    (home / "auth.json").write_text(json.dumps({
        "credential_pool": {"openai-codex": rows},
        "providers": {"openai-codex": state},
    }))
    constants.get_hermes_home = lambda: home
    constants.get_default_hermes_root = lambda: home
    usage = types.ModuleType("agent.account_usage")
    usage._codex_backend_urls = lambda base: ((base or "https://chatgpt.com/backend-api/codex").removesuffix('/codex') + '/wham/usage',)
    auth = types.ModuleType("hermes_cli.auth_codex")
    auth._codex_pool_route_base_url = lambda base: base
    auth._codex_base_url = lambda: "https://chatgpt.com/backend-api/codex"
    agent = types.ModuleType('agent')
    agent.account_usage = usage
    return {"hermes_constants": constants, "agent": agent, "agent.account_usage": usage,
            "hermes_cli.auth_codex": auth}


from contextlib import contextmanager


@contextmanager
def mock_http(**kwargs):
    """Offline httpx boundary, like the upstream tests; no installed deps needed."""
    from email.message import Message
    from urllib.error import HTTPError
    handler = mock.Mock(**kwargs)
    module = types.ModuleType('httpx')

    class StatusError(Exception):
        def __init__(self, status):
            self.response = types.SimpleNamespace(status_code=status)

    class Client:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers):
            header_map = Message()
            for key, value in headers.items():
                header_map[key] = value
            request = types.SimpleNamespace(url=url, headers=header_map)
            try:
                body = handler(request, timeout=self.timeout)
            except HTTPError as exc:
                exc.close()
                raise StatusError(exc.code) from None
            with body:
                raw = body.read()
            return types.SimpleNamespace(raise_for_status=lambda: None,
                                         json=lambda: json.loads(raw))

    module.Client = Client
    module.HTTPStatusError = StatusError
    module.TimeoutException = TimeoutError
    with mock.patch.dict(sys.modules, {'httpx': module}):
        yield handler


def payload(used):
    return {"plan_type": "plus", "rate_limit": {
        "primary_window": {"used_percent": used, "reset_at": 1900000000}},
        "additional_rate_limits": [{"limit_name": "GPT-5.3-Codex-Spark", "rate_limit": {
            "secondary_window": {"used_percent": 12, "reset_at": 1900100000}}}]}


class CodexAccountsTests(unittest.TestCase):
    def test_core_generated_email_label_is_not_exposed_but_custom_alias_is(self):
        rows = [
            {'access_token': token('a', email='private@example.invalid'), 'label': 'private@example.invalid'},
            {'access_token': token('b', email='other@example.invalid'), 'label': 'Work account'},
        ]
        with mock.patch.dict(sys.modules, auth_modules(rows)), mock_http(side_effect=lambda *a, **k: BytesIO(json.dumps(payload(21)).encode())):
            result = builtin._fetch_codex_with_models()
        self.assertEqual([account.label for account in result.accounts], ['Account 1', 'Work account'])
        self.assertNotIn('private@example.invalid', repr(result))

    def test_codex_residency_claim_is_sent_as_required_header(self):
        rows = [{'access_token': token('a', chatgpt_data_residency='eu')}]
        with mock.patch.dict(sys.modules, auth_modules(rows)), mock_http(return_value=BytesIO(json.dumps(payload(21)).encode())) as http:
            builtin._fetch_codex_with_models()
        self.assertEqual(http.call_args.args[0].headers.get('x-openai-internal-codex-residency'), 'eu')

    def test_primary_window_with_weekly_duration_is_labeled_weekly(self):
        raw = payload(21)
        raw['rate_limit'] = {
            'primary_window': {'used_percent': 21, 'limit_window_seconds': 604800},
            'secondary_window': {'used_percent': 42, 'limit_window_seconds': 18000},
        }
        raw['additional_rate_limits'] = []
        result = builtin._parse_codex_payload(raw)
        self.assertEqual([window.label for window in result.windows], ['Weekly', 'Session'])

    def test_saved_labels_follow_best_priority_even_with_replacement_token(self):
        rows = [
            {'access_token': token('a'), 'priority': 9, 'label': 'Later alias'},
            {'access_token': token('b'), 'priority': 2, 'label': '  Synthetic Blue  '},
            {'access_token': token('a', exp=1), 'priority': 0, 'label': 'Synthetic Amber'},
        ]
        with mock.patch.dict(sys.modules, auth_modules(rows)), mock_http(side_effect=lambda *a, **k: BytesIO(json.dumps(payload(21)).encode())):
            result = builtin._fetch_codex_with_models()
        self.assertEqual([a.label for a in result.accounts], ['Synthetic Amber', 'Synthetic Blue'])
        self.assertTrue(all(a.has_data() for a in result.accounts))

        # A blank best-priority label is an intentional fallback, not permission
        # to borrow a lower-priority alias from the replacement credential.
        rows[2]['label'] = ' \u202e '
        with mock.patch.dict(sys.modules, auth_modules(rows)), mock_http(side_effect=lambda *a, **k: BytesIO(json.dumps(payload(21)).encode())):
            result = builtin._fetch_codex_with_models()
        self.assertEqual([a.label for a in result.accounts], ['Account 1', 'Synthetic Blue'])

    def test_label_sanitization_and_no_identity_fallback(self):
        cases = [('  Alpha\n\x1b\x00\u202e\u2066\u2028\u2029 Team  ', 'Alpha Team'),
                 ('x' * 100, 'x' * 64), (' \t\u202e ', 'Account 1'),
                 (None, 'Account 1'), (123, 'Account 1')]
        for label, expected in cases:
            rows = [{'access_token': token('private-id'), 'label': label,
                     'email': 'private@example.invalid'}, {'access_token': token('b')}]
            with self.subTest(label=label), mock.patch.dict(sys.modules, auth_modules(rows)), mock_http(side_effect=lambda *a, **k: BytesIO(json.dumps(payload(21)).encode())):
                result = builtin._fetch_codex_with_models()
            self.assertEqual(result.accounts[0].label, expected)
            self.assertEqual(result.accounts[1].label, 'Account 2')

    def test_single_saved_label_is_additive_not_a_new_account_shape(self):
        from test_refresh_sweep import qc
        from widget_harness import render, text, tooltip_text
        with mock.patch.dict(sys.modules, auth_modules([
                {'access_token': token('a'), 'label': 'Synthetic Solo'},
                {'access_token': token('a', nonce='duplicate'), 'label': 'Duplicate alias'}])), mock_http(return_value=BytesIO(json.dumps(payload(21)).encode())):
            result = builtin._fetch_codex_with_models()
        self.assertEqual(result.label, 'openai-codex')
        self.assertEqual(result.accounts, [])
        record = qc._result_to_record(result)
        self.assertEqual(record['account_label'], 'Synthetic Solo')
        self.assertNotIn('accounts', record)
        for mode in ['all', 'worst']:
            tree = render(component='status', statusMode=mode, data={'age_s': 0, 'providers': {'openai-codex': record}})
            self.assertIn('OpenAI Codex', text(tree))
            self.assertIn('79%', text(tree))
            self.assertNotIn('Synthetic Solo', text(tree))
            self.assertNotIn('Account 1', text(tree))
            self.assertNotIn('Synthetic Solo', tooltip_text(tree))
        self.assertIn('Synthetic Solo', text(render(data={'providers': {'openai-codex': record}})))
        self.assertIn('Synthetic Solo', next(qc.iter_account_records({'openai-codex': record}))[1]['label'])

    def test_duplicate_best_priority_survives_alternate_valid_token(self):
        expired = token('synthetic-first', exp=1)
        valid = token('synthetic-first', nonce='alternate')
        rows = [{'access_token': token('synthetic-second'), 'priority': 5},
                {'access_token': valid, 'priority': 10},
                {'access_token': expired, 'priority': 1},
                {'access_token': token('synthetic-failed', exp=1), 'priority': 3}]
        with mock.patch.dict(sys.modules, auth_modules(rows)), mock_http(side_effect=lambda *a, **k: BytesIO(json.dumps(payload(21)).encode())) as http:
            result = builtin._fetch_codex_with_models()
        self.assertEqual(len(result.accounts), 3)
        self.assertEqual(result.accounts[1].unavailable_reason, 'reauth-required')
        self.assertEqual(result.accounts[0].windows[0].used_percent, 21)
        self.assertIn('Bearer ' + valid, [c.args[0].headers.get('Authorization') for c in http.call_args_list])
        self.assertNotIn(expired, repr(result))

    def test_large_numeric_priority_does_not_drop_the_pool(self):
        with mock.patch.dict(sys.modules, auth_modules([
                {'access_token': token('synthetic-later'), 'priority': 10 ** 400},
                {'access_token': token('synthetic-first'), 'priority': 1}])):
            self.assertEqual([c[1] for c in codex._credentials()], ['synthetic-first', 'synthetic-later'])

    def test_priority_order_is_numeric_validated_and_stable(self):
        rows = [{'access_token': token(name), 'priority': priority} for name, priority in
                [('last', None), ('second', 20), ('first', 2), ('tie', 20),
                 ('invalid', True), ('string', '1'), ('nan', float('nan'))]]
        with mock.patch.dict(sys.modules, auth_modules(rows)):
            credentials = codex._credentials()
        self.assertEqual([c[1] for c in credentials],
                         ['first', 'second', 'tie', 'last', 'invalid', 'string', 'nan'])


    def test_new_plan_and_model_names_keep_upstream_compatibility(self):
        for plan in ('prolite', 'synthetic-future-plan'):
            raw = payload(21)
            raw['plan_type'] = plan
            raw['additional_rate_limits'] = [{'limit_name': 'Synthetic Future Model',
                'rate_limit': {'primary_window': {'used_percent': 5}}}]
            result = builtin._parse_codex_payload(raw)
            # prolite is the $100 tier ("Pro 5x"); unknown names stay title-cased.
            expected_plan = 'Pro 5x' if plan == 'prolite' else plan.title()
            self.assertEqual(result.plan, expected_plan)
            self.assertEqual([w.label for w in result.windows],
                             ['Session', 'Synthetic Future Model · 5h'])

    def test_multi_account_reset_details_are_counts_not_commands(self):
        raw = payload(21)
        raw['rate_limit_reset_credits'] = {'available_count': 2}
        with mock.patch.dict(sys.modules, auth_modules([
                {'access_token': token('a')}, {'access_token': token('b')}])), mock_http(side_effect=lambda *a, **k: BytesIO(json.dumps(raw).encode())):
            result = builtin._fetch_codex_with_models()
        self.assertEqual(len(result.accounts), 2)
        for account in result.accounts:
            self.assertIn('2 resets banked', ' '.join(account.details))
            self.assertNotIn('/usage reset', ' '.join(account.details))

    def test_expired_duplicate_uses_saved_valid_token_without_refresh(self):
        expired = token("synthetic-a", exp=1)
        valid = token("synthetic-a")
        modules = auth_modules([{"access_token": expired}, {"access_token": valid},
                                {"access_token": token("synthetic-b", exp=1)}])
        with mock.patch.dict(sys.modules, modules), mock_http(return_value=BytesIO(json.dumps(payload(9)).encode())) as http:
            result = builtin._fetch_codex_with_models()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.args[0].headers.get("Authorization"), "Bearer " + valid)
        self.assertEqual(result.accounts[1].unavailable_reason, "reauth-required")
        self.assertEqual(result.accounts[1].windows, [])

    def test_requests_fail_independently_without_disclosing_errors(self):
        from urllib.error import HTTPError
        from email.message import Message
        for body, reason in [(b"PRIVATE_SECRET", "bad-json"), (b"[]", "bad-json"),
                             (HTTPError("https://example.invalid", 401, "PRIVATE_SECRET", Message(), BytesIO(b"PRIVATE_SECRET")), "reauth-required"),
                             (HTTPError("https://example.invalid", 302, "PRIVATE_SECRET", Message(), BytesIO(b"PRIVATE_SECRET")), "http-302")]:
            def get(request, **kwargs):
                self.assertEqual(str(request.url), "https://chatgpt.com/backend-api/wham/usage")
                self.assertLessEqual(kwargs["timeout"], 15)
                if request.headers.get("Chatgpt-account-id") == "synthetic-a":
                    if isinstance(body, Exception):
                        raise body
                    return BytesIO(body)
                return BytesIO(json.dumps(payload(42)).encode())
            with self.subTest(reason=reason), mock.patch.dict(sys.modules, auth_modules([
                    {"access_token": token("synthetic-a")},
                    {"access_token": token("synthetic-b")} ])), mock_http(side_effect=get):
                result = builtin._fetch_codex_with_models()
                self.assertEqual(result.accounts[0].unavailable_reason, reason)
                self.assertEqual(result.accounts[1].windows[0].used_percent, 42)
                self.assertNotIn("PRIVATE_SECRET", repr(result))

    def test_shared_deadline_preserves_fast_account_and_snapshot(self):
        import threading
        import time
        from quota_providers import codex
        release = threading.Event()
        finished = threading.Event()
        def get(request, **kwargs):
            if request.headers.get("Chatgpt-account-id") == "synthetic-a":
                release.wait(1)
                finished.set()
            return BytesIO(json.dumps(payload(42)).encode())
        try:
            with mock.patch.dict(sys.modules, auth_modules([{"access_token": token("synthetic-a")}, {"access_token": token("synthetic-b")}])), mock_http(side_effect=get), mock.patch.object(codex, "_FETCH_BUDGET_S", 0.05, create=True):
                start = time.monotonic()
                result = builtin._fetch_codex_with_models()
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertEqual(result.accounts[0].unavailable_reason, "timeout")
                self.assertEqual(result.accounts[1].windows[0].used_percent, 42)
                before = dataclasses.asdict(result)
        finally:
            release.set()
        finished.wait(1)
        self.assertEqual(dataclasses.asdict(result), before)

    def test_cache_preserves_accounts_and_data_contract(self):
        from test_refresh_sweep import qc
        from quota_providers.base import QuotaResult, QuotaWindow
        result = QuotaResult("openai-codex", accounts=[
            QuotaResult("Account 1", windows=[QuotaWindow("Session", 21)]),
            QuotaResult("Account 2", unavailable_reason="reauth-required")])
        self.assertTrue(result.has_data())
        record = qc._result_to_record(result)
        self.assertEqual([a["label"] for a in record["accounts"]], ["Account 1", "Account 2"])
        self.assertEqual(record["windows"], [])
        result.accounts[0].windows.clear()
        self.assertFalse(result.has_data())

    def test_empty_pool_uses_read_only_legacy_state_and_single_shape(self):
        modules = auth_modules([], {"tokens": {"access_token": token("synthetic-a")}})
        with mock.patch.dict(sys.modules, modules), mock_http(return_value=BytesIO(json.dumps(payload(21)).encode())):
            result = builtin._fetch_codex_with_models()
        self.assertEqual(result.label, "openai-codex")
        self.assertEqual(result.accounts, [])
        self.assertEqual([w.used_percent for w in result.windows], [21, 12])
        from test_refresh_sweep import qc
        self.assertNotIn('account_label', qc._result_to_record(result))


    def test_malformed_optional_fields_do_not_hide_valid_windows(self):
        raw = payload(21)
        raw.update(plan_type="synthetic-future-plan", rate_limit_reset_credits={"available_count": float("inf")},
                   credits={"has_credits": True, "balance": float("nan")})
        raw["rate_limit"]["secondary_window"] = ["bad-shape"]
        raw["additional_rate_limits"].append({"limit_name": "Synthetic Future Model", "rate_limit": {"primary_window": {"used_percent": 5}}})
        with mock.patch.dict(sys.modules, auth_modules([{"access_token": token("synthetic-a")} ])), mock_http(return_value=BytesIO(json.dumps(raw).encode())):
            result = builtin._fetch_codex_with_models()
        self.assertEqual([w.used_percent for w in result.windows], [21, 12, 5])
        self.assertEqual(result.plan, "Synthetic-Future-Plan")
        self.assertEqual(result.details, [])

    def test_metadata_expiry_and_network_timeout_are_honest(self):
        for expiry in (1, "1970-01-01T00:00:01Z"):
            with self.subTest(expiry=expiry), mock.patch.dict(sys.modules, auth_modules([
                    {"access_token": "SYNTHETIC_OPAQUE", "expires_at": expiry}])), mock_http() as http:
                result = builtin._fetch_codex_with_models()
                self.assertEqual(result.unavailable_reason, "reauth-required")
                http.assert_not_called()
        with mock.patch.dict(sys.modules, auth_modules([{"access_token": token("synthetic-a")} ])), mock_http(side_effect=TimeoutError("PRIVATE_SECRET")):
            self.assertEqual(builtin._fetch_codex_with_models().unavailable_reason, "timeout")

    def test_text_surfaces_show_each_account_without_totals(self):
        import importlib
        from test_refresh_sweep import qc
        commands = importlib.import_module("quota_plugin_under_test.commands")
        plugin = importlib.import_module("quota_plugin_under_test.__init__")
        record = {"providers": {"openai-codex": {"accounts": [
            {"label": "Synthetic Amber", "windows": [{"label": "Session", "used_percent": 21, "reset_at": "2030-01-01T00:00:00Z"}]},
            {"label": "Synthetic Blue", "unavailable_reason": "reauth-required"}]}}}
        with mock.patch.object(commands, "read_quota_cache", return_value=record), mock.patch.object(commands, "_age_label", return_value="fresh"):
            rendered = commands._render_quota("openai-codex")
        self.assertIn("Synthetic Amber", rendered)
        self.assertIn("Synthetic Blue", rendered)
        self.assertIn("79%", rendered)
        self.assertIn("reauth-required", rendered)
        self.assertNotIn("Account 1", rendered)
        footer = plugin._format_quota_block(record)
        self.assertIn("Synthetic Amber", footer)
        self.assertNotIn("Account 1", footer)

    def test_all_accounts_unavailable_is_not_a_successful_total(self):
        with mock.patch.dict(sys.modules, auth_modules([{"access_token": token("synthetic-a", exp=1)}, {"access_token": token("synthetic-b", exp=1)}])), mock_http() as http:
            result = builtin._fetch_codex_with_models()
        self.assertEqual(result.unavailable_reason, "no-data")
        self.assertFalse(result.has_data())
        self.assertEqual([a.unavailable_reason for a in result.accounts], ["reauth-required", "reauth-required"])
        http.assert_not_called()

    def test_invalid_optional_sections_do_not_erase_healthy_quota(self):
        for extra in (1, True, None, {}, "bad"):
            raw = payload(21)
            raw["additional_rate_limits"] = extra
            raw["rate_limit_reset_credits"] = {"available_count": 10 ** 400}
            raw["credits"] = {"has_credits": True, "balance": 10 ** 400}
            with self.subTest(extra=extra), mock.patch.dict(sys.modules, auth_modules([{"access_token": token("synthetic-a")} ])), mock_http(return_value=BytesIO(json.dumps(raw).encode())):
                result = builtin._fetch_codex_with_models()
            self.assertEqual([w.used_percent for w in result.windows], [21])
            self.assertEqual(result.details, [])

    def test_saved_accounts_deduplicate_identity_not_oauth_token(self):
        a, duplicate, b = token("synthetic-a"), token("synthetic-a", nonce="two"), token("synthetic-b")
        modules = auth_modules([{"access_token": t, "label": label} for t, label in [(a, "Synthetic Amber"), (duplicate, "Ignored alias"), (b, "Synthetic Blue")]])
        def get(request, **kwargs):
            used = 21 if request.headers.get("Authorization") == "Bearer " + a else 42
            return BytesIO(json.dumps(payload(used)).encode())
        with mock.patch.dict(sys.modules, modules), mock_http(side_effect=get) as http:
            result = builtin._fetch_codex_with_models()
        accounts = getattr(result, "accounts", [])
        self.assertEqual(len(accounts), 2)
        self.assertEqual([r.label for r in accounts], ["Synthetic Amber", "Synthetic Blue"])
        self.assertEqual([r.windows[0].used_percent for r in accounts], [21, 42])
        self.assertEqual(accounts[0].windows[1].label, "5.3 Codex Spark · Weekly")
        self.assertIsNotNone(accounts[1].windows[0].reset_at)
        self.assertEqual(http.call_count, 2)

        self.assertEqual(result.windows, [])  # no cross-account percentage total
        serialized = json.dumps(dataclasses.asdict(result))
        for private in (a, duplicate, b, "synthetic-a", "synthetic-b", "synthetic-subject"):
            self.assertNotIn(private, serialized)
        # Exercise the actual backend -> cache schema -> statusbar path, not
        # merely a tooltip or a manually constructed account fixture.
        from test_refresh_sweep import qc
        from widget_harness import render, text, nodes, tooltip_text
        tree = render(component="status", data={"age_s": 0, "providers": {"openai-codex": qc._result_to_record(result)}})
        chips = [n for n in nodes(tree) if n.get("type") == "button"]
        self.assertEqual(len(chips), 1)
        self.assertIn("Synthetic Amber", text(chips[0]))
        self.assertIn("79%", text(chips[0]))
        tip = tooltip_text(tree)
        self.assertLess(tip.index('Synthetic Amber'), tip.index('Synthetic Blue'))
        for expected in ('79% left', '58% left', 'resets', '● Synthetic Amber'):
            self.assertIn(expected, tip)


class CodexWidgetTests(unittest.TestCase):
    def test_failed_poll_preserves_other_providers_original_hidden_state(self):
        from widget_harness import render, text
        providers = {'anthropic': {'windows': [{'label': 'Weekly', 'used_percent': 42}]}}
        self.assertNotIn('Anthropic', text(render(component='status', isError=True,
                                               data={'age_s': 0, 'providers': providers})))

    def test_equal_saved_names_mark_only_the_selected_account(self):
        from widget_harness import render, tooltip_text
        for mode in ['all', 'worst']:
            data = {'age_s': 0, 'providers': {'openai-codex': {'accounts': [
                {'label': 'Same name', 'windows': [{'label': 'Session', 'used_percent': 100}]},
                {'label': 'Same name', 'windows': [{'label': 'Weekly', 'used_percent': 42}]}]}}}
            tip = tooltip_text(render(component='status', statusMode=mode, data=data))
            self.assertEqual(tip.count('●'), 1)
            self.assertIn('○ Same name · limit reached', tip)
            self.assertIn('● Same name · 58% left', tip)
            self.assertEqual(len(tip.splitlines()), 4)
            self.assertNotIn('Quota breakdown', tip)

    def test_cached_labels_are_sanitized_and_blank_names_fall_back(self):
        from widget_harness import render, text, tooltip_text
        from test_refresh_sweep import qc
        for raw, expected in [(' Custom\n\x1b\u202e\u2028 Name ', 'Custom Name'),
                              ('\u2066\t ', 'Account 1'), (True, 'Account 1'),
                              ('😀' * 80, '😀' * 64)]:
            accounts = [{'label': raw, 'windows': [{'label': 'Session', 'used_percent': 21}]},
                        {'label': 'Second', 'windows': [{'label': 'Weekly', 'used_percent': 42}]}]
            data = {'age_s': 0, 'providers': {'openai-codex': {'accounts': accounts}}}
            for mode in ['all', 'worst']:
                tree = render(component='status', statusMode=mode, data=data)
                self.assertIn(expected, text(tree))
                self.assertIn('● ' + expected + ' · 79% left', tooltip_text(tree))
                self.assertEqual(len(tooltip_text(tree).splitlines()), 4)
            self.assertIn(expected, text(render(data=data)))
            self.assertEqual(next(qc.iter_account_records(data['providers']))[1]['label'], 'openai-codex · ' + expected)

    def test_unknown_codex_does_not_hide_balance_only_providers_in_worst_mode(self):
        from widget_harness import render, text, nodes, tooltip_text
        data = {'age_s': 0, 'providers': {
            'openai-codex': {'accounts': [{'unavailable_reason': 'reauth-required'}, {'windows': []}]},
            'deepseek': {'account_balances': [{'currency': 'USD', 'total_balance': '12.00'}]}}}
        tree = render(component='status', statusMode='worst', data=data)
        self.assertIn('unknown', text(tree))
        self.assertIn('12.00', text(tree))
        self.assertIn('DeepSeek', text(tree))

    def test_provider_modes_compare_representatives_not_exhausted_siblings(self):
        from widget_harness import render, text, nodes, tooltip_text
        data = {'age_s': 0, 'providers': {
            'openai-codex': {'accounts': [
                {'windows': [{'label': 'Session', 'used_percent': 100}]},
                {'windows': [{'label': 'Weekly', 'used_percent': 42}]}]},
            'anthropic': {'windows': [{'label': 'Session', 'used_percent': 60}]}}}
        all_tree = render(component='status', data=data)
        self.assertEqual(len([n for n in nodes(all_tree) if n.get('type') == 'button']), 2)
        worst = render(component='status', statusMode='worst', data=data)
        self.assertIn('Anthropic 40%', text(worst))
        tip = tooltip_text(worst)
        self.assertIn('Account 1 · limit reached', tip)
        self.assertIn('● Account 2 · 58% left', tip)
        data['providers']['openai-codex']['accounts'][1]['windows'][0]['used_percent'] = 100
        worst = render(component='status', statusMode='worst', data=data)
        self.assertIn('limit reached', text(worst))
        self.assertNotIn('0%', text(worst))

    def test_hover_has_ordered_headers_selection_errors_and_all_resets(self):
        from widget_harness import render, text, nodes, tooltip_text
        accounts = [
            {'windows': [{'label': 'Session', 'used_percent': 100, 'reset_at': '2099-01-01T00:00:00Z'}]},
            {'unavailable_reason': 'reauth-required', 'windows': [{'label': 'Weekly', 'used_percent': 21, 'reset_at': '2099-01-02T00:00:00Z'}]},
            {'windows': [{'label': 'Session', 'used_percent': 42, 'reset_at': '2099-01-03T00:00:00Z'}]}]
        for mode in ['all', 'worst']:
            tree = render(component='status', statusMode=mode, data={'age_s': 0, 'providers': {'openai-codex': {'accounts': accounts}}})
            chip = next(n for n in nodes(tree) if n.get('type') == 'button')
            self.assertIn('Account 2', text(chip))
            self.assertIn('unknown', text(chip))
            tip = tooltip_text(tree)
            self.assertLess(tip.index('Account 1'), tip.index('Account 2'))
            self.assertLess(tip.index('Account 2'), tip.index('Account 3'))
            self.assertEqual(tip.count('resets'), 2)  # errored history belongs in the pane
            selected = next(line for line in tip.splitlines() if line.startswith('● Account'))
            self.assertIn('Account 2', selected)
            self.assertIn('unknown', selected)
            self.assertIn('reauth-required', selected)
            self.assertIn('limit reached', tip)

    def test_saved_placeholder_or_poll_error_never_claims_fresh_eligibility(self):
        from widget_harness import render, text, nodes, tooltip_text
        data = {'age_s': 0, 'providers': {'openai-codex': {'accounts': [
            {'windows': [{'label': 'Session', 'used_percent': 100}]},
            {'windows': [{'label': 'Weekly', 'used_percent': 42}]}]}}}
        for flags in [{'isPlaceholderData': True}, {'isError': True}]:
            for mode in ['all', 'worst']:
                tree = render(component='status', statusMode=mode, data=data, **flags)
                chips = [n for n in nodes(tree) if n.get('type') == 'button']
                self.assertEqual(len(chips), 1)
                self.assertIn('Account 1', text(chips[0]))
                self.assertIn('unknown', text(chips[0]))
                self.assertIn('Account 2', tooltip_text(tree))


    def test_single_account_shape_stays_compact_with_generic_windows(self):
        from widget_harness import render, text, nodes, tooltip_text
        for mode in ['all', 'worst']:
            for used, expected in [(21, '79%'), (100, 'limit reached')]:
                data = {'age_s': 0, 'providers': {'openai-codex': {'windows': [
                    {'label': 'Session', 'used_percent': used},
                    {'label': '5.3 Codex Spark · Weekly', 'used_percent': 100}]}}}
                tree = render(component='status', statusMode=mode, data=data)
                chips = [n for n in nodes(tree) if n.get('type') == 'button']
                self.assertEqual(len(chips), 1)
                self.assertIn('OpenAI Codex', text(chips[0]))
                self.assertNotIn('Account 1', text(chips[0]))
                self.assertIn(expected, text(chips[0]))
                self.assertNotIn('Spark', tooltip_text(tree))

    def test_stale_or_missing_age_blocks_fallback_until_fresh(self):
        from widget_harness import render, text, nodes, tooltip_text
        accounts = [{'windows': [{'label': 'Session', 'used_percent': 100}]},
                    {'windows': [{'label': 'Session', 'used_percent': 42}]}]
        for age in [None, 60, 1801, 'bad', -1]:
            for mode in ['all', 'worst']:
                with self.subTest(age=age, mode=mode):
                    data = {'age_s': age, 'providers': {'openai-codex': {'accounts': accounts}}}
                    tree = render(component='status', statusMode=mode, data=data)
                    chip = next(n for n in nodes(tree) if n.get('type') == 'button')
                    self.assertIn('Account 1', text(chip))
                    self.assertIn('unknown', text(chip))
                    self.assertIn('stale', tooltip_text(tree).lower())
                    self.assertTrue(tooltip_text(tree).startswith('● Account 1 · unknown · stale'))
                    self.assertIn('Account 2', tooltip_text(tree))


    def test_only_exact_exhaustion_advances_and_all_exhausted_says_limit_reached(self):
        from widget_harness import render, text, nodes, tooltip_text
        for used, expected in [(99.9, '<1%'), (100, 'limit reached')]:
            accounts = [{'windows': [{'label': 'Session', 'used_percent': used}]},
                        {'windows': [{'label': 'Weekly', 'used_percent': 100}]}]
            tree = render(component='status', data={'age_s': 0, 'providers': {'openai-codex': {'accounts': accounts}}})
            chip = next(n for n in nodes(tree) if n.get('type') == 'button')
            self.assertIn('Account 1', text(chip))
            self.assertIn(expected, text(chip))
            self.assertNotIn('0%', text(chip))

    def test_unknown_or_failed_first_is_not_skipped_or_shown_as_available(self):
        from widget_harness import render, text, nodes, tooltip_text
        for first in [
                {'windows': []},
                {'unavailable_reason': 'reauth-required', 'windows': [{'label': 'Session', 'used_percent': 100}]},
                {'unavailable_reason': 'timeout'},
                {'windows': [{'label': 'Session', 'used_percent': None}, {'label': 'Weekly', 'used_percent': 100}]},
                {'windows': [{'label': '5.3 Codex Spark · Weekly', 'used_percent': 100}]}]:
            with self.subTest(first=first):
                accounts = [first, {'windows': [{'label': 'Session', 'used_percent': 42}]}]
                tree = render(component='status', data={'age_s': 0, 'providers': {'openai-codex': {'accounts': accounts}}})
                chip = next(n for n in nodes(tree) if n.get('type') == 'button')
                self.assertIn('Account 1', text(chip))
                self.assertIn('unknown', text(chip))
                self.assertNotIn('58%', text(chip))
                self.assertIn('Account 2', tooltip_text(tree))
                self.assertIn('58% left', tooltip_text(tree))

    def test_model_scoped_exhaustion_does_not_exhaust_generic_codex(self):
        from widget_harness import render, text, nodes, tooltip_text
        accounts = [{'windows': [{'label': 'Session', 'used_percent': 21},
                                 {'label': '5.3 Codex Spark · Weekly', 'used_percent': 100}]},
                    {'windows': [{'label': 'Session', 'used_percent': 42}]}]
        tree = render(component='status', data={'age_s': 0, 'providers': {'openai-codex': {'accounts': accounts}}})
        chip = next(n for n in nodes(tree) if n.get('type') == 'button')
        self.assertIn('Account 1', text(chip))
        self.assertIn('79%', text(chip))
        self.assertNotIn('Spark', tooltip_text(tree))
        self.assertIn('5.3 Codex Spark', text(render(data={'age_s': 0, 'providers': {'openai-codex': {'accounts': accounts}}})))

    def test_exhausted_first_switches_then_refreshed_recovery_returns(self):
        from widget_harness import render, text, nodes, tooltip_text
        accounts = [{'windows': [{'label': 'Session', 'used_percent': 100}]},
                    {'windows': [{'label': 'Weekly', 'used_percent': 42}]}]
        for used, selected, percent in [(100, 'Account 2', '58%'), (21, 'Account 1', '79%')]:
            accounts[0]['windows'][0]['used_percent'] = used
            tree = render(component='status', data={'age_s': 0, 'providers': {'openai-codex': {'accounts': accounts}}})
            chips = [n for n in nodes(tree) if n.get('type') == 'button']
            self.assertEqual(len(chips), 1)
            self.assertIn(selected, text(chips[0]))
            self.assertIn(percent, text(chips[0]))


    def test_pane_and_worst_mode_keep_account_identity_and_provider_icon(self):
        from widget_harness import render, text, nodes, tooltip_text
        data = {"age_s": 0, "providers": {"openai-codex": {"accounts": [
            {"label": "Synthetic Amber", "windows": [{"label": "Session", "used_percent": 21}]},
            {"label": "Synthetic Blue", "windows": [{"label": "Weekly", "used_percent": 42}]}]}}}
        pane = render(data=data)
        self.assertIn("Synthetic Amber", text(pane))
        self.assertIn("Synthetic Blue", text(pane))
        self.assertIn("Synthetic Amber", json.dumps(pane))
        self.assertGreaterEqual(len([n for n in nodes(pane) if n.get("type") == "svg"]), 2)
        worst = render(component="status", statusMode="worst", data=data)
        self.assertIn("Synthetic Amber", text(worst))
        self.assertIn("79%", text(worst))
        self.assertIn("Synthetic Amber", tooltip_text(worst))
        hidden = render(component="status", data=data, disabled=["openai-codex"])
        self.assertNotIn("Account", text(hidden))
        data["providers"]["openai-codex"]["accounts"][0]["unavailable_reason"] = "reauth-required"
        failed = render(component="status", statusMode="worst", data=data)
        self.assertIn("● Synthetic Amber · unknown · reauth-required", tooltip_text(failed))

    def test_compact_chip_hover_keeps_all_accounts_in_actual_statusbar(self):
        from widget_harness import render, text, nodes, tooltip_text
        for reason in (None, "reauth-required"):
            accounts = [{"label": "Account 1", "windows": [{"label": "Session", "used_percent": 21}]},
                        {"label": "Account 2", "windows": [{"label": "Weekly", "used_percent": 42}], "unavailable_reason": reason}]
            tree = render(component="status", data={"age_s": 0, "providers": {"openai-codex": {"accounts": accounts}}})
            chips = [n for n in nodes(tree) if n.get("type") == "button"]
            self.assertEqual(len(chips), 1)
            self.assertIn("Account 1", text(chips[0]))
            self.assertIn("79%", text(chips[0]))
            tip = tooltip_text(tree)
            self.assertIn("Account 2", tip)
            self.assertIn("unknown" if reason else "58%", tip)
            self.assertIn("Weekly" if not reason else reason, tip)


if __name__ == "__main__":
    unittest.main()
