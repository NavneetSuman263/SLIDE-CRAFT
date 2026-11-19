import io
import os

from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader


PRESETS = {
    "presentation",
    "print",
    "a4_landscape",
    "a4_portrait",
    "letter",
    "compressed",
    "speaker_notes",
    "handout_2",
    "handout_4",
    "handout_6",
}

NAVY = colors.HexColor("#10234b")
BLUE = colors.HexColor("#3268f5")
CORAL = colors.HexColor("#ef806a")
INK = colors.HexColor("#243451")
MUTED = colors.HexColor("#65738c")
PAPER = colors.HexColor("#f7f8fc")
WHITE = colors.white


def _register_fonts():
    candidates = [
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    ]
    for regular, bold in candidates:
        if os.path.isfile(regular) and os.path.isfile(bold):
            pdfmetrics.registerFont(TTFont("SlideCraftSans", regular))
            pdfmetrics.registerFont(TTFont("SlideCraftSans-Bold", bold))
            return "SlideCraftSans", "SlideCraftSans-Bold", True
    return "Helvetica", "Helvetica-Bold", False


FONT, FONT_BOLD, UNICODE_FONTS = _register_fonts()


def _text(value):
    cleaned = str(value or "").replace("\r", " ").replace("\n", " ")
    if not UNICODE_FONTS:
        return cleaned.encode("cp1252", "replace").decode("cp1252")
    return cleaned


def _wrap(value, font, size, max_width):
    words = _text(value).split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and pdfmetrics.stringWidth(candidate, font, size) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _draw_wrapped(c, value, x, y, max_width, font, size, color, leading=None, max_lines=None):
    leading = leading or size * 1.35
    c.setFont(font, size)
    c.setFillColor(color)
    lines = _wrap(value, font, size, max_width)
    if max_lines:
        lines = lines[:max_lines]
    for line in lines:
        c.drawString(x, y, line)
        y -= leading
    return y


def _rounded_card(c, x, y, width, height, fill=WHITE, stroke=colors.HexColor("#dce3ef")):
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.roundRect(x, y, width, height, min(12, height / 8), fill=1, stroke=1)


