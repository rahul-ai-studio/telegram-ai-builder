"""Image and PDF analysis using Claude's vision capabilities."""

from __future__ import annotations

import base64
import logging
import os

import anthropic

from router import MODEL_SONNET

logger = logging.getLogger(__name__)


def _get_client() -> anthropic.Anthropic:
    """Get an Anthropic client instance."""
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ──────────────────────────────────────────────
# Image analysis
# ──────────────────────────────────────────────

def analyze_image(image_bytes: bytes, mime_type: str, question: str | None = None) -> str:
    """Analyze an image using Claude's vision capability.

    Args:
        image_bytes: Raw image bytes
        mime_type: MIME type (e.g. image/jpeg, image/png)
        question: Optional specific question about the image

    Returns:
        Claude's analysis of the image
    """
    try:
        client = _get_client()

        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        user_text = question or (
            "Describe this image in detail. Extract any visible text. "
            "Explain anything important or noteworthy you see."
        )

        message = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=1500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )

        return message.content[0].text

    except Exception as e:
        logger.error(f"Image analysis failed: {e}")
        return f"Sorry, image analysis failed: {e}"


# ──────────────────────────────────────────────
# PDF analysis
# ──────────────────────────────────────────────

# Thresholds
_TEXT_MIN_CHARS_PER_PAGE = 100   # below this → likely scanned, use vision
_TEXT_MAX_CHARS         = 80_000 # cap text sent to Claude
_VISION_MAX_PAGES       = 6      # max pages to render when using vision
_VISION_ZOOM            = 1.0    # 1.0× keeps images small (was 1.5×)
_VISION_JPEG_QUALITY    = 75     # JPEG quality for rendered pages


def analyze_pdf(pdf_bytes: bytes, question: str | None = None) -> str:
    """Analyze a PDF using the best available method.

    Strategy:
      1. Try text extraction (fast, handles any size PDF).
      2. If text is too sparse (scanned PDF), fall back to vision:
         render up to 6 pages as JPEG and send to Claude.

    Args:
        pdf_bytes: Raw PDF file bytes
        question: Optional specific question about the PDF

    Returns:
        Claude's summary / answer
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return (
            "PDF analysis requires PyMuPDF. Please install with:\n"
            "pip install pymupdf"
        )

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        total_pages = len(doc)

        # ── Step 1: try text extraction ──────────────────────────────
        all_text = []
        for page in doc:
            all_text.append(page.get_text("text"))

        full_text  = "\n\n".join(all_text).strip()
        avg_chars  = len(full_text) / max(total_pages, 1)

        if avg_chars >= _TEXT_MIN_CHARS_PER_PAGE and len(full_text) >= 200:
            doc.close()
            return _summarise_text(full_text, total_pages, question)

        # ── Step 2: scanned PDF → vision fallback ────────────────────
        logger.info(
            f"PDF text sparse ({avg_chars:.0f} chars/page avg) — using vision mode"
        )

        pages_to_render = min(total_pages, _VISION_MAX_PAGES)
        image_contents  = []
        matrix          = fitz.Matrix(_VISION_ZOOM, _VISION_ZOOM)

        for page_num in range(pages_to_render):
            page = doc[page_num]
            pix  = page.get_pixmap(matrix=matrix)

            # Save as JPEG (much smaller than PNG)
            img_bytes = pix.tobytes("jpeg", jpg_quality=_VISION_JPEG_QUALITY)
            img_b64   = base64.standard_b64encode(img_bytes).decode("utf-8")

            image_contents.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": "image/jpeg",
                    "data":       img_b64,
                },
            })

        doc.close()

        skipped = total_pages - pages_to_render
        note    = f" (showing first {pages_to_render} of {total_pages})" if skipped else ""

        user_text = (
            f"This is a scanned PDF with {total_pages} pages{note}. "
            + (question or (
                "Please provide a comprehensive summary. "
                "Extract key points, important data, and actionable information."
            ))
        )
        image_contents.append({"type": "text", "text": user_text})

        return _call_vision(image_contents)

    except Exception as e:
        logger.error(f"PDF analysis failed: {e}")
        return f"Sorry, PDF analysis failed: {e}"


# ── helpers ──────────────────────────────────────────────────────────────────

def _summarise_text(full_text: str, total_pages: int, question: str | None) -> str:
    """Summarise extracted text using Claude."""
    client = _get_client()

    # Trim to avoid huge context bills on very long PDFs
    trimmed = full_text[:_TEXT_MAX_CHARS]
    truncated_note = (
        f"\n\n[Note: text truncated to {_TEXT_MAX_CHARS:,} chars; "
        f"full PDF has {total_pages} pages]"
        if len(full_text) > _TEXT_MAX_CHARS else ""
    )

    prompt = (
        f"This is extracted text from a {total_pages}-page PDF.\n\n"
        f"{trimmed}{truncated_note}\n\n"
        + (question or (
            "Please provide a comprehensive summary. "
            "Extract key points, important data, and actionable information."
        ))
    )

    message = client.messages.create(
        model=MODEL_SONNET,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def _call_vision(image_contents: list) -> str:
    """Send image content blocks to Claude vision."""
    client = _get_client()
    message = client.messages.create(
        model=MODEL_SONNET,
        max_tokens=3000,
        messages=[{"role": "user", "content": image_contents}],
    )
    return message.content[0].text
