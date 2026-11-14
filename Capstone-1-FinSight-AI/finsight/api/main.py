"""FinSight AI REST API (FastAPI).

Run locally:  uvicorn finsight.api.main:app --reload
Docs:         http://localhost:8000/docs
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from finsight import __version__
from finsight.analytics.entities import entity_backend, extract_entities
from finsight.analytics.sentiment import analyze_sentiment
from finsight.config import get_settings
from finsight.ingestion.loaders import SUPPORTED_EXTENSIONS
from finsight.llm.providers import LLMError
from finsight.observability import metrics as M
from finsight.rag.engine import FinSightEngine, GuardrailViolation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
log = logging.getLogger("finsight.api")

_engine: FinSightEngine | None = None


def get_engine() -> FinSightEngine:
    global _engine
    if _engine is None:
        _engine = FinSightEngine()
    return _engine


def set_engine(engine: FinSightEngine | None) -> None:
    """Used by tests to inject an engine with fake models."""
    global _engine
    _engine = engine


# Background auto-ingest state, reported by /ready.
_ingest = {"running": False, "ingested": 0, "error": None}


def _auto_ingest(engine: FinSightEngine, directory: Path) -> None:
    _ingest.update(running=True, error=None)
    try:
        done = engine.ingest_directory(directory, skip_existing=True)
        _ingest["ingested"] = len(done)
        log.info("Auto-ingest finished: %d new documents from %s", len(done), directory)
    except Exception as exc:
        _ingest["error"] = str(exc)
        log.error("Auto-ingest failed: %s", exc)
    finally:
        _ingest["running"] = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        engine = get_engine()
        log.info("FinSight engine ready")
        auto_dir = engine.s.auto_ingest_dir
        if auto_dir and Path(auto_dir).is_dir():
            # Runs in a background thread so /health answers immediately (a blocking ingest gets the pod killed
            # by the liveness probe). Inside the API process because embedded Chroma must have a single writer.
            # skip_existing makes it resumable after a restart.
            _ingest["running"] = True
            threading.Thread(target=_auto_ingest, args=(engine, Path(auto_dir)), daemon=True, name="auto-ingest").start()
    except Exception as exc:  # keep the API up so /health and /ready can report the problem
        log.error("Engine init failed: %s", exc)
    yield


app = FastAPI(
    title="FinSight AI",
    version=__version__,
    description="Compliance-aware financial research copilot - hybrid RAG with citations, guardrails, "
    "semantic caching and model routing. 100% free/open-source stack.",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(NotImplementedError)
async def not_implemented(request: Request, exc: NotImplementedError):
    """Unfinished TODOs raise NotImplementedError - tell the learner which one instead of a bare 500."""
    return JSONResponse(
        status_code=501,
        content={"error": "not_implemented", "todo": str(exc), "hint": "Implement this TODO; run `python -m scripts.progress`"},
    )


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = get_settings().api_key
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


@app.middleware("http")
async def count_requests(request: Request, call_next):
    response = await call_next(request)
    if request.url.path not in {"/metrics", "/health"}:
        M.REQUESTS.labels(request.url.path, str(response.status_code)).inc()
    return response


# ----------------------------------------------------------------- schemas
class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=4000)


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000, examples=["What was Trivenza Bank's Gross NPA in FY2024-25?"])
    doc_ids: list[str] | None = Field(default=None, description="Restrict search to these documents")
    use_cache: bool = True
    force_tier: Literal["small", "strong"] | None = None
    history: list[ChatTurn] | None = Field(
        default=None, max_length=10, description="Previous turns, used to rewrite follow-up questions"
    )


class TextRequest(BaseModel):
    text: str = Field(..., min_length=3, max_length=20000)


# ----------------------------------------------------------------- endpoints
@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok", "version": __version__}


@app.get("/ready", tags=["ops"])
def ready():
    try:
        engine = get_engine()
        if _ingest["running"]:
            # Not ready until the sample corpus is indexed: Kubernetes keeps traffic away meanwhile.
            return JSONResponse(status_code=503, content={"status": "ingesting", "indexed_chunks": engine.store.count()})
        return {
            "status": "ready",
            "indexed_chunks": engine.store.count(),
            "reranker": engine.reranker.active,
            "entity_backend": entity_backend(),
            "models": {t: f"{m.provider}:{m.model}" for t, m in engine.router.models.items()},
            "auto_ingest": {"ingested": _ingest["ingested"], "error": _ingest["error"]},
        }
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "not_ready", "error": str(exc)})


@app.get("/metrics", tags=["ops"], include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/query", tags=["rag"], dependencies=[Depends(require_api_key)])

@app.post("/v1/query", tags=["rag"], dependencies=[Depends(require_api_key)])
def query(req: QueryRequest):
    try:
        history = [t.model_dump() for t in req.history] if req.history else None

        return get_engine().query(
            question=req.question,
            history=history,
            doc_ids=req.doc_ids,
            force_tier=req.force_tier,
            use_cache=req.use_cache,
        ).to_dict()

    except GuardrailViolation as exc:
        raise HTTPException(
            status_code=422,
            detail={"guardrail": exc.kind, "message": exc.detail},
        ) from exc

    except NotImplementedError:
        raise

    except LLMError as exc:
        log.error("LLM failure: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"LLM backend unavailable: {exc}",
        ) from exc



@app.post("/v1/documents", tags=["documents"], dependencies=[Depends(require_api_key)])
async def upload_document(file: UploadFile = File(...)):
    engine = get_engine()
    settings = engine.s
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{suffix}'. Allowed: {sorted(SUPPORTED_EXTENSIONS)}")
    uploads = settings.data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename).name
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_mb * 1024 * 1024:
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB")
            tmp.write(chunk)
    dest = uploads / safe_name
    shutil.move(tmp.name, dest)
    started = time.perf_counter()
    try:
        info = engine.ingest_file(dest)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(503, f"Embedding backend unavailable: {exc}") from exc
    return {**info, "ingest_seconds": round(time.perf_counter() - started, 2)}


@app.get("/v1/documents", tags=["documents"], dependencies=[Depends(require_api_key)])
def list_documents():
    return get_engine().store.documents()


@app.delete("/v1/documents/{doc_id}", tags=["documents"], dependencies=[Depends(require_api_key)])
def delete_document(doc_id: str):
    removed = get_engine().delete_document(doc_id)
    if not removed:
        raise HTTPException(404, f"Document '{doc_id}' not found")
    return {"doc_id": doc_id, "chunks_removed": removed}


@app.post("/v1/analyze/sentiment", tags=["analytics"], dependencies=[Depends(require_api_key)])
def sentiment(req: TextRequest):
    return analyze_sentiment(req.text)


@app.post("/v1/analyze/entities", tags=["analytics"], dependencies=[Depends(require_api_key)])
def entities(req: TextRequest):
    return {"backend": entity_backend(), "entities": extract_entities(req.text)}


@app.delete("/v1/cache", tags=["ops"], dependencies=[Depends(require_api_key)])
def clear_cache():
    return {"cleared": get_engine().cache.clear()}