def _draw_chart(c, chart, x, y, width, height, compact=False):
    if not chart:
        return
    title_size = max(6, min(15, height * 0.075))
    _draw_wrapped(c, chart["title"], x, y + height - title_size, width, FONT_BOLD, title_size, NAVY, max_lines=1)
    labels = chart["labels"]
    series = chart["series"]
    plot_y = y + height * 0.2
    plot_h = height * 0.55
    plot_x = x + width * 0.12
    plot_w = width * 0.82
    values = [value for entry in series for value in entry["values"]]
    maximum = max(max(values), 1)
    palette = [BLUE, CORAL, colors.HexColor("#2ea47e"), colors.HexColor("#8e63e0")]

    if chart["type"] in {"pie", "doughnut"}:
        values = series[0]["values"]
        total = sum(values) or 1
        diameter = min(plot_h * 1.7, width * 0.4)
        center_x = x + width * 0.33
        center_y = y + height * 0.48
        start = 90
        for index, value in enumerate(values):
            extent = -360 * value / total
            c.setFillColor(palette[index % len(palette)])
            c.wedge(center_x - diameter / 2, center_y - diameter / 2, center_x + diameter / 2, center_y + diameter / 2, start, extent, fill=1, stroke=0)
            start += extent
        if chart["type"] == "doughnut":
            c.setFillColor(WHITE)
            c.circle(center_x, center_y, diameter * 0.26, fill=1, stroke=0)
        legend_x = x + width * 0.59
        legend_y = center_y + min(32, height * 0.16)
        for index, label in enumerate(labels):
            c.setFillColor(palette[index % len(palette)])
            c.circle(legend_x, legend_y + 3, 3, fill=1, stroke=0)
            _draw_wrapped(c, f"{label} · {values[index]:g}", legend_x + 8, legend_y, width * 0.36, FONT, max(6, title_size * 0.64), INK, max_lines=1)
            legend_y -= max(12, height * 0.11)
    elif chart["type"] == "line":
        c.setStrokeColor(colors.HexColor("#cfd8e8"))
        c.line(plot_x, plot_y, plot_x + plot_w, plot_y)
        for series_index, entry in enumerate(series):
            c.setStrokeColor(palette[series_index % len(palette)])
            c.setLineWidth(2 if not compact else 1)
            points = []
            for index, value in enumerate(entry["values"]):
                px = plot_x + (plot_w * index / max(1, len(labels) - 1))
                py = plot_y + (plot_h * value / maximum)
                points.append((px, py))
            for first, second in zip(points, points[1:]):
                c.line(first[0], first[1], second[0], second[1])
            for px, py in points:
                c.circle(px, py, max(1.5, title_size * 0.22), fill=1, stroke=0)
    else:
        group_w = plot_w / max(1, len(labels))
        bar_w = group_w * 0.72 / max(1, len(series))
        for label_index, label in enumerate(labels):
            for series_index, entry in enumerate(series):
                value = entry["values"][label_index]
                bar_h = plot_h * value / maximum
                bx = plot_x + label_index * group_w + group_w * 0.12 + series_index * bar_w
                c.setFillColor(palette[series_index % len(palette)])
                c.roundRect(bx, plot_y, max(2, bar_w - 2), max(1, bar_h), 2, fill=1, stroke=0)
            if not compact:
                _draw_wrapped(c, label, plot_x + label_index * group_w, plot_y - 10, group_w, FONT, 6, MUTED, max_lines=1)
    if chart["type"] not in {"pie", "doughnut"} and not compact:
        legend_y = y + height * 0.065
        legend_x = x
        legend_size = max(6, title_size * 0.56)
        for index, entry in enumerate(series):
            entry_name = _text(entry["name"])[:36]
            entry_width = pdfmetrics.stringWidth(entry_name, FONT, legend_size) + 22
            if legend_x + entry_width > x + width:
                legend_x = x
                legend_y += legend_size + 5
            c.setFillColor(palette[index % len(palette)])
            c.circle(legend_x + 3, legend_y + 3, 3, fill=1, stroke=0)
            legend_x += 10
            c.setFont(FONT, legend_size)
            c.setFillColor(MUTED)
            c.drawString(legend_x, legend_y, entry_name)
            legend_x += entry_width - 10
    if not compact:
        c.setFillColor(MUTED)
        c.setFont(FONT, max(6, title_size * 0.55))
        labels = []
        if chart.get("units"):
            labels.append(chart["units"])
        if chart.get("illustrative"):
            labels.append("Illustrative data")
        elif chart.get("source"):
            labels.append(f"Source: {chart['source']}")
        c.drawString(x, y + 1, _text(" · ".join(labels))[:180])


def _draw_diagram(c, diagram, x, y, width, height, compact=False):
    if not diagram:
        return
    items = diagram["items"]
    gap = max(4, width * 0.025)
    item_w = (width - gap * (len(items) - 1)) / len(items)
    box_h = min(height * 0.48, 65 if not compact else height * 0.68)
    box_y = y + (height - box_h) / 2
    for index, item in enumerate(items):
        bx = x + index * (item_w + gap)
        _rounded_card(c, bx, box_y, item_w, box_h, fill=colors.HexColor("#f3f6ff"), stroke=colors.HexColor("#d4def2"))
        _draw_wrapped(c, item, bx + 5, box_y + box_h / 2 + 4, item_w - 10, FONT_BOLD, max(6, min(10, item_w * 0.1)), INK, leading=9, max_lines=3)
        if index < len(items) - 1:
            c.setFillColor(BLUE)
            c.setFont(FONT_BOLD, max(8, box_h * 0.27))
            c.drawCentredString(bx + item_w + gap / 2, box_y + box_h / 2 - 3, "›")
    if diagram.get("caption") and not compact:
        _draw_wrapped(c, diagram["caption"], x, y + 3, width, FONT, 8, MUTED, max_lines=1)


def _draw_image(c, path, x, y, width, height, compressed):
    if not path or not path.is_file():
        return False
    try:
        if compressed:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((1800, 1200), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=62, optimize=True)
                buffer.seek(0)
                source_image = ImageReader(buffer)
                c.drawImage(source_image, x, y, width, height, preserveAspectRatio=True, anchor="c", mask="auto")
        else:
            c.drawImage(ImageReader(str(path)), x, y, width, height, preserveAspectRatio=True, anchor="c", mask="auto")
        return True
    except (OSError, ValueError):
        return False


