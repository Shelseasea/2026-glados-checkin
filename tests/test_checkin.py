import json
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

import checkin


class FakeClient:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0

    def checkin(self):
        self.calls += 1
        return next(self.results)


class CheckinResultTests(unittest.TestCase):
    def test_current_observation_message_is_normal(self):
        result = {
            'code': 1,
            'message': "Today's observation logged. Return tomorrow for more points.",
        }
        self.assertTrue(checkin.is_normal_checkin_result(result))

    def test_historic_success_message_is_normal(self):
        self.assertTrue(
            checkin.is_normal_checkin_result({'code': 0, 'message': 'Checkin! Got 15 Points'})
        )

    def test_unknown_error_is_failure(self):
        self.assertFalse(checkin.is_normal_checkin_result({'code': 2, 'message': 'Cookie expired'}))

    def test_permission_failure_is_not_retryable(self):
        self.assertTrue(
            checkin.is_non_retryable_checkin_result({'code': -2, 'message': '没有权限'})
        )

    def test_device_mismatch_is_not_retryable(self):
        self.assertTrue(
            checkin.is_non_retryable_checkin_result(
                {'code': 4, 'reason': 'device-mismatch', 'message': 'denied'}
            )
        )

    @mock.patch('checkin.time.sleep')
    def test_retry_stops_after_success(self, sleep):
        client = FakeClient([
            None,
            {'code': 0, 'message': 'Checkin! Got 15 Points'},
        ])

        result, success = checkin.checkin_with_retry(client, attempts=3, delay_seconds=1)

        self.assertTrue(success)
        self.assertEqual(result['code'], 0)
        self.assertEqual(client.calls, 2)
        sleep.assert_called_once_with(1)

    @mock.patch('checkin.time.sleep')
    def test_retry_stops_immediately_for_auth_failure(self, sleep):
        client = FakeClient([
            {'code': -2, 'message': '没有权限'},
            {'code': 0, 'message': 'Checkin! Got 15 Points'},
        ])

        result, success = checkin.checkin_with_retry(client, attempts=3, delay_seconds=1)

        self.assertFalse(success)
        self.assertEqual(result['code'], -2)
        self.assertEqual(client.calls, 1)
        sleep.assert_not_called()


class BrowserHeaderTests(unittest.TestCase):
    def test_custom_chrome_user_agent_builds_matching_client_hints(self):
        user_agent = (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/154.0.0.0 Safari/537.36'
        )
        with mock.patch.dict(os.environ, {'GLADOS_USER_AGENT': user_agent}):
            headers = checkin.get_browser_headers()

        self.assertEqual(headers['User-Agent'], user_agent)
        self.assertIn('v="154"', headers['Sec-CH-UA'])
        self.assertEqual(headers['Sec-CH-UA-Mobile'], '?0')
        self.assertEqual(headers['Sec-CH-UA-Platform'], '"macOS"')

    def test_empty_user_agent_uses_compatible_default(self):
        with mock.patch.dict(os.environ, {'GLADOS_USER_AGENT': ''}):
            headers = checkin.get_browser_headers()

        self.assertEqual(headers['User-Agent'], checkin.DEFAULT_USER_AGENT)


