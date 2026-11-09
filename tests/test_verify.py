import unittest
from unittest.mock import patch

from cloudcrew.verify import NoRedirect, tls_evidence


class VerificationTests(unittest.TestCase):
    def test_branded_custom_error_is_detected_without_a_copy_specific_sentence(self):
        from cloudcrew.verify import fetch
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.code = 404
        response.headers = {"Content-Type": "text/html"}
        response.read.return_value = b'<html><title>Page not found | GBM Cloud</title><main data-error-page="404">Page missing</main></html>'
        with patch("cloudcrew.verify.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            result = fetch("https://example.test/missing")
        self.assertTrue(result["custom_404_present"])
        self.assertEqual(result["status_code"], 404)

    def test_redirects_are_observed_not_automatically_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 301, "Moved", {}, "https://example.test/"))

    def test_tls_uses_hostname_and_certificate_validation(self):
        with patch("cloudcrew.verify.ssl.create_default_context") as context, patch("cloudcrew.verify.socket.create_connection") as connect:
            tls_evidence("project.example.test")
            context.assert_called_once_with()
            self.assertEqual(context.return_value.wrap_socket.call_args.kwargs["server_hostname"], "project.example.test")
            self.assertEqual(connect.call_args.args[0], ("project.example.test", 443))


if __name__ == "__main__":
    unittest.main()