def _draw_slide(c, slide, x, y, width, height, image_path_getter, compressed=False, compact=False):
    c.saveState()
    c.setFillColor(PAPER)
    c.rect(x, y, width, height, fill=1, stroke=0)
    pad = max(12, min(width, height) * (0.075 if not compact else 0.045))
    title = _text(slide["title"])
    visual = slide["visual"]
    chart = slide.get("chart")
    diagram = slide.get("diagram")
    bullets = slide.get("bullets", [])
    body = str(slide.get("body") or "").strip()
    has_photo = bool(visual.get("image_asset") and image_path_getter(visual["image_asset"]))

    if compact:
        title_size = max(6, min(12, height * 0.12))
        _draw_wrapped(c, title, x + pad, y + height - pad - title_size, width - 2 * pad, FONT_BOLD, title_size, NAVY, max_lines=2)
        content_y = y + height * 0.45
        if body:
            body_size = max(5.5, height * 0.05)
            content_y = _draw_wrapped(
                c, body, x + pad, content_y, width - 2 * pad,
                FONT, body_size, MUTED, leading=max(7, body_size * 1.2), max_lines=2,
            ) - 5
        if chart:
            _draw_chart(c, chart, x + pad, y + height * 0.05, width - 2 * pad, height * 0.4, compact=True)
        elif diagram:
            _draw_diagram(c, diagram, x + pad, y + height * 0.08, width - 2 * pad, height * 0.38, compact=True)
        else:
            for bullet in bullets[:2]:
                content_y = _draw_wrapped(c, f"• {bullet}", x + pad, content_y, width - 2 * pad, FONT, max(5.5, height * 0.055), MUTED, leading=max(7, height * 0.07), max_lines=2)
        if has_photo:
            photo_h = height * 0.24
            _draw_image(c, image_path_getter(visual["image_asset"]), x + width - pad - width * 0.29, y + pad, width * 0.29, photo_h, compressed)
        c.setStrokeColor(colors.HexColor("#dce3ef"))
        c.rect(x, y, width, height, fill=0, stroke=1)
        c.restoreState()
        return

    photo_w = width * 0.43
    gap = width * 0.055
    left_x = x + pad
    text_w = width - 2 * pad
    body_y = y + height - pad - max(36, height * 0.14)
    title_size = min(32, max(16, height * 0.067))
    if has_photo:
        photo_x = x + width - pad - photo_w
        photo_y = y + pad + height * 0.08
        photo_h = height - 2 * pad - height * 0.08
        if visual.get("layout") == "full_image" and x == 0:
            _draw_image(c, image_path_getter(visual["image_asset"]), x + width * 0.46, y, width * 0.54, height, compressed)
        else:
            _draw_image(c, image_path_getter(visual["image_asset"]), photo_x, photo_y, photo_w, photo_h, compressed)
        text_w = width - photo_w - 3 * pad
    title_y = y + height - pad - title_size
    title_lines = _wrap(title, FONT_BOLD, title_size, text_w)[:3]
    title_leading = title_size * 1.12
    _draw_wrapped(c, title, left_x, title_y, text_w, FONT_BOLD, title_size, NAVY, leading=title_leading, max_lines=3)
    c.setFillColor(BLUE)
    rule_y = title_y - max(1, len(title_lines) - 1) * title_leading - title_size * 0.55
    c.roundRect(left_x, rule_y, min(width * 0.12, 72), 4, 2, fill=1, stroke=0)

    content_top = min(body_y, rule_y - 15)
    if body:
        body_size = max(9, min(12, height * 0.024))
        body_bottom = _draw_wrapped(
            c, body, left_x, rule_y - 16, text_w, FONT, body_size, INK,
            leading=body_size * 1.25, max_lines=3,
        )
        content_top = min(content_top, body_bottom - 10)
    if chart:
        chart_y = y + pad + height * 0.04
        chart_h = max(100, height * 0.48)
        _draw_chart(c, chart, left_x, chart_y, text_w, chart_h)
        content_top = chart_y - 12
    elif diagram:
        diagram_y = y + pad + height * 0.04
        diagram_h = max(75, height * 0.34)
        _draw_diagram(c, diagram, left_x, diagram_y, text_w, diagram_h)
        content_top = diagram_y - 6

    bullet_size = max(10, min(15, height * 0.031))
    cursor_y = content_top
    for index, bullet in enumerate(bullets[:5]):
        c.setFillColor(CORAL if index == 1 else BLUE)
        c.circle(left_x + 3, cursor_y - 4, 3, fill=1, stroke=0)
        cursor_y = _draw_wrapped(c, bullet, left_x + 15, cursor_y, text_w - 15, FONT, bullet_size, MUTED, leading=bullet_size * 1.4, max_lines=3) - 8
    credit = visual.get("image_credit")
    if has_photo and credit:
        credit_x = x + width - pad - photo_w
        credit_y = y + pad * 0.48
        c.setFont(FONT, max(6, bullet_size * 0.58))
        c.setFillColor(MUTED)
        label = f"Photo by {_text(credit)} · Unsplash"
        c.drawString(credit_x, credit_y, label[:110])
        name_width = pdfmetrics.stringWidth(f"Photo by {_text(credit)}", FONT, max(6, bullet_size * 0.58))
        photographer_url = visual.get("photographer_url") or visual.get("image_source")
        if photographer_url:
            c.linkURL(photographer_url, (credit_x, credit_y - 2, credit_x + name_width, credit_y + 10), relative=0, thickness=0)
        unsplash_url = visual.get("unsplash_url")
        if unsplash_url:
            unsplash_x = credit_x + name_width + pdfmetrics.stringWidth(" · ", FONT, max(6, bullet_size * 0.58))
            c.linkURL(unsplash_url, (unsplash_x, credit_y - 2, credit_x + photo_w, credit_y + 10), relative=0, thickness=0)
    c.setStrokeColor(colors.HexColor("#dce3ef"))
    c.line(x + pad, y + pad * 0.55, x + width - pad, y + pad * 0.55)
    c.setFillColor(MUTED)
    c.setFont(FONT, max(7, bullet_size * 0.62))
    c.drawString(x + pad, y + pad * 0.23, f"SLIDE {slide.get('_index', 1):02d}")
    c.restoreState()


