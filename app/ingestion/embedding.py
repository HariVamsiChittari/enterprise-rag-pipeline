"""OpenAI embedding generation for document chunks."""

from __future__ import annotations

import logging
from typing import Any

from ingestion.errors import TerminalDocumentError

logger = logging.getLogger(__name__)

EMBEDDING_BATCH_SIZE = 100
EMBEDDING_DIMENSIONS = 3_072
EMBEDDING_MODEL = "text-embedding-3-large"


def embed_texts(
    client: Any,
    texts: list[str],
    deployment: str = EMBEDDING_MODEL,
) -> list[tuple[float, ...]]:
    """Generate embeddings for a list of texts in batches."""
    embeddings: list[tuple[float, ...]] = []
    for offset in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[offset : offset + EMBEDDING_BATCH_SIZE]
        try:
            response = client.embeddings.create(
                model=deployment,
                input=batch,
                dimensions=EMBEDDING_DIMENSIONS,
                encoding_format="float",
            )
        except Exception as error:
            # Classify OpenAI errors: 429/timeout → retryable, others → terminal
            error_type = type(error).__name__
            if "RateLimit" in error_type or "Timeout" in error_type or "APITimeout" in error_type:
                raise TimeoutError(f"openai_throttled:{error_type}") from error
            if "APIConnectionError" in error_type or "InternalServerError" in error_type:
                raise TimeoutError(f"openai_transient:{error_type}") from error
            raise TerminalDocumentError(f"openai_failed:{error_type}") from error
        ordered = sorted(response.data, key=lambda item: item.index)
        if len(ordered) != len(batch):
            raise TerminalDocumentError("embedding_count_mismatch")
        for item in ordered:
            vector = tuple(item.embedding)
            if len(vector) != EMBEDDING_DIMENSIONS:
                raise TerminalDocumentError("embedding_dimension_mismatch")
            embeddings.append(vector)

    logger.info("Generated %d embeddings (%d dimensions each)", len(embeddings), EMBEDDING_DIMENSIONS)
    return embeddings
