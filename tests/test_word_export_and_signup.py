import io
import re
import sqlite3
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from docx import Document
from pptx import Presentation
from werkzeug.security import check_password_hash

import app as slidecraft_app


class WordExportTests(unittest.TestCase):
    def request_export(self, endpoint, payload):
        client = slidecraft_app.app.test_client()
        client.get("/create")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        return client.post(
            endpoint,
            headers={"X-CSRFToken": csrf},
            json=payload,
        )

    def test_word_export_contains_ordered_slide_content_and_attribution(self):
        response = self.request_export(
            "/api/export/word",
            {
                "title": "AI healthcare",
                "slides": [
                    {
                        "title": "Opening ✨",
                        "body": "A concise explanation gives the audience context before the presentation introduces its main supporting points.",
                        "bullets": ["First point", "Second point"],
                        "notes": "Speaker note, preserved exactly.",
                        "visual": {
                            "description": "A clinician reviewing a chart.",
                            "image_asset": "c" * 64,
                            "image_credit": "Example Photographer",
                            "image_source": "https://unsplash.com/photos/example",
                        },
                    },
                    {
                        "title": "Measured outcomes",
                        "bullets": ["Outcome detail"],
                        "chart": {
                            "type": "bar",
                            "title": "Change by year",
                            "labels": ["2024", "2025"],
                            "series": [{"name": "Visits", "values": [3, 5]}],
                            "units": "million",
                            "source": "Example annual report",
                        },
                        "diagram": {
                            "type": "flow",
                            "items": ["Assess", "Treat", "Review"],
                            "caption": "Clinical workflow",
                        },
                    },
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.mimetype,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertRegex(
            response.headers["Content-Disposition"],
            r'filename="?slidecraft_ai-healthcare_\d{8}_\d{6}_[a-f0-9]{8}\.docx"?',
        )
        document = Document(io.BytesIO(response.data))
        paragraph_text = [paragraph.text for paragraph in document.paragraphs]
        self.assertIn("AI healthcare", paragraph_text[0])
        self.assertTrue(paragraph_text[1].startswith("Generated "))
        self.assertLess(paragraph_text.index("Opening ✨"), paragraph_text.index("Measured outcomes"))
        for expected in (
            "A concise explanation gives the audience context before the presentation introduces its main supporting points.",
            "First point",
            "Second point",
            "Speaker note, preserved exactly.",
            "A clinician reviewing a chart.",
            "Photo by Example Photographer",
            "Change by year",
            "2024: 3 million, 2025: 5 million",
            "Source: Example annual report",
            "Assess",
            "Clinical workflow",
        ):
            self.assertTrue(any(expected in text for text in paragraph_text), expected)
        headings = [p.text for p in document.paragraphs if p.style.name == "Heading 1"]
        self.assertEqual(headings, ["Opening ✨", "Measured outcomes"])
        self.assertEqual(document.core_properties.title, "AI healthcare")

    def test_word_export_rejects_empty_or_invalid_slide_content(self):
        for slides, expected_message in (
            ([], "between 1 and"),
            ([{}], "no valid slide content"),
            (["not a slide"], "no valid slide content"),
        ):
            with self.subTest(slides=slides):
                response = self.request_export(
                    "/api/export/word",
                    {"slides": slides},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_message, response.get_json()["error"])

    def test_readiness_shortener_and_template_api_smoke(self):
        shorten_client = slidecraft_app.app.test_client()
        shorten_client.get("/shorten")
        with shorten_client.session_transaction() as session:
            csrf = session["csrf_token"]
        shortened = shorten_client.post(
            "/api/shorten",
            headers={"X-CSRFToken": csrf},
            json={"text": "Artificial intelligence can support clinicians while preserving the important role of human judgment.", "level": "Highly Concise"},
        )
        self.assertEqual(shortened.status_code, 200)
        self.assertTrue(shortened.get_json()["shortened"])

        templates = shorten_client.post(
            "/api/templates/recommend",
            headers={"X-CSRFToken": csrf},
            json={"topic": "AI healthcare research", "audience": "Academic", "purpose": "Research summary"},
        )
        self.assertEqual(templates.status_code, 200)
        recommendations = templates.get_json()["recommendations"]
        self.assertEqual(len(recommendations), 3)
        self.assertTrue(all(1 <= item["score"] <= 100 for item in recommendations))

    def test_pptx_and_docx_aliases_download_supported_formats(self):
        slides = [{"title": "Format aliases", "bullets": ["PPTX and DOCX"]}]
        jobs = (
            ("pptx", "/api/export/pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            ("docx", "/api/export/docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        )
        for extension, endpoint, mimetype in jobs:
            with self.subTest(extension=extension):
                response = self.request_export(endpoint, {"title": "Format aliases", "slides": slides})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, mimetype)
                self.assertIn(f".{extension}", response.headers["Content-Disposition"])

    def test_concurrent_powerpoint_word_and_pdf_exports_have_unique_names(self):
        slides = [{"title": "Parallel export", "bullets": ["Same content"]}]
        jobs = [
            ("/api/export", {"title": "Parallel export", "slides": slides}),
            ("/api/export/word", {"title": "Parallel export", "slides": slides}),
            ("/api/export/pdf", {"title": "Parallel export", "slides": slides, "preset": "presentation"}),
        ] * 2
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda job: self.request_export(*job), jobs))

        filenames = []
        expected_types = {
            "/api/export": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "/api/export/word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "/api/export/pdf": "application/pdf",
        }
        for (endpoint, _), response in zip(jobs, responses):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, expected_types[endpoint])
            filename = re.search(r'filename="?([^";]+)"?', response.headers["Content-Disposition"]).group(1)
            filenames.append(filename)
            if endpoint == "/api/export":
                self.assertTrue(Presentation(io.BytesIO(response.data)).slides)
            elif endpoint == "/api/export/word":
                self.assertIn("Parallel export", [p.text for p in Document(io.BytesIO(response.data)).paragraphs])
            else:
                self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertEqual(len(filenames), len(set(filenames)))

    def test_export_filenames_sanitize_untrusted_titles(self):
        first = slidecraft_app.export_filename(r"..\CON: AI/Healthcare?", "docx")
        second = slidecraft_app.export_filename(r"..\CON: AI/Healthcare?", "docx")
        self.assertRegex(first, r"^slidecraft_con-ai-healthcare_\d{8}_\d{6}_[a-f0-9]{8}\.docx$")
        self.assertNotRegex(first, r'[<>:"/\\|?*]')
        self.assertNotIn("..", first)
        self.assertNotEqual(first, second)


