"""Authenticated usage requests identify the quota plugin, not a vendor CLI."""
import unittest
from unittest import mock
from quota_providers import builtin


class UsageClientIdentityTests(unittest.TestCase):
    def test_usage_request_identifies_the_plugin(self):
        with mock.patch.object(builtin, 'urlopen_no_redirect', side_effect=OSError('synthetic transport')) as opener:
            builtin._request_anthropic_usage('synthetic-access-token')
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header('User-agent'), 'hermes-quota-plugin/2.10.0')
        self.assertNotIn('claude-code', request.get_header('User-agent'))
