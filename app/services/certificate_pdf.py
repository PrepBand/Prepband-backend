"""On-demand certificate PDF (FR-CERT-01..06), rendered with ReportLab and
streamed straight to the student - nothing is stored.

Deliberately plain: no logos, seals, signatures or anything resembling an
official IELTS Test Report Form (FR-CERT-04). Every value comes from the
server's own records; nothing on it can come from the client (FR-CERT-07).
"""

import io
from datetime import datetime
from pathlib import Path

import reportlab
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from ..schemas.mocks import MockModuleResult

# FR-CERT-03, verbatim.
UNOFFICIAL_LABEL = "UNOFFICIAL IELTS-STYLE PRACTICE COMPLETION CERTIFICATE — NOT AN OFFICIAL IELTS RESULT"

DISCLAIMER = (
    "This is not an IELTS Test Report Form, not an IELTS result and not a qualification. It only records that the "
    "holder completed a practice mock test on Prepband. Prepband is an independent practice platform and is not "
    "affiliated with, endorsed by or connected to IELTS or its owners (the British Council, IDP: IELTS Australia "
    "and Cambridge University Press & Assessment). Listening and Reading bands are approximate estimates from a "
    "simple percentage conversion. Writing and Speaking were not assessed."
)

INK = HexColor("#1f2a44")
MUTED = HexColor("#5b6275")
RULE = HexColor("#c9bfa8")
PAPER = HexColor("#fbf8f1")
REVOKED_RED = HexColor("#b3261e")

_FONTS_REGISTERED = False


def _register_fonts() -> None:
    """Bitstream Vera ships with ReportLab and, unlike the built-in
    Helvetica, embeds as a real TrueType font, so accented names render."""

    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    font_dir = Path(reportlab.__file__).parent / "fonts"
    pdfmetrics.registerFont(TTFont("PB-Sans", str(font_dir / "Vera.ttf")))
    pdfmetrics.registerFont(TTFont("PB-Sans-Bold", str(font_dir / "VeraBd.ttf")))
    pdfmetrics.registerFont(TTFont("PB-Sans-Italic", str(font_dir / "VeraIt.ttf")))
    _FONTS_REGISTERED = True


def _fit_font_size(text: str, font: str, max_size: float, min_size: float, max_width: float) -> float:
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def _wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Greedy word wrap on ordinary spaces only, so non-breaking spaces
    (used in dates) really don't break."""

    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if current and stringWidth(candidate, font, size) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _format_date(value: datetime) -> str:
    # Non-breaking spaces keep the date on one line when text wraps.
    return f"{value.day} {value.strftime('%B')} {value.year}"


def module_outcome(result: MockModuleResult) -> tuple[str, str]:
    """(outcome, provenance) for one module row."""

    if result.status in ("not_started", "in_progress", "cancelled"):
        return "Not completed", "-"
    timed_out = " (time ran out)" if result.status == "expired" else ""
    if result.module in ("listening", "reading"):
        score = (
            f"{result.raw_score:g} / {result.max_score:g} correct"
            if result.raw_score is not None and result.max_score is not None
            else "No score"
        )
        band = f" · est. band {result.estimated_band:.1f}" if result.estimated_band is not None else ""
        return score + band + timed_out, "Marked against an answer key; band is approximate"
    if result.module == "writing":
        return (
            f"{result.tasks_answered or 0} of {result.tasks_total or 0} tasks submitted · {result.words_written or 0} words{timed_out}",
            "Not assessed",
        )
    return (
        f"{result.speaking_answers or 0} of {result.speaking_prompts_total or 0} prompts answered{timed_out}",
        "Not assessed",
    )