class CookieTests(unittest.TestCase):
    def test_json_token_uses_real_cookie_name(self):
        self.assertEqual(checkin.extract_cookie('{"token":"abc"}'), 'koa:sess=abc')

    def test_missing_configuration_returns_no_accounts(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(checkin.get_cookies(), [])

    def test_current_signed_session_is_detected(self):
        cookie = 'koa:sess=old; gld:sess=current; gld:sess.sig=signature'

        self.assertEqual(checkin.get_session_cookie_kind(cookie), 'gld')

    def test_legacy_signed_session_is_detected(self):
        cookie = 'koa:sess=old; koa:sess.sig=signature'

        self.assertEqual(checkin.get_session_cookie_kind(cookie), 'koa')

    def test_incomplete_current_session_is_not_accepted(self):
        self.assertIsNone(checkin.get_session_cookie_kind('gld:sess=current'))

    def test_cookie_editor_json_export_preserves_current_session(self):
        raw = json.dumps([
            {'name': 'koa:sess', 'value': 'old'},
            {'name': 'gld:sess', 'value': 'current'},
            {'name': 'gld:sess.sig', 'value': 'signature'},
        ])

        cookie = checkin.extract_cookie(raw)

        self.assertIn('gld:sess=current', cookie)
        self.assertEqual(checkin.get_session_cookie_kind(cookie), 'gld')

    def test_cookie_header_prefix_is_removed(self):
        raw = 'Cookie: gld:sess=current; gld:sess.sig=signature'

        self.assertEqual(
            checkin.extract_cookie(raw),
            'gld:sess=current; gld:sess.sig=signature',
        )

    @mock.patch('checkin.requests.get')
    def test_full_cookie_header_is_forwarded_to_api(self, request_get):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'code': 0, 'data': {}}
        request_get.return_value = response
        cookie = (
            'koa:sess=legacy; koa:sess.sig=legacy-signature; '
            'gld:sess=current; gld:sess.sig=current-signature; tracking=optional'
        )

        result = checkin.GLaDOS(cookie).req('GET', '/api/user/status')

        self.assertEqual(result['code'], 0)
        self.assertEqual(request_get.call_args.kwargs['headers']['Cookie'], cookie)


class CompatibilityRegressionTests(unittest.TestCase):
    def test_pretty_printed_cookie_editor_export_is_one_account(self):
        raw = json.dumps([
            {'name': 'gld:sess', 'value': 'fake-session'},
            {'name': 'gld:sess.sig', 'value': 'fake-signature'},
        ], indent=2)
        with mock.patch.dict(os.environ, {'GLADOS_COOKIE': raw}, clear=True):
            self.assertEqual(checkin.get_cookies(), [
                'gld:sess=fake-session; gld:sess.sig=fake-signature',
            ])

    def test_multiline_accounts_remain_supported(self):
        cookies = [
            'gld:sess=fake-a; gld:sess.sig=fake-signature-a',
            'gld:sess=fake-b; gld:sess.sig=fake-signature-b',
        ]
        with mock.patch.dict(os.environ, {'GLADOS_COOKIE': '\n'.join(cookies)}, clear=True):
            self.assertEqual(checkin.get_cookies(), cookies)

    def test_auth_failure_overrides_success_code(self):
        self.assertFalse(checkin.is_normal_checkin_result({
            'code': 0, 'reason': 'device-mismatch', 'message': 'denied',
        }))

    @mock.patch('checkin.time.sleep')
    def test_exhausted_retries_report_failure(self, sleep):
        client = FakeClient([None, None, None])
        result, success = checkin.checkin_with_retry(client, attempts=3, delay_seconds=0)
        self.assertIsNone(result)
        self.assertFalse(success)
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleep.call_count, 2)

    @mock.patch('checkin.requests.post')
    def test_telegram_exception_does_not_log_token(self, post):
        post.side_effect = checkin.requests.RequestException(
            'https://api.telegram.org/botFAKE-TEST-TOKEN/sendMessage',
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertFalse(checkin.telegram_push('FAKE-TEST-TOKEN', 'fake-chat', 'title', 'body'))
        self.assertNotIn('FAKE-TEST-TOKEN', output.getvalue())

    def test_main_exit_status_and_private_account_details(self):
        for result, expected in [
            ({'code': 0, 'message': 'Checkin! Got 15 Points'}, 0),
            ({'code': -2, 'message': '没有权限'}, 1),
        ]:
            with self.subTest(expected=expected), mock.patch.dict(
                os.environ, {'CHECKIN_RETRY_DELAY_SECONDS': '0'}, clear=True,
            ), mock.patch('checkin.get_cookies', return_value=[
                'gld:sess=fake; gld:sess.sig=fake-signature',
            ]), mock.patch('checkin.GLaDOS') as factory:
                client = factory.return_value
                client.checkin.return_value = result
                client.email = 'private@example.invalid'
                client.points = '999'
                client.left_days = '100'
                client.points_change = '+15'
                client.exchange_info = ''
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(checkin.main(), expected)
                self.assertNotIn(client.email, output.getvalue())
                client.exchange.assert_not_called()


if __name__ == '__main__':
    unittest.main()
