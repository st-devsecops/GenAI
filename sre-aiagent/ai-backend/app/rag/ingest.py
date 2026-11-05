"""Ingest a UTF-8 runbook from stdin using the retrieval embedding settings."""

import argparse
import asyncio
import sys
from uuid import NAMESPACE_URL, uuid5
from urllib.parse import quote

import httpx

from app.config import settings
from app.rag.retriever import RetrievalError, _check_response, _embed


async def ingest_document(text: str, source: str) -> int:
    if not text.strip() or not source.strip():
        raise RetrievalError("Nonempty document text and source are required for ingestion")
    if len(text) > 1_000_000:
        raise RetrievalError("Split documents larger than 1000000 characters before ingestion")
    if not settings.embedding_model or settings.embedding_dimensions is None:
        raise RetrievalError("Set EMBEDDING_MODEL and EMBEDDING_DIMENSIONS before ingestion")

    collection = quote(settings.qdrant_collection, safe="")
    url = f"{settings.qdrant_url.rstrip('/')}/collections/{collection}"
    headers = {"api-key": settings.qdrant_api_key} if settings.qdrant_api_key else {}
    async with httpx.AsyncClient(timeout=settings.rag_timeout_seconds) as client:
        response = await client.get(url, headers=headers)
        _check_response(response, "Qdrant collection")
        vectors = response.json()["result"]["config"]["params"]["vectors"]
        vector_config = vectors.get(settings.qdrant_vector_name) if settings.qdrant_vector_name else vectors
        if not isinstance(vector_config, dict) or "size" not in vector_config:
            raise RetrievalError("QDRANT_VECTOR_NAME does not match the collection's vector configuration")
        if vector_config["size"] != settings.embedding_dimensions:
            raise RetrievalError(
                f"Collection vector size is {vector_config['size']}; EMBEDDING_DIMENSIONS is "
                f"{settings.embedding_dimensions}. Use matching settings; the collection was not changed."
            )

        points = []
        for chunk_index, offset in enumerate(range(0, len(text), 2700)):
            chunk = text[offset:offset + 3000]
            if not chunk.strip():
                continue
            vector = await _embed(client, chunk)
            points.append({
                "id": str(uuid5(NAMESPACE_URL, f"{source}#chunk={chunk_index}")),
                "vector": {settings.qdrant_vector_name: vector} if settings.qdrant_vector_name else vector,
                "payload": {
                    "text": chunk, "source": source, "chunk_index": chunk_index,
                    "embedding_model": settings.embedding_model,
                    "embedding_dimensions": settings.embedding_dimensions,
                },
            })

        response = await client.put(
            f"{url}/points", params={"wait": "true"}, headers=headers, json={"points": points},
        )
        _check_response(response, "Qdrant ingestion")
    return len(points)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Stable runbook filename or source identifier")
    arguments = parser.parse_args()
    try:
        count = asyncio.run(ingest_document(sys.stdin.read(1_000_001), arguments.source))
    except RetrievalError as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Ingestion failed ({type(exc).__name__}); no raw response logged", file=sys.stderr)
        return 1
    print(f"Ingested {count} chunks with wait=true; no collection was recreated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())