def render_certificate_pdf(
    *,
    code: str,
    holder_name: str,
    track: str,
    issued_at: datetime,
    completed_at: datetime | None,
    modules: list[MockModuleResult],
    overall_band: float | None,
    overall_band_note: str,
    verification_url: str | None,
    revoked_at: datetime | None,
) -> bytes:
    _register_fonts()
    buffer = io.BytesIO()
    width, height = landscape(A4)
    pdf = canvas.Canvas(buffer, pagesize=(width, height))
    pdf.setTitle(f"Prepband practice completion certificate {code}")
    pdf.setAuthor("Prepband")
    pdf.setSubject(UNOFFICIAL_LABEL)

    margin = 42
    content_width = width - 2 * margin

    # Background and frame.
    pdf.setFillColor(PAPER)
    pdf.rect(0, 0, width, height, stroke=0, fill=1)
    pdf.setStrokeColor(RULE)
    pdf.setLineWidth(1)
    pdf.rect(margin / 2, margin / 2, width - margin, height - margin, stroke=1, fill=0)

    # FR-CERT-03: the unofficial label is the first and most prominent thing.
    band_height = 46
    band_top = height - margin
    pdf.setFillColor(INK)
    pdf.rect(margin, band_top - band_height, content_width, band_height, stroke=0, fill=1)
    label_size = _fit_font_size(UNOFFICIAL_LABEL, "PB-Sans-Bold", 15, 9, content_width - 24)
    pdf.setFillColor(white)
    pdf.setFont("PB-Sans-Bold", label_size)
    pdf.drawCentredString(width / 2, band_top - band_height / 2 - label_size / 3, UNOFFICIAL_LABEL)

    y = band_top - band_height - 34
    pdf.setFillColor(MUTED)
    pdf.setFont("PB-Sans-Bold", 11)
    pdf.drawString(margin, y, "Prepband")
    pdf.setFont("PB-Sans", 10)
    pdf.drawRightString(width - margin, y, f"Certificate ID  {code}")

    y -= 34
    pdf.setFillColor(INK)
    pdf.setFont("PB-Sans-Bold", 22)
    pdf.drawCentredString(width / 2, y, "Practice completion certificate")

    y -= 26
    pdf.setFont("PB-Sans", 12)
    pdf.setFillColor(MUTED)
    pdf.drawCentredString(width / 2, y, "This confirms that")

    y -= 32
    name_size = _fit_font_size(holder_name, "PB-Sans-Bold", 26, 12, content_width - 40)
    pdf.setFillColor(INK)
    pdf.setFont("PB-Sans-Bold", name_size)
    pdf.drawCentredString(width / 2, y, holder_name)

    y -= 24
    finished = _format_date(completed_at or issued_at)
    statement = (
        f"completed a full four-module IELTS-style practice mock test ({track.title()} track) "
        f"on Prepband, finished on {finished}."
    )
    pdf.setFont("PB-Sans", 12)
    pdf.setFillColor(MUTED)
    for line in _wrap(statement, "PB-Sans", 12, content_width - 80):
        pdf.drawCentredString(width / 2, y, line)
        y -= 16

    # Module table.
    y -= 12
    col_module = margin + 20
    col_outcome = margin + 150
    col_source = margin + 470
    pdf.setStrokeColor(RULE)
    pdf.line(margin + 10, y + 6, width - margin - 10, y + 6)
    pdf.setFont("PB-Sans-Bold", 10)
    pdf.setFillColor(MUTED)
    pdf.drawString(col_module, y - 8, "Module")
    pdf.drawString(col_outcome, y - 8, "Outcome")
    pdf.drawString(col_source, y - 8, "How it was scored")
    y -= 14
    pdf.line(margin + 10, y - 2, width - margin - 10, y - 2)

    rows = [(r.module.title(), *module_outcome(r)) for r in modules]
    rows.append(("Overall band", "Not available" if overall_band is None else f"{overall_band:.1f} (unofficial estimate)", ""))
    for label, outcome, source in rows:
        y -= 18
        pdf.setFillColor(INK)
        pdf.setFont("PB-Sans-Bold" if label == "Overall band" else "PB-Sans", 10.5)
        pdf.drawString(col_module, y, label)
        pdf.setFont("PB-Sans", 10.5)
        pdf.drawString(col_outcome, y, outcome)
        pdf.setFillColor(MUTED)
        pdf.setFont("PB-Sans", 9.5)
        pdf.drawString(col_source, y, source)
    y -= 8
    pdf.setStrokeColor(RULE)
    pdf.line(margin + 10, y, width - margin - 10, y)

    y -= 14
    pdf.setFont("PB-Sans-Italic", 9)
    pdf.setFillColor(MUTED)
    for line in _wrap(overall_band_note, "PB-Sans-Italic", 9, content_width - 40):
        pdf.drawString(col_module, y, line)
        y -= 11

    # Footer: issue date, verification, disclaimer.
    qr_size = 74
    footer_text_width = content_width - (qr_size + 30 if verification_url else 0)
    footer_y = margin + 96
    pdf.setFillColor(INK)
    pdf.setFont("PB-Sans-Bold", 9.5)
    pdf.drawString(margin, footer_y, f"Issued {_format_date(issued_at)}  ·  Certificate ID {code}")
    pdf.setFont("PB-Sans", 9)
    pdf.setFillColor(MUTED)
    if verification_url:
        pdf.drawString(margin, footer_y - 13, f"Verify at {verification_url}")
        widget = QrCodeWidget(verification_url)
        x1, y1, x2, y2 = widget.getBounds()
        drawing = Drawing(qr_size, qr_size, transform=[qr_size / (x2 - x1), 0, 0, qr_size / (y2 - y1), 0, 0])
        drawing.add(widget)
        renderPDF.draw(drawing, pdf, width - margin - qr_size, margin + 40)
    elif revoked_at is not None:
        pdf.drawString(margin, footer_y - 13, "This certificate has been revoked and is no longer valid.")
    else:
        pdf.drawString(
            margin,
            footer_y - 13,
            "Verification is private. The holder can turn on public verification from their Prepband account.",
        )

    text_y = footer_y - 32
    pdf.setFont("PB-Sans", 8)
    for line in _wrap(DISCLAIMER, "PB-Sans", 8, footer_text_width):
        pdf.drawString(margin, text_y, line)
        text_y -= 10

    if revoked_at is not None:
        pdf.saveState()
        pdf.setFillColor(Color(REVOKED_RED.red, REVOKED_RED.green, REVOKED_RED.blue, alpha=0.18))
        pdf.translate(width / 2, height / 2)
        pdf.rotate(24)
        pdf.setFont("PB-Sans-Bold", 110)
        pdf.drawCentredString(0, -30, "REVOKED")
        pdf.restoreState()
        pdf.setFillColor(REVOKED_RED)
        pdf.setFont("PB-Sans-Bold", 10)
        pdf.drawRightString(width - margin, band_top - band_height - 50, f"Revoked on {_format_date(revoked_at)}")

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
