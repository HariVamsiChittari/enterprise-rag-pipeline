"""Token-based page-aware chunking with configurable window and overlap."""

from __future__ import annotations

import re

import tiktoken

from ingestion.errors import TerminalDocumentError
from ingestion.models import Chunk, Page

DEFAULT_MAX_TOKENS = 800
DEFAULT_OVERLAP_TOKENS = 100
DEFAULT_TOKENIZER = "cl100k_base"
MAX_CHUNKS_PER_DOCUMENT = 2_000


def chunk_pages(
    pages: list[Page],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    tokenizer_name: str = DEFAULT_TOKENIZER,
) -> list[Chunk]:
    """Split extracted pages into token-bounded chunks with overlap."""
    encoding = tiktoken.get_encoding(tokenizer_name)
    chunks: list[Chunk] = []
    step = max_tokens - overlap_tokens

    for page in pages:
        for segment in _page_segments(page.text):
            tokens = encoding.encode(segment)
            for offset in range(0, len(tokens), step):
                token_slice = tokens[offset : offset + max_tokens]
                if not token_slice:
                    break
                text = encoding.decode(token_slice).strip()
                if text:
                    chunks.append(Chunk(ordinal=len(chunks), page_number=page.number, content=text))
                    if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
                        raise TerminalDocumentError("chunking_limit_exceeded")
                if offset + max_tokens >= len(tokens):
                    break

    if not chunks:
        raise TerminalDocumentError("chunking_no_output")
    return chunks


def token_count(text: str, tokenizer_name: str = DEFAULT_TOKENIZER) -> int:
    encoding = tiktoken.get_encoding(tokenizer_name)
    return len(encoding.encode(text))


def _page_segments(text: str) -> list[str]:
    """Split page text into segments by headings and paragraph breaks."""
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    segments: list[str] = []
    buffer: list[str] = []
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            if buffer:
                segments.append(" ".join(buffer).strip())
                buffer = []
            continue
        if line.startswith("#") and buffer:
            segments.append(" ".join(buffer).strip())
            buffer = [line]
            continue
        if re.match(r"^(?:[-*]|\d+\.)\s+", line) and buffer and buffer[-1].startswith("#"):
            segments.append(" ".join(buffer).strip())
            buffer = [line]
            continue
        buffer.append(line)
    if buffer:
        segments.append(" ".join(buffer).strip())
    return segments
