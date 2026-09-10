"""Pakasir hosted QRIS checkout and server-side transaction verification."""

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request


class PakasirError(Exception):
    def __init__(self, code=0):
        self.code = code
        super().__init__("Pakasir request failed.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PakasirError(code)


class Pakasir:
    BASE_URL = "https://app.pakasir.com"
    MAX_RESPONSE_BYTES = 1024 * 1024

    def __init__(self, project, api_key):
        if (
            not isinstance(project, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", project) is None
            or not isinstance(api_key, str)
            or not api_key.strip()
        ):
            raise PakasirError()
        self.project = project
        self._api_key = api_key
        self._opener = urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _validate(reference, amount):
        if (
            not isinstance(reference, str)
            or not reference.strip()
            or type(amount) is not int
            or amount <= 0
        ):
            raise PakasirError()

    def checkout_url(self, reference, amount):
        self._validate(reference, amount)
        # ponytail: QRIS only; remove this flag to offer other hosted methods.
        try:
            query = urllib.parse.urlencode({"order_id": reference, "qris_only": 1})
            return f"{self.BASE_URL}/pay/{self.project}/{amount}?{query}"
        except (ValueError, UnicodeError):
            raise PakasirError() from None

    def _request(self, endpoint, reference, amount, *, post=False):
        self._validate(reference, amount)
        payload = {
            "project": self.project,
            "order_id": reference,
            "amount": amount,
            "api_key": self._api_key,
        }
        try:
            url = f"{self.BASE_URL}/api/{endpoint}"
            headers = {"Accept": "application/json"}
            data = None
            if post:
                data = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"
            else:
                url += "?" + urllib.parse.urlencode(payload)
            request = urllib.request.Request(url, data=data, headers=headers)
            with self._opener.open(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise PakasirError(response.status)
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
            if len(raw) > self.MAX_RESPONSE_BYTES:
                raise PakasirError()
            result = json.loads(raw)
            if (
                not isinstance(result, dict)
                or result.get("error")
                or result.get("success") is False
                or result.get("status") in ("error", "failed")
            ):
                raise PakasirError()
            return result
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise PakasirError(code) from None
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            raise PakasirError() from None

    def detail(self, reference, amount):
        try:
            result = self._request("transactiondetail", reference, amount)
        except PakasirError as exc:
            if exc.code == 404:
                return {"status": "not_found"}
            raise
        transaction = result.get("transaction")
        if (
            not isinstance(transaction, dict)
            or transaction.get("project") != self.project
            or transaction.get("order_id") != reference
            or type(transaction.get("amount")) is not int
            or transaction["amount"] != amount
            or not isinstance(transaction.get("status"), str)
            or not transaction["status"].strip()
        ):
            raise PakasirError()
        return transaction

    def cancel(self, reference, amount):
        """Request cancellation; the caller must recheck detail for final status."""
        self._request("transactioncancel", reference, amount, post=True)
