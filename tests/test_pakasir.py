import http.client
import io
import json
import traceback
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import MagicMock, patch

from pakasir import Pakasir, PakasirError, _NoRedirect


class PakasirTests(unittest.TestCase):
    def setUp(self):
        self.key = "secret-key+&/?"
        self.project = "Shop_123-test"
        self.reference = "TG-order /?&=雪"
        self.amount = 25000
        self.client = Pakasir(self.project, self.key)
        self.open_patch = patch.object(self.client._opener, "open")
        self.open = self.open_patch.start()
        self.addCleanup(self.open_patch.stop)
        self.transaction = {
            "project": self.project,
            "order_id": self.reference,
            "amount": self.amount,
            "status": "completed",
        }

    def response(self, body, status=200, *, raw=False):
        response = MagicMock()
        response.status = status
        response.read.return_value = body if raw else json.dumps(body).encode()
        self.open.return_value.__enter__.return_value = response
        return response

    def assert_safe_error(self, operation, code=0):
        try:
            operation()
        except PakasirError as exc:
            self.assertEqual(exc.code, code)
            self.assertEqual(str(exc), "Pakasir request failed.")
            rendered = "".join(traceback.format_exception(exc))
            self.assertNotIn(self.key, rendered)
            self.assertNotIn("https://evil.example", rendered)
            return
        self.fail("PakasirError not raised")

    def detail(self):
        return self.client.detail(self.reference, self.amount)

    def cancel(self):
        return self.client.cancel(self.reference, self.amount)

    def test_checkout_url_is_encoded_and_credential_free(self):
        url = self.client.checkout_url(self.reference, self.amount)
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "app.pakasir.com")
        self.assertEqual(parsed.path, f"/pay/{self.project}/{self.amount}")
        self.assertEqual(parsed.fragment, "")
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {
            "order_id": [self.reference], "qris_only": ["1"],
        })
        self.assertNotIn(self.key, url)
        self.assertNotIn("api_key", url)
        self.open.assert_not_called()

    def test_detail_uses_exact_get_query_and_timeout(self):
        response = self.response({"transaction": self.transaction})
        self.assertEqual(self.detail(), self.transaction)
        request = self.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        parsed = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                         "https://app.pakasir.com/api/transactiondetail")
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {
            "project": [self.project], "order_id": [self.reference],
            "amount": [str(self.amount)], "api_key": [self.key],
        })
        self.assertEqual(self.open.call_args.kwargs, {"timeout": 10})
        response.read.assert_called_once_with(1024 * 1024 + 1)

    def test_cancel_posts_exact_json_without_claiming_final_status(self):
        self.response({"success": True})
        self.assertIsNone(self.cancel())
        request = self.open.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://app.pakasir.com/api/transactioncancel")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {
            "project": self.project, "order_id": self.reference,
            "amount": self.amount, "api_key": self.key,
        })
        self.assertEqual(self.open.call_args.kwargs, {"timeout": 10})

    def test_constructor_rejects_invalid_values(self):
        for project in (None, 12, "", "x" * 101, "foo/bar", "foo.bar", "a?b", "é", "a\n"):
            with self.subTest(project=project):
                self.assert_safe_error(lambda: Pakasir(project, self.key))
        for key in (None, 12, "", " \n"):
            with self.subTest(key=key):
                self.assert_safe_error(lambda: Pakasir(self.project, key))

    def test_input_validation_for_every_operation(self):
        for method in (self.client.checkout_url, self.client.detail, self.client.cancel):
            for reference in (None, 42, "", " \n"):
                with self.subTest(method=method.__name__, reference=reference):
                    self.assert_safe_error(lambda: method(reference, self.amount))
            for amount in (None, "25000", 25000.0, True, False, 0, -1):
                with self.subTest(method=method.__name__, amount=amount):
                    self.assert_safe_error(lambda: method(self.reference, amount))
        self.open.assert_not_called()

    def test_detail_rejects_mismatched_or_missing_fields(self):
        invalid = {
            "project": ("another-project", None),
            "order_id": ("another-order", None),
            "amount": (25001, "25000", 25000.0, True, None),
            "status": ("", " \n", None, 1, {}, []),
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.response({"transaction": {**self.transaction, field: value}})
                    self.assert_safe_error(self.detail)
            transaction = self.transaction.copy()
            del transaction[field]
            self.response({"transaction": transaction})
            self.assert_safe_error(self.detail)

    def test_detail_rejects_missing_or_nonobject_transaction(self):
        for body in ({}, {"status": "not_found"}, {"transaction": None},
                     {"transaction": []}, {"transaction": "bad"}):
            with self.subTest(body=body):
                self.response(body)
                self.assert_safe_error(self.detail)

    def test_detail_preserves_nonempty_provider_status(self):
        for status in ("pending", "completed", "cancelled", "expired", "future_status"):
            with self.subTest(status=status):
                transaction = {**self.transaction, "status": status}
                self.response({"transaction": transaction})
                self.assertEqual(self.detail(), transaction)

    def test_only_http_404_means_not_found_for_detail(self):
        for code in (301, 302, 303, 307, 308, 400, 401, 403, 404, 429, 500, 503):
            for operation in (self.detail, self.cancel):
                with self.subTest(code=code, operation=operation.__name__):
                    self.open.side_effect = urllib.error.HTTPError(
                        "https://evil.example/" + self.key, code, self.key,
                        {}, io.BytesIO(self.key.encode()),
                    )
                    if code == 404 and operation == self.detail:
                        self.assertEqual(operation(), {"status": "not_found"})
                    else:
                        self.assert_safe_error(operation, code)

    def test_network_errors_are_redacted(self):
        for error_type in (urllib.error.URLError, TimeoutError, OSError,
                           http.client.HTTPException, http.client.RemoteDisconnected,
                           http.client.IncompleteRead):
            for operation in (self.detail, self.cancel):
                with self.subTest(error=error_type, operation=operation.__name__):
                    self.open.side_effect = error_type(self.key)
                    self.assert_safe_error(operation)

    def test_read_errors_are_redacted(self):
        response = self.response({})
        response.read.side_effect = http.client.IncompleteRead(self.key.encode())
        self.assert_safe_error(self.detail)

    def test_invalid_json_and_error_objects_are_rejected(self):
        bodies = (b"not json", b"\xff", b"[]", b"null", b"1", b'"text"',
                  b'{"error":"secret-key+&/?"}', b'{"success":false}',
                  b'{"status":"error"}', b'{"status":"failed"}',
                  json.dumps({"transaction": self.transaction, "error": self.key}).encode())
        for body in bodies:
            for operation in (self.detail, self.cancel):
                with self.subTest(body=body, operation=operation.__name__):
                    self.response(body, raw=True)
                    self.assert_safe_error(operation)

    def test_oversized_and_deep_json_responses_are_rejected(self):
        for body in (b" " * (1024 * 1024 + 1), b"[" * 2000 + b"]" * 2000):
            for operation in (self.detail, self.cancel):
                self.response(body, raw=True)
                self.assert_safe_error(operation)

    def test_non_success_response_status_is_rejected(self):
        for status in (300, 302, 401, 500):
            for operation in (self.detail, self.cancel):
                self.response({"transaction": self.transaction}, status=status)
                self.assert_safe_error(operation, status)

    def test_redirects_never_open_another_url(self):
        handler = next(handler for handler in self.client._opener.handlers
                       if isinstance(handler, _NoRedirect))
        for method in ("GET", "POST"):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(method=method, status=status):
                    request = urllib.request.Request(
                        "https://app.pakasir.com/api/transactiondetail?api_key="
                        + urllib.parse.quote(self.key),
                        data=self.key.encode() if method == "POST" else None,
                    )
                    self.assert_safe_error(lambda: handler.http_error_302(
                        request, io.BytesIO(), status, "Found",
                        {"location": "https://evil.example/" + self.key},
                    ), status)
        self.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
