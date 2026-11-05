"""Direct Qdrant knowledge retrieval over the native REST API (no MCP)."""
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings


class RetrievalError(RuntimeError):
    pass


class RetrievalHTTPError(RetrievalError):
    pass


def _check_response(response: httpx.Response, service: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        hints = {
            400: "check the request model, vector dimensions, or named vector configuration",
            401: "check this service's API credential",
            403: "check this service's credential permissions",
            404: "check this service's endpoint, model, or collection name",
            429: "service rate limit or quota exceeded",
        }
        hint = hints.get(response.status_code, "check service availability and server logs")
        raise RetrievalHTTPError(f"{service} HTTP {response.status_code}: {hint}") from exc


async def _embed(client: httpx.AsyncClient, query: str) -> list[float]:
    base_url = (settings.embedding_base_url or settings.llm_base_url).rstrip("/")
    api_key = settings.embedding_api_key or settings.llm_api_key
    body = {"model": settings.embedding_model, "input": query}
    if settings.embedding_dimensions is not None:
        body["dimensions"] = settings.embedding_dimensions
    response = await client.post(
        f"{base_url}/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )
    _check_response(response, "Embedding API")
    vector = response.json()["data"][0]["embedding"]
    if settings.embedding_dimensions is not None and len(vector) != settings.embedding_dimensions:
        raise RetrievalError(
            f"Embedding dimension mismatch: expected {settings.embedding_dimensions}, received {len(vector)}. "
            "Check EMBEDDING_DIMENSIONS and embedding gateway support for the dimensions parameter."
        )
    return vector


async def retrieve_knowledge(query: str) -> list[dict[str, Any]]:
    if not settings.rag_enabled:
        return []
    async with httpx.AsyncClient(timeout=settings.rag_timeout_seconds) as client:
        headers = {"api-key": settings.qdrant_api_key} if settings.qdrant_api_key else {}
        collection = quote(settings.qdrant_collection, safe="")
        collection_response = await client.get(
            f"{settings.qdrant_url.rstrip('/')}/collections/{collection}",
            headers=headers,
        )
        _check_response(collection_response, "Qdrant collection")
        vectors = collection_response.json()["result"]["config"]["params"]["vectors"]
        vector_config = vectors.get(settings.qdrant_vector_name) if settings.qdrant_vector_name else vectors
        if not isinstance(vector_config, dict) or "size" not in vector_config:
            raise RetrievalError("QDRANT_VECTOR_NAME does not match the collection's vector configuration")
        if settings.embedding_dimensions is not None and vector_config["size"] != settings.embedding_dimensions:
            raise RetrievalError(
                f"Qdrant collection expects {vector_config['size']} dimensions, but EMBEDDING_DIMENSIONS is "
                f"{settings.embedding_dimensions}; set both to the collection's configured vector size."
            )
        vector = await _embed(client, query)
        if len(vector) != vector_config["size"]:
            raise RetrievalError(
                f"Embedding has {len(vector)} dimensions, but the Qdrant collection expects {vector_config['size']}."
            )
        body: dict[str, Any] = {"query": vector, "limit": settings.rag_top_k, "with_payload": True}
        if settings.qdrant_vector_name:
            body["using"] = settings.qdrant_vector_name
        response = await client.post(
            f"{settings.qdrant_url.rstrip('/')}/collections/{collection}/points/query",
            headers=headers, json=body,
        )
        _check_response(response, "Qdrant query")

    results = []
    for point in response.json().get("result", {}).get("points", [])[: settings.rag_top_k]:
        payload = point.get("payload") or {}
        results.append({
            **payload,
            "source": payload.get("source") or f"qdrant/{settings.qdrant_collection}/{point.get('id')}",
            "score": point.get("score"),
        })
    return results