def _draw_notes_page(c, slide, index, width, height):
    c.setFillColor(WHITE)
    c.rect(0, 0, width, height, fill=1, stroke=0)
    margin = 52
    c.setFillColor(BLUE)
    c.setFont(FONT_BOLD, 9)
    c.drawString(margin, height - 48, f"SPEAKER NOTES · SLIDE {index + 1}")
    y = _draw_wrapped(c, slide["title"], margin, height - 88, width - 2 * margin, FONT_BOLD, 25, NAVY, leading=31)
    y -= 18
    notes = slide.get("notes") or "No speaker notes were added for this slide."
    for paragraph in str(notes).splitlines():
        y = _draw_wrapped(c, paragraph, margin, y, width - 2 * margin, FONT, 12, INK, leading=18) - 8


def _page_size(preset):
    if preset == "a4_landscape":
        return landscape(A4)
    if preset == "a4_portrait":
        return A4
    if preset == "letter":
        return landscape(LETTER)
    return (960, 540)


def generate_pdf(slides, preset, image_path_getter):
    if preset not in PRESETS:
        raise ValueError("Choose a supported PDF layout.")
    if not slides:
        raise ValueError("Add at least one slide before exporting.")

    for index, slide in enumerate(slides, start=1):
        slide["_index"] = index

    compressed = preset == "compressed"
    width, height = _page_size(preset)
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(width, height), pageCompression=1)
    pdf.setTitle("SlideCraft Presentation")
    pdf.setAuthor("SlideCraft Studio")

    if preset.startswith("handout_"):
        per_page = int(preset.rsplit("_", 1)[1])
        columns = 1 if per_page == 2 else 2
        rows = per_page // columns
        margin = 30
        gap = 16
        cell_w = (width - 2 * margin - (columns - 1) * gap) / columns
        cell_h = (height - 2 * margin - (rows - 1) * gap) / rows
        for start in range(0, len(slides), per_page):
            for offset, slide in enumerate(slides[start:start + per_page]):
                row, col = divmod(offset, columns)
                cell_x = margin + col * (cell_w + gap)
                cell_y = height - margin - (row + 1) * cell_h - row * gap
                _draw_slide(pdf, slide, cell_x, cell_y, cell_w, cell_h, image_path_getter, compressed=True, compact=True)
            pdf.showPage()
    elif preset == "speaker_notes":
        for index, slide in enumerate(slides):
            _draw_slide(pdf, slide, 0, 0, width, height, image_path_getter)
            pdf.showPage()
            _draw_notes_page(pdf, slide, index, width, height)
            pdf.showPage()
    else:
        for slide in slides:
            _draw_slide(pdf, slide, 0, 0, width, height, image_path_getter, compressed=compressed)
            pdf.showPage()

    pdf.save()
    output.seek(0)
    if output.read(5) != b"%PDF-":
        raise ValueError("PDF generation did not produce a valid document.")
    output.seek(0)
    return output
