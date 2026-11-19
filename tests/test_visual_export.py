import io
import json
import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image
from pptx import Presentation
from pptx.util import Inches, Pt

import app as slidecraft_app


class VisualExportTests(unittest.TestCase):
    def test_firefly_is_disabled_without_server_credentials(self):
        client = slidecraft_app.app.test_client()
        client.get("/create")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        with patch.dict(os.environ, {
            "ADOBE_FIREFLY_CLIENT_ID": "",
            "ADOBE_FIREFLY_CLIENT_SECRET": "",
            "ADOBE_FIREFLY_SCOPES": "",
        }, clear=False):
            status = client.get("/api/firefly/status")
            response = client.post(
                "/api/firefly/generate",
                headers={"X-CSRFToken": csrf},
                json={"prompt": "A clean presentation image", "slide_index": 0},
            )
        self.assertFalse(status.get_json()["available"])
        self.assertEqual(response.status_code, 503)

    def test_quiz_fallback_preserves_requested_choice_format(self):
        client = slidecraft_app.app.test_client()
        client.get("/quiz")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        deck = slidecraft_app.make_pptx([
            {"title": "Storage", "bullets": ["Batteries store excess energy", "Stored energy supports peak demand"]},
            {"title": "Grid", "bullets": ["Grid operators balance supply", "Renewables vary with weather"]},
        ])
        deck.seek(0)
        with patch.object(slidecraft_app, "ai_json", return_value=None):
            response = client.post(
                "/api/quiz",
                headers={"X-CSRFToken": csrf},
                data={
                    "file": (deck, "energy.pptx"),
                    "count": "3",
                    "difficulty": "Medium",
                    "type": "Multiple Choice",
                },
            )
        self.assertEqual(response.status_code, 200)
        questions = response.get_json()["questions"]
        self.assertTrue(questions)
        for question in questions:
            self.assertEqual(question["type"], "Multiple Choice")
            self.assertIn(question["answer"], question["options"])
            self.assertGreaterEqual(len(question["options"]), 2)

    def test_quiz_powerpoint_export_keeps_all_questions_and_options_across_slides(self):
        client = slidecraft_app.app.test_client()
        client.get("/quiz")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        long_question = ("Which statement best describes this topic? " + ("Review the source material carefully. " * 20))[:500]
        long_options = [
            (f"Choice {index + 1}: " + ("A detailed explanation of this answer. " * 10))[:300]
            for index in range(6)
        ]
        questions = [
            {
                "question": long_question,
                "type": "Multiple Choice",
                "options": long_options,
                "answer": long_options[0],
                "explanation": "The first choice is supported by the presentation.",
            },
            {
                "question": "Does the deck identify the main finding?",
                "type": "True or False",
                "options": ["True", "False"],
                "answer": "True",
                "explanation": "The finding appears in the source material.",
            },
        ]
        response = client.post(
            "/api/quiz/export/pptx",
            headers={"X-CSRFToken": csrf},
            json={"questions": questions, "difficulty": "Medium"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.mimetype,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        presentation = Presentation(io.BytesIO(response.data))
        self.assertGreater(len(presentation.slides), len(questions))
        slide_text = "\n".join(
            shape.text
            for slide in presentation.slides
            for shape in slide.shapes
            if shape.has_text_frame
        )
        for question in questions:
            self.assertIn(question["question"].strip(), slide_text)
            for option in question["options"]:
                self.assertIn(option.strip(), slide_text)
        notes = "\n".join(slide.notes_slide.notes_text_frame.text for slide in presentation.slides)
        self.assertIn("Correct answer:", notes)
        self.assertTrue(json.loads(response.headers["X-SlideCraft-Quality-Report"])["passed"])

    def test_quality_report_flags_unresolved_text_overflow(self):
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(0.2))
        shape.text_frame.text = "A very long line of text that cannot fit in this small text box. " * 8
        shape.text_frame.paragraphs[0].runs[0].font.size = Pt(8)
        report = slidecraft_app.validate_and_correct_pptx(presentation)
        self.assertGreater(report["unresolved_text_overflow"], 0)
        self.assertFalse(report["passed"])

    def test_shortener_rejects_bad_level_and_keeps_source_numbers(self):
        client = slidecraft_app.app.test_client()
        client.get("/shorten")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        headers = {"X-CSRFToken": csrf}
        invalid = client.post(
            "/api/shorten",
            headers=headers,
            json={"text": "A useful sentence to shorten.", "level": "unlimited"},
        )
        self.assertEqual(invalid.status_code, 400)
        original = "By 2024, the team delivered 30 percent more renewable energy to the regional grid."
        with patch.object(
            slidecraft_app, "ai_json",
            return_value={"shortened": "The team delivered more energy to the grid."},
        ):
            response = client.post(
                "/api/shorten",
                headers=headers,
                json={"text": original, "level": "Highly Concise"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("2024", payload["shortened"])
        self.assertLess(len(payload["shortened"].split()), len(original.split()))

    def test_firefly_generation_preview_and_accept_use_cached_image(self):
        client = slidecraft_app.app.test_client()
        client.get("/create")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        job_id = f"firefly_{uuid.uuid4().hex}"
        image_buffer = io.BytesIO()
        Image.new("RGB", (32, 18), (42, 103, 255)).save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue()

        generation_response = MagicMock()
        generation_response.json.return_value = {"jobId": job_id}
        status_response = MagicMock()
        status_response.json.return_value = {
            "status": "succeeded",
            "result": {"outputs": [{"image": {"url": "https://assets.adobe.com/generated.png"}}]},
        }
        image_response = MagicMock()
        image_response.__enter__.return_value = image_response
        image_response.iter_content.return_value = [image_bytes]

        with tempfile.TemporaryDirectory() as cache_dir, patch.object(
            slidecraft_app, "FIREFLY_CACHE_DIR", Path(cache_dir)
        ), patch.object(
            slidecraft_app, "firefly_is_configured", return_value=True
        ), patch.object(
            slidecraft_app, "firefly_headers",
            return_value={"x-api-key": "test-key", "Authorization": "Bearer test-token"},
        ), patch.object(
            slidecraft_app.requests, "post", return_value=generation_response
        ) as post_request, patch.object(
            slidecraft_app.requests, "get", side_effect=[status_response, image_response]
        ):
            start = client.post(
                "/api/firefly/generate",
                headers={"X-CSRFToken": csrf},
                json={
                    "prompt": "A clear energy-storage visual",
                    "negative_prompt": "text",
                    "slide_index": 2,
                    "num_variations": 1,
                    "model": "image4_standard",
                    "aspect_ratio": "16:9",
                },
            )
            self.assertEqual(start.status_code, 202)
            self.assertEqual(post_request.call_args.kwargs["headers"]["x-model-version"], "image4_standard")
            self.assertEqual(
                post_request.call_args.kwargs["json"]["size"],
                {"width": 2688, "height": 1536},
            )
            status = client.get(f"/api/firefly/jobs/{job_id}")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.get_json()["status"], "succeeded")
            accepted = client.post(
                "/api/firefly/accept",
                headers={"X-CSRFToken": csrf},
                json={"job_id": job_id, "output_index": 0},
            )
            image_response = client.get(
                f"/api/slide-image/{accepted.get_json()['visual']['image_asset']}"
            )
            image_mimetype = image_response.mimetype
            image_response.close()
            firefly_presentation = Presentation(slidecraft_app.make_pptx([{
                "title": "Firefly-generated visual",
                "bullets": ["A generated visual is embedded in the PowerPoint."],
                "visual": accepted.get_json()["visual"],
            }]))

        self.assertEqual(accepted.status_code, 200)
        visual = accepted.get_json()["visual"]
        self.assertEqual(visual["image_provider"], "adobe_firefly")
        self.assertEqual(visual["image_credit"], "Adobe Firefly")
        self.assertEqual(image_mimetype, "image/png")
        self.assertTrue(any(shape.shape_type == 13 for shape in firefly_presentation.slides[0].shapes))

    def test_layout_recommendations_only_return_existing_slide_layouts(self):
        recommendations = slidecraft_app.recommend_templates(
            "Research data workflow", "Academic", "Research summary",
            {"needs_charts": True, "needs_diagrams": True},
        )
        self.assertEqual(len(recommendations), 3)
        self.assertTrue(all(item["layout"] in slidecraft_app.SELECTABLE_LAYOUTS for item in recommendations))
        self.assertTrue(all("description" in item and "preview" not in item for item in recommendations))

    def test_generation_applies_selected_layout_and_preserves_cover_and_conclusion(self):
        client = slidecraft_app.app.test_client()
        create_page = client.get("/create")
        self.assertIn(b'name="template" id="generation-layout"', create_page.data)
        for internal_copy in (
            b'name="provider"',
            b"Gemini",
            b"Groq",
            b"Gemini and Groq run independently",
            b"The cover and closing slides keep their dedicated layouts.",
            b"Charts and diagrams are used only when the generated slide has matching content.",
        ):
            self.assertNotIn(internal_copy, create_page.data)
        self.assertIn(b'<option value="metrics">Key metrics</option>', create_page.data)
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        slides = slidecraft_app.offline_slides(
            "Grid-scale energy storage", 4, "General audience", "Presentation"
        )
        with patch.object(
            slidecraft_app, "generate_slides_with_fallback",
            return_value=(slides, "offline", "Test generation"),
        ), patch.object(
            slidecraft_app, "enrich_slide_visuals",
            return_value="Unsplash is not configured.",
        ):
            response = client.post(
                "/api/generate",
                headers={"X-CSRFToken": csrf},
                json={
                    "topic": "Grid-scale energy storage",
                    "count": 4,
                    "template": "metrics",
                },
            )

        self.assertEqual(response.status_code, 200)
        generated = response.get_json()["slides"]
        self.assertEqual(generated[0]["visual"]["layout"], "cover")
        self.assertEqual(generated[-1]["visual"]["layout"], "conclusion")
        self.assertEqual(
            [slide["visual"]["layout"] for slide in generated[1:-1]],
            ["metrics", "metrics"],
        )

    def test_slide_prompt_requires_detailed_content_and_parser_rejects_outlines(self):
        prompt = slidecraft_app.build_slide_prompt(
            "Grid-scale energy storage", 4, "General audience", "Presentation", "Professional"
        )
        self.assertIn('"body":', prompt)
        self.assertIn("25-35 word explanatory body", prompt)
        self.assertIn("opening slide must establish", prompt)
        self.assertIn("final slide must synthesize", prompt)

        with self.assertRaisesRegex(ValueError, "detailed explanation"):
            slidecraft_app.parse_generated_slides(
                json.dumps({"slides": [{
                    "title": "Storage benefits",
                    "bullets": ["Introduction", "Benefits", "Challenges"],
                    "notes": "Short note",
                }]}),
                1,
            )

    def test_generation_does_not_replace_configured_provider_failure_with_outline(self):
        client = slidecraft_app.app.test_client()
        client.get("/create")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "configured",
            "GROQ_API_KEY": "",
        }), patch.object(
            slidecraft_app,
            "generate_slides_with_fallback",
            return_value=(None, "offline", "Gemini returned invalid or incomplete content."),
        ), patch.object(slidecraft_app, "enrich_slide_visuals") as enrich:
            response = client.post(
                "/api/generate",
                headers={"X-CSRFToken": csrf},
                json={"topic": "Grid-scale energy storage", "count": 4},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"],
            "We couldn't create your presentation right now. Please try again in a moment.",
        )
        self.assertNotIn("Gemini", response.get_json()["error"])
        self.assertNotIn("Groq", response.get_json()["error"])
        enrich.assert_not_called()

    def test_explicit_provider_selection_does_not_fall_back_to_the_other_provider(self):
        slides = slidecraft_app.offline_slides("Provider selection", 4)
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "configured",
            "GROQ_API_KEY": "configured",
        }), patch.object(
            slidecraft_app, "gemini_generate", return_value=(None, "Gemini failed.")
        ) as gemini, patch.object(slidecraft_app, "groq_generate") as groq:
            result, provider, detail = slidecraft_app.generate_slides_with_fallback(
                "Provider selection",
                4,
                "General audience",
                "Presentation",
                "Professional",
                preferred_provider="gemini",
            )

        self.assertIsNone(result)
        self.assertEqual(provider, "gemini")
        self.assertEqual(detail, "Gemini failed.")
        gemini.assert_called_once()
        groq.assert_not_called()

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "configured",
            "GROQ_API_KEY": "configured",
        }), patch.object(
            slidecraft_app, "gemini_generate"
        ) as gemini, patch.object(
            slidecraft_app, "groq_generate", return_value=(slides, "Groq generated content.")
        ) as groq:
            result, provider, _detail = slidecraft_app.generate_slides_with_fallback(
                "Provider selection",
                4,
                "General audience",
                "Presentation",
                "Professional",
                preferred_provider="groq",
            )

        self.assertEqual(provider, "groq")
        self.assertEqual(len(result), 4)
        gemini.assert_not_called()
        groq.assert_called_once()

    def test_generation_rejects_unknown_layout(self):
        client = slidecraft_app.app.test_client()
        client.get("/create")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        response = client.post(
            "/api/generate",
            headers={"X-CSRFToken": csrf},
            json={"topic": "Grid-scale energy storage", "count": 4, "template": "Executive Blue"},
        )
        self.assertEqual(response.status_code, 400)

    def test_pptx_quality_validation_passes_for_generated_deck(self):
        output = slidecraft_app.make_pptx([
            {"title": "A clear opening", "bullets": ["One concise point"], "notes": "Speaker note"},
            {"title": "Conclusion", "bullets": ["A clear next step"], "notes": ""},
        ])
        presentation = Presentation(output)
        report = slidecraft_app.validate_and_correct_pptx(presentation)
        self.assertEqual(report["slides_checked"], 2)
        self.assertEqual(report["total_slide_count"], 2)
        self.assertIn("structural validation", report["rendering"])
        self.assertFalse(report["visual_rendering_verified"])

    def test_legacy_title_bullets_and_notes_export(self):
        legacy_slide = {
            "title": "Legacy slide",
            "bullets": ["A first point", "A second point"],
            "notes": "Legacy speaker notes are preserved.",
        }
        output = slidecraft_app.make_pptx([legacy_slide])
        presentation = Presentation(io.BytesIO(output.read()))
        slide = presentation.slides[0]
        text = "\n".join(shape.text for shape in slide.shapes if shape.has_text_frame)
        self.assertIn("Legacy slide", text)
        self.assertIn("A first point", text)
        self.assertIn("Legacy speaker notes are preserved", slide.notes_slide.notes_text_frame.text)

    def test_export_embeds_photos_charts_diagrams_and_notes(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = Path(cache_dir)
            asset = "a" * 64
            Image.new("RGB", (640, 360), (42, 103, 255)).save(cache_path / f"{asset}.jpg", "JPEG")
            slides = [
                {
                    "title": "A visual cover",
                    "bullets": ["Introduce the topic"],
                    "notes": "Cover speaker notes are retained.",
                    "visual": {
                        "layout": "cover",
                        "image_asset": asset,
                        "image_url": "https://images.unsplash.com/photo-test?ixid=test",
                        "image_alt": "Blue abstract cover",
                        "image_credit": "Test Photographer",
                        "image_source": "https://unsplash.com/photos/test-photo?utm_source=slidecraft_studio&utm_medium=referral",
                        "photographer_url": "https://unsplash.com/@test-photographer?utm_source=slidecraft_studio&utm_medium=referral",
                        "unsplash_url": "https://unsplash.com/?utm_source=slidecraft_studio&utm_medium=referral",
                    },
                }
            ]
            chart_types = ("bar", "line", "pie", "doughnut", "comparison")
            for chart_type in chart_types:
                slides.append({
                    "title": f"{chart_type.title()} chart",
                    "bullets": ["Compare the supported values."],
                    "notes": f"Notes for {chart_type}.",
                    "visual": {"layout": "chart"},
                    "chart": {
                        "type": chart_type,
                        "title": "Verified sample",
                        "labels": ["One", "Two", "Three"],
                        "series": [{"name": "Units", "values": [12, 24, 36]}],
                        "units": "items",
                        "source": "Test dataset",
                    },
                })
            slides.extend([
                {
                    "title": "Editable workflow",
                    "bullets": ["Shape-based flow"],
                    "notes": "Diagram notes are retained.",
                    "visual": {"layout": "diagram"},
                    "diagram": {"type": "flow", "items": ["Input", "Process", "Outcome"]},
                },
                {
                    "title": "The takeaway",
                    "bullets": ["A designed closing slide"],
                    "notes": "Closing notes are retained.",
                    "visual": {"layout": "conclusion"},
                },
            ])
            with patch.object(slidecraft_app, "IMAGE_CACHE_DIR", cache_path):
                output = slidecraft_app.make_pptx(slides)

        presentation = Presentation(io.BytesIO(output.read()))
        self.assertEqual(len(presentation.slides), 8)
        self.assertTrue(any(shape.shape_type == 13 for shape in presentation.slides[0].shapes))
        attribution_links = {
            relation.target_ref
            for relation in presentation.slides[0].part.rels.values()
            if relation.is_external
        }
        self.assertTrue(any("unsplash.com/@test-photographer" in link for link in attribution_links))
        self.assertTrue(any("unsplash.com/?utm_source=slidecraft_studio" in link for link in attribution_links))
        chart_shapes = [
            shape for slide in presentation.slides for shape in slide.shapes if shape.has_chart
        ]
        self.assertEqual(len(chart_shapes), len(chart_types))
        diagram_slide = presentation.slides[6]
        self.assertGreater(len(diagram_slide.shapes), 5)
        self.assertIn("Cover speaker notes are retained", presentation.slides[0].notes_slide.notes_text_frame.text)
        self.assertIn("Diagram notes are retained", diagram_slide.notes_slide.notes_text_frame.text)

    def test_chart_schema_rejects_mismatched_or_unsourced_data(self):
        base_chart = {
            "type": "bar",
            "labels": ["A", "B"],
            "series": [{"name": "Values", "values": [1, 2]}],
        }
        with self.assertRaisesRegex(ValueError, "source"):
            slidecraft_app.normalize_chart(base_chart)

        base_chart["source"] = "Verified source"
        base_chart["series"][0]["values"] = [1]
        with self.assertRaisesRegex(ValueError, "one value per label"):
            slidecraft_app.normalize_chart(base_chart)

    def test_native_fallback_and_safe_image_route(self):
        slides = slidecraft_app.offline_slides("Climate adaptation", 4)
        with patch.dict("os.environ", {"UNSPLASH_ACCESS_KEY": ""}):
            status = slidecraft_app.enrich_slide_visuals(slides, "Climate adaptation")
        self.assertIn("native editable visuals", status)
        self.assertTrue(all(not slide["visual"]["image_asset"] for slide in slides))

        response = slidecraft_app.app.test_client().get("/api/slide-image/not-a-cache-id")
        self.assertEqual(response.status_code, 404)

    def test_export_api_accepts_expanded_slide_schema(self):
        client = slidecraft_app.app.test_client()
        client.get("/create")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        response = client.post(
            "/api/export",
            headers={"X-CSRFToken": csrf},
            json={
                "slides": [{
                    "title": "A process",
                    "bullets": ["Start", "Finish"],
                    "notes": "Retain this speaker note.",
                    "visual": {"layout": "diagram", "description": "A two-step process."},
                    "diagram": {"type": "flow", "items": ["Start", "Finish"]},
                }],
            },
        )
        self.assertEqual(response.status_code, 200)
        exported = Presentation(io.BytesIO(response.data))
        self.assertEqual(len(exported.slides), 1)
        self.assertIn("Retain this speaker note", exported.slides[0].notes_slide.notes_text_frame.text)
        self.assertGreater(len(exported.slides[0].shapes), 5)

    def test_pdf_export_supports_page_presets_notes_and_handouts(self):
        client = slidecraft_app.app.test_client()
        client.get("/create")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        slides = [
            {
                "title": "PDF presentation",
                "bullets": ["Readable content", "Keeps slide order"],
                "notes": "Speaker note for the first slide.",
            },
            {
                "title": "Editable process",
                "bullets": ["The process is shown below."],
                "notes": "Explain each step.",
                "diagram": {"type": "flow", "items": ["Plan", "Build", "Share"]},
            },
            {
                "title": "Chart page",
                "bullets": ["Illustrative data is labeled."],
                "chart": {
                    "type": "bar",
                    "title": "Sample values",
                    "labels": ["A", "B"],
                    "series": [{"name": "Units", "values": [2, 5]}],
                    "illustrative": True,
                },
            },
        ]
        expected_pages = {
            "presentation": 3,
            "print": 3,
            "a4_landscape": 3,
            "a4_portrait": 3,
            "letter": 3,
            "compressed": 3,
            "speaker_notes": 6,
            "handout_2": 2,
            "handout_4": 1,
            "handout_6": 1,
        }
        for preset, page_count in expected_pages.items():
            with self.subTest(preset=preset):
                response = client.post(
                    "/api/export/pdf",
                    headers={"X-CSRFToken": csrf},
                    json={"slides": slides, "preset": preset},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "application/pdf")
                self.assertIn(b"%PDF-", response.data[:8])
                self.assertTrue(response.data.rstrip().endswith(b"%%EOF"))
                self.assertEqual(
                    len(re.findall(rb"/Type\s*/Page\b", response.data)),
                    page_count,
                )
                self.assertRegex(
                    response.headers["Content-Disposition"],
                    rf'filename="?slidecraft_presentation_{preset.replace("_", "-")}_\d{{8}}_\d{{6}}_[a-f0-9]{{8}}\.pdf"?',
                )

        invalid = client.post(
            "/api/export/pdf",
            headers={"X-CSRFToken": csrf},
            json={"slides": slides, "preset": "powerpoint"},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()["error"], "Choose a supported PDF layout.")

    def test_pdf_export_embeds_cached_photo_asset(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = Path(cache_dir)
            asset = "b" * 64
            Image.new("RGB", (640, 360), (42, 103, 255)).save(cache_path / f"{asset}.jpg", "JPEG")
            client = slidecraft_app.app.test_client()
            client.get("/create")
            with client.session_transaction() as session:
                csrf = session["csrf_token"]
            with patch.object(slidecraft_app, "IMAGE_CACHE_DIR", cache_path):
                response = client.post(
                    "/api/export/pdf",
                    headers={"X-CSRFToken": csrf},
                    json={
                        "preset": "presentation",
                        "slides": [{
                            "title": "A slide with a real photo",
                            "bullets": ["Photo remains embedded in PDF."],
                            "visual": {"image_asset": asset, "image_credit": "Test Photographer"},
                        }],
                    },
                )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/Subtype /Image", response.data)

    def test_unsplash_landscape_search_download_attribution_and_cache(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            image_bytes = io.BytesIO()
            Image.new("RGB", (320, 180), (20, 80, 180)).save(image_bytes, "JPEG")
            search_response = MagicMock()
            search_response.json.return_value = {
                "results": [
                    {
                        "id": "portrait-photo",
                        "width": 320,
                        "height": 640,
                        "urls": {"regular": "https://images.unsplash.com/portrait?ixid=portrait"},
                    },
                    {
                        "id": "landscape-photo",
                        "width": 1600,
                        "height": 900,
                        "urls": {"regular": "https://images.unsplash.com/photo.jpg?ixid=landscape"},
                        "links": {
                            "download_location": "https://api.unsplash.com/photos/landscape-photo/download?ixid=event",
                            "html": "https://unsplash.com/photos/landscape-photo",
                        },
                        "user": {
                            "name": "Test Photographer",
                            "links": {"html": "https://unsplash.com/@test-photographer"},
                        },
                        "alt_description": "Blue visual",
                    },
                ],
            }
            download_event_response = MagicMock()
            image_response = MagicMock()
            image_response.__enter__.return_value = image_response
            image_response.headers = {"Content-Type": "image/jpeg"}
            image_response.iter_content.return_value = [image_bytes.getvalue()]
            with patch.object(
                slidecraft_app, "IMAGE_CACHE_DIR", Path(cache_dir)
            ), patch.dict("os.environ", {"UNSPLASH_ACCESS_KEY": "test-key"}), patch(
                "app.requests.get",
                side_effect=[search_response, download_event_response, image_response, download_event_response],
            ) as get:
                result = slidecraft_app.fetch_unsplash_image("blue visual")
                cached_result = slidecraft_app.fetch_unsplash_image("blue visual")
                image_response_route = slidecraft_app.app.test_client().get(
                    f"/api/slide-image/{result['asset']}"
                )

            self.assertEqual(result, cached_result)
            self.assertEqual(get.call_count, 4)
            search_call = get.call_args_list[0]
            self.assertEqual(search_call.args[0], "https://api.unsplash.com/search/photos")
            self.assertEqual(search_call.kwargs["headers"]["Authorization"], "Client-ID test-key")
            self.assertEqual(search_call.kwargs["headers"]["Accept-Version"], "v1")
            self.assertEqual(search_call.kwargs["params"]["orientation"], "landscape")
            self.assertEqual(search_call.kwargs["params"]["content_filter"], "high")
            self.assertEqual(get.call_args_list[1].args[0], "https://api.unsplash.com/photos/landscape-photo/download?ixid=event")
            self.assertEqual(get.call_args_list[2].args[0], "https://images.unsplash.com/photo.jpg?ixid=landscape")
            self.assertFalse(get.call_args_list[2].kwargs["allow_redirects"])
            self.assertEqual(image_response_route.status_code, 200)
            self.assertEqual(image_response_route.mimetype, "image/jpeg")
            self.assertEqual(image_response_route.get_data(), image_bytes.getvalue())
            self.assertEqual(result["image_credit"], "Test Photographer")
            self.assertEqual(result["image_url"], "https://images.unsplash.com/photo.jpg?ixid=landscape")
            self.assertIn("utm_source=slidecraft_studio", result["photographer_url"])
            self.assertIn("utm_medium=referral", result["unsplash_url"])
            image_response_route.close()

    @unittest.skipUnless(os.getenv("UNSPLASH_ACCESS_KEY", "").strip(), "UNSPLASH_ACCESS_KEY is not configured.")
    def test_live_unsplash_search_and_powerpoint_embed(self):
        query = f"landscape nature presentation {uuid.uuid4().hex}"
        result = slidecraft_app.fetch_unsplash_image(query)
        self.assertIsNotNone(result, "Unsplash did not return a usable landscape photo.")
        self.assertTrue(Path(slidecraft_app.unsplash_cache_path(result["asset"])).is_file())
        self.assertTrue(result["image_url"].startswith("https://images.unsplash.com/"))
        output = slidecraft_app.make_pptx([{
            "title": "Unsplash photo test",
            "bullets": ["Verify the selected landscape photo is embedded."],
            "visual": {"layout": "cover", "image_asset": result["asset"], **result},
        }])
        presentation = Presentation(io.BytesIO(output.read()))
        self.assertTrue(any(shape.shape_type == 13 for shape in presentation.slides[0].shapes))
        links = {
            relation.target_ref
            for relation in presentation.slides[0].part.rels.values()
            if relation.is_external
        }
        self.assertTrue(any("utm_source=slidecraft_studio" in link for link in links))

    def test_gemini_response_preserves_visual_instructions_and_chart_data(self):
        generated_slides = [{
            "title": "Grid storage overview",
            "body": (
                "Grid-scale storage holds electricity when supply exceeds demand and releases it later. "
                "This flexibility helps integrate variable renewable generation effectively."
            ),
            "bullets": [
                "Storage shifts surplus renewable electricity to periods of higher demand.",
                "Fast response can help grid operators balance sudden changes in supply.",
                "A cited source supports the chart's capacity comparison.",
            ],
            "notes": (
                "Explain how storage shifts electricity between different time periods. "
                "Use the cited capacity figures as context and clarify that storage complements "
                "rather than replaces transmission, generation, and demand management."
            ),
            "visual": {"description": "Grid-scale batteries connected to renewable power.", "layout": "split"},
            "chart": {
                "type": "bar",
                "title": "Capacity by source",
                "labels": ["Solar", "Wind"],
                "series": [{"name": "Capacity", "values": [10, 15]}],
                "units": "GW",
                "source": "Verified energy report",
            },
            "diagram": {"type": "flow", "items": ["Renewables", "Storage", "Grid"]},
        }]
        model_response = MagicMock()
        model_text = json.dumps({"slides": generated_slides})
        split_at = len(model_text) // 2
        model_response.json.return_value = {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [
                    {"text": "non-output thought", "thought": True},
                    {"text": model_text[:split_at]},
                    {"text": model_text[split_at:]},
                ]},
            }]
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-gemini-key"}), patch(
            "app.requests.post", return_value=model_response
        ) as post:
            slides, status = slidecraft_app.gemini_generate(
                "Grid storage", 1, "General audience", "Presentation", "Professional"
            )

        self.assertEqual(status, "Gemini generated the presentation content.")
        self.assertEqual(slides[0]["body"], generated_slides[0]["body"])
        self.assertEqual(slides[0]["visual"]["description"], generated_slides[0]["visual"]["description"])
        self.assertEqual(slides[0]["chart"]["source"], "Verified energy report")
        self.assertEqual(slides[0]["diagram"]["items"], ["Renewables", "Storage", "Grid"])
        self.assertEqual(slides[0]["notes"], generated_slides[0]["notes"])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-gemini-key")
        self.assertEqual(
            post.call_args.kwargs["json"]["generationConfig"]["maxOutputTokens"],
            12000,
        )
        self.assertIn(
            '"body":',
            post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"],
        )
        self.assertNotIn("test-gemini-key", post.call_args.args[0])

    def test_groq_uses_server_side_bearer_key(self):
        response = MagicMock()
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({"slides": [{
                        "title": "A test story",
                        "body": (
                            "A focused explanation makes the topic easier to follow. "
                            "It connects the central idea to a practical situation for a general audience."
                        ),
                        "bullets": [
                            "The first point explains the main mechanism in practical terms.",
                            "A second point adds supporting context for the intended audience.",
                            "A final point describes a useful implication or next step.",
                        ],
                        "notes": (
                            "Expand on the mechanism and connect it to an example relevant "
                            "to the audience. Explain any limits and invite a focused question."
                        ),
                    }]})
                }
            }]
        }
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-groq-secret"}), patch(
            "app.requests.post", return_value=response
        ) as post:
            slides, detail = slidecraft_app.groq_generate(
                "A test story", 1, "General audience", "Presentation", "Professional"
            )

        self.assertEqual(len(slides), 1)
        self.assertIn("Groq", detail)
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer test-groq-secret",
        )
        self.assertEqual(post.call_args.kwargs["json"]["max_completion_tokens"], 12000)
        self.assertIn('"body":', post.call_args.kwargs["json"]["messages"][1]["content"])
        self.assertEqual(
            slides[0]["body"],
            "A focused explanation makes the topic easier to follow. It connects the central idea to a practical situation for a general audience.",
        )
        self.assertNotIn("test-groq-secret", post.call_args.args[0])

    def test_providers_reject_token_truncated_responses(self):
        gemini_response = MagicMock()
        gemini_response.json.return_value = {
            "candidates": [{
                "finishReason": "MAX_TOKENS",
                "content": {"parts": [{"text": "{}"}]},
            }]
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "configured"}), patch(
            "app.requests.post", return_value=gemini_response
        ):
            gemini_slides, gemini_detail = slidecraft_app.gemini_generate(
                "Grid storage", 1, "General audience", "Presentation", "Professional"
            )

        groq_response = MagicMock()
        groq_response.json.return_value = {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": "{}"},
            }]
        }
        with patch.dict(os.environ, {"GROQ_API_KEY": "configured"}), patch(
            "app.requests.post", return_value=groq_response
        ):
            groq_slides, groq_detail = slidecraft_app.groq_generate(
                "Grid storage", 1, "General audience", "Presentation", "Professional"
            )

        self.assertIsNone(gemini_slides)
        self.assertIn("incomplete", gemini_detail)
        self.assertIsNone(groq_slides)
        self.assertIn("incomplete", groq_detail)

    def test_provider_http_errors_are_actionable_and_redact_keys(self):
        api_response = MagicMock()
        api_response.status_code = 429
        api_response.json.return_value = {
            "error": {"message": "Quota exceeded for test-gemini-secret."}
        }
        request_error = slidecraft_app.requests.HTTPError(
            "request failed",
            response=api_response,
        )
        response = MagicMock()
        response.raise_for_status.side_effect = request_error

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-gemini-secret"}), patch(
            "app.requests.post", return_value=response
        ):
            slides, detail = slidecraft_app.gemini_generate(
                "Grid storage", 1, "General audience", "Presentation", "Professional"
            )

        self.assertIsNone(slides)
        self.assertIn("rate limit or quota", detail)
        self.assertIn("[redacted]", detail)
        self.assertNotIn("test-gemini-secret", detail)

    def test_fallback_progress_reports_groq_activation_and_visuals(self):
        progress = []
        slides = slidecraft_app.offline_slides("Progress test", 4)

        def delayed_gemini(*_args):
            import time
            time.sleep(1.1)
            return None, "Gemini did not return."

        with patch.dict(os.environ, {
            "AI_FALLBACK_DELAY_SECONDS": "1",
            "GEMINI_API_KEY": "configured",
            "GROQ_API_KEY": "configured",
        }), patch.object(
            slidecraft_app, "gemini_generate", side_effect=delayed_gemini
        ), patch.object(
            slidecraft_app, "groq_generate", return_value=(slides, "Groq generated content.")
        ):
            result, provider, _detail = slidecraft_app.generate_slides_with_fallback(
                "Progress test",
                4,
                "General audience",
                "Presentation",
                "Professional",
                progress_callback=lambda stage, message: progress.append((stage, message)),
            )

        self.assertEqual(provider, "groq")
        self.assertEqual(len(result), 4)
        self.assertEqual([stage for stage, _message in progress], ["gemini", "groq", "visuals"])
        self.assertEqual(progress[1][1], "Still working on your presentation...")

    def test_missing_ai_keys_select_offline_without_waiting_or_provider_calls(self):
        progress = []
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "",
            "GROQ_API_KEY": "",
        }), patch.object(slidecraft_app, "gemini_generate") as gemini, patch.object(
            slidecraft_app, "groq_generate"
        ) as groq:
            slides, provider, detail = slidecraft_app.generate_slides_with_fallback(
                "Offline test",
                4,
                "General audience",
                "Presentation",
                "Professional",
                progress_callback=lambda stage, message: progress.append((stage, message)),
            )

        self.assertIsNone(slides)
        self.assertEqual(provider, "offline")
        self.assertEqual(detail, "No AI providers are configured.")
        self.assertEqual([stage for stage, _message in progress], ["offline"])
        self.assertEqual(progress[0][1], "Preparing your presentation.")
        gemini.assert_not_called()
        groq.assert_not_called()

    def test_generate_api_reports_offline_source_clearly(self):
        client = slidecraft_app.app.test_client()
        client.get("/create")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "",
            "GROQ_API_KEY": "",
        }), patch.object(
            slidecraft_app, "gemini_generate", return_value=(None, "Gemini API key is not configured.")
        ), patch.object(
            slidecraft_app, "enrich_slide_visuals", return_value="Unsplash is not configured."
        ):
            response = client.post(
                "/api/generate",
                headers={"X-CSRFToken": csrf},
                json={"topic": "Grid-scale energy storage", "count": 4},
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("source", payload)
        self.assertNotIn("source_label", payload)
        self.assertNotIn("source_detail", payload)
        self.assertEqual(payload["slide_count"], 4)

    def test_generation_status_requires_valid_id_and_reports_current_stage(self):
        generation_id = f"test_status_{uuid.uuid4().hex}"
        slidecraft_app.update_generation_status(
            generation_id, "groq", "Still working on your presentation..."
        )
        client = slidecraft_app.app.test_client()

        response = client.get(f"/api/generation-status/{generation_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"stage": "groq", "message": "Still working on your presentation..."},
        )
        self.assertEqual(
            client.get("/api/generation-status/invalid").status_code, 404
        )
        slidecraft_app.clear_generation_status(generation_id)

    def test_generate_to_edited_export_keeps_gemini_visuals(self):
        client = slidecraft_app.app.test_client()
        client.get("/create")
        with client.session_transaction() as session:
            csrf = session["csrf_token"]
        slides = [
            {
                "title": "Storage systems",
                "body": "Grid storage holds electricity when supply is high and releases it when demand rises.",
                "bullets": ["Store excess power for periods when demand is higher."],
                "notes": "Explain the purpose.",
            },
            {
                "title": "How storage works",
                "body": "Storage charges during surplus generation, retains energy, and discharges when the grid needs additional supply.",
                "bullets": ["Charge", "Hold energy", "Discharge"],
                "notes": "Describe the energy flow.",
                "visual": {"layout": "diagram", "description": "A three-stage energy flow."},
                "diagram": {"type": "flow", "items": ["Charge", "Store", "Discharge"]},
            },
            {
                "title": "Capacity comparison",
                "body": "Comparing reported capacity helps explain the scale of different storage technologies.",
                "bullets": ["Compare cited values."],
                "notes": "These values come from a source.",
                "visual": {"layout": "chart"},
                "chart": {
                    "type": "bar",
                    "title": "Capacity comparison",
                    "labels": ["Solar", "Wind"],
                    "series": [{"name": "Capacity", "values": [12, 18]}],
                    "units": "GW",
                    "source": "Verified energy report",
                },
            },
            {
                "title": "Next steps",
                "body": "Match storage choices to grid needs, operating conditions, and evidence from reliable sources.",
                "bullets": ["Choose a suitable system after evaluating the use case."],
                "notes": "Invite questions.",
            },
        ]
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "configured",
            "GROQ_API_KEY": "",
        }), patch.object(
            slidecraft_app, "gemini_generate", return_value=(slides, "Generated with Gemini.")
        ), patch.object(
            slidecraft_app, "enrich_slide_visuals", return_value="Photos are not configured."
        ):
            generated = client.post(
                "/api/generate",
                headers={"X-CSRFToken": csrf},
                json={"topic": "Grid-scale energy storage", "count": 4, "provider": "gemini"},
            )

        payload = generated.get_json()
        self.assertEqual(generated.status_code, 200)
        self.assertNotIn("source", payload)
        self.assertNotIn("source_label", payload)
        self.assertNotIn("source_detail", payload)
        generated_deck = Presentation(io.BytesIO(slidecraft_app.make_pptx(payload["slides"]).read()))
        cover_text = [shape.text for shape in generated_deck.slides[0].shapes if shape.has_text_frame]
        conclusion_text = [shape.text for shape in generated_deck.slides[-1].shapes if shape.has_text_frame]
        self.assertIn(payload["slides"][0]["body"], cover_text)
        self.assertIn(payload["slides"][-1]["body"], conclusion_text)
        self.assertEqual(payload["slides"][1]["diagram"]["items"], ["Charge", "Store", "Discharge"])
        self.assertEqual(payload["slides"][2]["chart"]["series"][0]["values"], [12, 18])

        edited_slides = payload["slides"]
        edited_slides[1]["title"] = "Edited energy flow"
        edited_slides[1]["body"] = "Updated explanation retained after editing."
        edited_slides[1]["notes"] = "Updated notes from the editor."
        exported = client.post(
            "/api/export",
            headers={"X-CSRFToken": csrf},
            json={"slides": edited_slides},
        )
        self.assertEqual(exported.status_code, 200)
        presentation = Presentation(io.BytesIO(exported.data))
        diagram_text = [shape.text for shape in presentation.slides[1].shapes if shape.has_text_frame]
        self.assertIn("Edited energy flow", diagram_text)
        self.assertIn("Updated explanation retained after editing.", diagram_text)
        for bullet in edited_slides[1]["bullets"]:
            self.assertIn(bullet, diagram_text)
        self.assertTrue(any(shape.has_chart for shape in presentation.slides[2].shapes))
        self.assertIn("Updated notes from the editor", presentation.slides[1].notes_slide.notes_text_frame.text)
        self.assertGreater(len(presentation.slides[1].shapes), 5)

    def test_generation_still_saves_full_slides_to_project_history(self):
        email = f"slide-history-{uuid.uuid4().hex}@example.invalid"
        db = slidecraft_app.db_connect()
        try:
            cursor = db.execute(
                "INSERT INTO users(name, email, password_hash) VALUES (?, ?, ?)",
                ("History Test", email, "unused"),
            )
            user_id = cursor.lastrowid
            db.commit()
        finally:
            db.close()
        try:
            client = slidecraft_app.app.test_client()
            client.get("/create")
            with client.session_transaction() as session:
                csrf = session["csrf_token"]
                session["user_id"] = user_id
            with patch.dict(os.environ, {
                "GEMINI_API_KEY": "",
                "GROQ_API_KEY": "",
            }), patch.object(
                slidecraft_app, "gemini_generate", return_value=(None, "Gemini API key is not configured.")
            ), patch.object(
                slidecraft_app, "enrich_slide_visuals", return_value="No photo provider configured."
            ):
                response = client.post(
                    "/api/generate",
                    headers={"X-CSRFToken": csrf},
                    json={"topic": "Grid-scale storage", "count": 4},
                )
            self.assertEqual(response.status_code, 200)
            db = slidecraft_app.db_connect()
            try:
                saved = db.execute("SELECT * FROM projects WHERE user_id = ?", (user_id,)).fetchone()
            finally:
                db.close()
            self.assertEqual(saved["slide_count"], 4)
            self.assertIn("visual", saved["slides_json"])
        finally:
            db = slidecraft_app.db_connect()
            try:
                db.execute("DELETE FROM projects WHERE user_id = ?", (user_id,))
                db.execute("DELETE FROM users WHERE id = ?", (user_id,))
                db.commit()
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