class SignupEmailTests(unittest.TestCase):
    def setUp(self):
        self.client = slidecraft_app.app.test_client()
        self.client.get("/signup")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]
        self.email = f"slidecraft-{uuid.uuid4().hex}@gmail.com"
        self.test_emails = set()

    def signup(self, email):
        self.test_emails.add(email.strip().lower())
        return self.client.post(
            "/signup",
            headers={"X-CSRFToken": self.csrf},
            data={
                "name": "SlideCraft Test",
                "email": email,
                "password": "secure-test-password",
            },
        )

    def tearDown(self):
        with slidecraft_app.db_connect() as db:
            db.executemany("DELETE FROM users WHERE email = ?", ((email,) for email in self.test_emails))

    def test_malformed_emails_are_rejected_beside_the_email_field(self):
        for email in (
            "not-an-email",
            "person@",
            "@example.com",
            "person@example",
            "person..dots@example.com",
            f"{'a' * 255}@example.com",
        ):
            with self.subTest(email=email):
                response = self.signup(email)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Enter a valid email address", response.data)
                self.assertIn(b'aria-invalid="true"', response.data)

    def test_valid_gmail_and_other_provider_are_normalized_and_login_works(self):
        response = self.signup(f"  {self.email.upper()}  ")
        self.assertEqual(response.status_code, 302)
        with slidecraft_app.db_connect() as db:
            user = db.execute("SELECT * FROM users WHERE email = ?", (self.email,)).fetchone()
        self.assertIsNotNone(user)
        self.assertTrue(check_password_hash(user["password_hash"], "secure-test-password"))
        self.assertNotEqual(user["password_hash"], "secure-test-password")

        self.client.get("/logout")
        self.client.get("/login")
        with self.client.session_transaction() as session:
            login_csrf = session["csrf_token"]
        login = self.client.post(
            "/login",
            headers={"X-CSRFToken": login_csrf},
            data={"email": self.email.upper(), "password": "secure-test-password"},
        )
        self.assertEqual(login.status_code, 302)
        self.assertIn("/dashboard", login.headers["Location"])

        other_provider = f"slidecraft-{uuid.uuid4().hex}@example.org"
        other_response = self.signup(other_provider)
        self.assertEqual(other_response.status_code, 302)

    def test_duplicate_email_is_rejected_case_insensitively(self):
        self.assertEqual(self.signup(self.email).status_code, 302)
        self.client.get("/signup")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]
        duplicate = self.signup(self.email.upper())
        self.assertEqual(duplicate.status_code, 200)
        self.assertIn(b"An account with this email already exists.", duplicate.data)

    def test_database_email_uniqueness_is_case_insensitive(self):
        first = f"slidecraft-{uuid.uuid4().hex}@example.org"
        self.test_emails.add(first)
        with slidecraft_app.db_connect() as db:
            db.execute(
                "INSERT INTO users(name, email, password_hash) VALUES (?, ?, ?)",
                ("First", first, "hash"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO users(name, email, password_hash) VALUES (?, ?, ?)",
                    ("Second", first.upper(), "hash"),
                )
            db.execute("DELETE FROM users WHERE email = ?", (first,))


if __name__ == "__main__":
    unittest.main()
