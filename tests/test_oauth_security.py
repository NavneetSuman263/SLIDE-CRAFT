import os
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import app as slidecraft_app


class OAuthAndAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = slidecraft_app.app.test_client()
        self.db = sqlite3.connect(slidecraft_app.DB_PATH)
        self.db.row_factory = sqlite3.Row
        self.email = f"oauth-conflict-{uuid.uuid4().hex}@example.com"
        self.db.execute(
            "INSERT INTO users (name, email, password_hash, auth_provider) VALUES (?, ?, ?, ?)",
            ("Existing User", self.email, slidecraft_app.generate_password_hash("secret123"), "email"),
        )
        self.db.commit()

    def tearDown(self):
        self.db.execute("DELETE FROM users WHERE email = ?", (self.email,))
        self.db.commit()
        self.db.close()

    def test_unconfigured_google_login_redirects_cleanly(self):
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""}, clear=False):
            response = self.client.get("/login/google")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_guest_mode_allows_workspace_access(self):
        response = self.client.get("/guest")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/create").status_code, 200)

    def test_landing_and_auth_navigation_routes(self):
        landing = self.client.get("/")
        self.assertEqual(landing.status_code, 200)
        self.assertIn(b"Turn big ideas into", landing.data)
        self.assertIn(b'href="/login"', landing.data)
        self.assertIn(b'href="/signup"', landing.data)
        self.assertIn(b'href="/create"', landing.data)

        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.get("/signup").status_code, 200)
        self.assertEqual(self.client.get("/create").status_code, 200)

    def test_home_link_stays_on_original_landing_for_signed_in_and_guest_sessions(self):
        user = self.db.execute("SELECT id FROM users WHERE email = ?", (self.email,)).fetchone()
        with self.client.session_transaction() as session:
            session["user_id"] = user["id"]
        signed_in_home = self.client.get("/")
        self.assertEqual(signed_in_home.status_code, 200)
        self.assertIn(b"Turn big ideas into", signed_in_home.data)
        self.assertEqual(self.client.get("/login").status_code, 200)
        for page in ("/dashboard", "/account", "/create"):
            response = self.client.get(page)
            self.assertEqual(response.status_code, 200, page)
            self.assertIn(b'class="brand sidebar-brand" href="/"', response.data, page)

        with self.client.session_transaction() as session:
            session.clear()
            session["guest"] = True
        guest_home = self.client.get("/")
        self.assertEqual(guest_home.status_code, 200)
        self.assertIn(b"Turn big ideas into", guest_home.data)

    def test_oauth_conflict_does_not_auto_link_accounts(self):
        email = self.email

        class FakeGoogleClient:
            def authorize_access_token(self):
                return {"access_token": "abc"}

            def userinfo(self):
                return {"sub": "google-123", "email": email, "name": "Google User"}

        with patch.object(slidecraft_app, "provider_configured", return_value=True), patch.object(
            slidecraft_app.oauth, "create_client", return_value=FakeGoogleClient()
        ):
            response = self.client.get("/auth/google/callback")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))
        user = self.db.execute("SELECT * FROM users WHERE email = ?", (self.email,)).fetchone()
        self.assertIsNotNone(user)
        self.assertEqual(user["auth_provider"], "email")
        self.assertIsNone(user["provider_user_id"])


if __name__ == "__main__":
    unittest.main()
