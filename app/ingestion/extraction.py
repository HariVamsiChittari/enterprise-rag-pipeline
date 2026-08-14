"""Document extraction — converts raw file bytes into structured pages.

Currently supports PDF via Azure Document Intelligence (prebuilt-layout → Markdown).
When new file types are needed, add a new extract function and dispatch by MIME type.
"""

from __future__ import annotations

import logging
from typing import Any

from azure.ai.documentintelligence.models import AnalyzeDocumentRequest, DocumentContentFormat

from ingestion.errors import TerminalDocumentError
from ingestion.models import Page

logger = logging.getLogger(__name__)

MIN_TEXT_CHARACTERS = 50


def extract_pdf(client: Any, content: bytes, max_pdf_pages: int = 500) -> list[Page]:
    """Extract pages from a PDF using Document Intelligence prebuilt-layout model."""
    try:
        poller = client.begin_analyze_document(
            model_id="prebuilt-layout",
            body=AnalyzeDocumentRequest(bytes_source=content),
            output_content_format=DocumentContentFormat.MARKDOWN,
        )
        result = poller.result()
    except Exception as error:
        status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
        if status == 429:
            raise TimeoutError("document_intelligence_throttled") from error
        if isinstance(status, int) and 500 <= status < 600:
            raise TimeoutError("document_intelligence_transient") from error
        if isinstance(status, int) and 400 <= status < 500:
            raise TerminalDocumentError("document_intelligence_rejected") from error
        raise TimeoutError("document_intelligence_transient") from error

    service_pages = list(result.pages or [])
    if not service_pages:
        raise TerminalDocumentError("extraction_no_pages")
    if len(service_pages) > max_pdf_pages:
        raise TerminalDocumentError("extraction_page_limit_exceeded")

    result_content = result.content or ""
    pages: list[Page] = []
    for index, service_page in enumerate(service_pages):
        page_text = "\n".join(
            result_content[span.offset : span.offset + span.length]
            for span in (service_page.spans or [])
        ).strip()
        pages.append(Page(number=index + 1, text=page_text))

    total_chars = sum(len(page.text) for page in pages)
    if total_chars < MIN_TEXT_CHARACTERS:
        raise TerminalDocumentError("extraction_insufficient_text")

    logger.info("Extracted %d pages (%d chars) from PDF", len(pages), total_chars)
    return pages
