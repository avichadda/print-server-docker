from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["PRINT_SERVER_DATA_DIR"] = TEST_DATA.name

from manager.app import app  # noqa: E402
from manager.services import (  # noqa: E402
    PrinterServiceError,
    validate_printer_uri,
    validate_queue_name,
    validate_subnets,
)


class ValidationTests(unittest.TestCase):
    def test_accepts_valid_queue_name(self) -> None:
        self.assertEqual(validate_queue_name("Canon_G3010"), "Canon_G3010")

    def test_rejects_command_like_queue_name(self) -> None:
        with self.assertRaises(PrinterServiceError):
            validate_queue_name("printer; rm")

    def test_accepts_driverless_ipp_uri(self) -> None:
        uri = "ipp://192.168.1.22/ipp/print"
        self.assertEqual(validate_printer_uri(uri), uri)

    def test_rejects_uri_credentials(self) -> None:
        with self.assertRaises(PrinterServiceError):
            validate_printer_uri("ipp://user:secret@192.168.1.22/ipp/print")

    def test_rejects_public_discovery_network(self) -> None:
        with self.assertRaises(PrinterServiceError):
            validate_subnets("8.8.8.0/24")

    def test_limits_discovery_address_count(self) -> None:
        with self.assertRaises(PrinterServiceError):
            validate_subnets("10.0.0.0/21")


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        app.config.update(TESTING=True)
        self.client = app.test_client()
        with app.app_context():
            from manager.app import database

            with database() as connection:
                connection.execute("DELETE FROM users")
                connection.execute("DELETE FROM settings")

    def csrf_token(self) -> str:
        self.client.get("/onboarding")
        with self.client.session_transaction() as current_session:
            return str(current_session["csrf_token"])

    def test_rejects_post_without_csrf_token(self) -> None:
        response = self.client.post(
            "/onboarding",
            data={
                "username": "owner",
                "password": "correct-horse-battery-staple",
                "password_confirmation": "correct-horse-battery-staple",
            },
        )
        self.assertEqual(response.status_code, 400)

    @patch("manager.app.discover_printers", return_value=([], []))
    def test_creates_owner_and_requires_login_after_logout(self, _: object) -> None:
        response = self.client.post(
            "/onboarding",
            data={
                "csrf_token": self.csrf_token(),
                "username": "owner",
                "password": "correct-horse-battery-staple",
                "password_confirmation": "correct-horse-battery-staple",
                "discovery_subnets": "192.168.1.0/24",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/printers/discover?scan=1", response.location)

        with self.client.session_transaction() as current_session:
            token = str(current_session["csrf_token"])
        response = self.client.post("/logout", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_rejects_short_owner_password(self) -> None:
        response = self.client.post(
            "/onboarding",
            data={
                "csrf_token": self.csrf_token(),
                "username": "owner",
                "password": "short",
                "password_confirmation": "short",
                "discovery_subnets": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"at least 10 characters", response.data)


if __name__ == "__main__":
    unittest.main()
