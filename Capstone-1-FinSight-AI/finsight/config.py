"""Central configuration. Every value can be overridden with an env var (prefix FINSIGHT_)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINSIGHT_", env_file=".env", extra="ignore")

    # --- Storage -----------------------------------------------------------
    data_dir: Path = Path("data")
    chroma_dir: Path = Path("storage/chroma")
    cache_db: Path = Path("storage/semantic_cache.sqlite")
    collection: str = "finsight_docs"

    # --- LLM providers (all free) -------------------------------------------
    # "ollama" runs fully local. "groq" uses Groq's free tier (needs FINSIGHT_GROQ_API_KEY).
    ollama_url: str = "http://localhost:11434"
    groq_url: str = "https://api.groq.com/openai/v1"
    groq_api_key: str = ""

    # Model router: simple questions -> small tier, analytical questions -> strong tier.
    small_provider: str = "ollama"
    small_model: str = "llama3.2"
    strong_provider: str = "ollama"
    strong_model: str = "llama3.1:8b"
    llm_timeout_s: float = 300.0  # CPU-only laptops can need minutes for an 8B answer
    temperature: float = 0.1

    # --- Embeddings ----------------------------------------------------------
    embed_model: str = "nomic-embed-text"

    # --- Retrieval -------------------------------------------------------------
    chunk_size: int = 900
    chunk_overlap: int = 150
    top_k_dense: int = 12
    top_k_sparse: int = 12
    top_k_final: int = 10
    rrf_k: int = 60
    reranker_enabled: bool = True
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # --- Semantic cache ----------------------------------------------------------
    cache_enabled: bool = True
    cache_threshold: float = 0.95
    cache_ttl_s: int = 24 * 3600

    # --- Guardrails ----------------------------------------------------------------
    pii_masking: bool = True
    injection_blocking: bool = True

    # --- API ---------------------------------------------------------------------------
    api_key: str = ""  # empty = auth disabled (local dev)
    # If set and the index is empty at startup, the API ingests this folder itself (handy for Docker/K8s demos).
    auto_ingest_dir: str = ""
    max_upload_mb: int = 25


@lru_cache
def get_settings() -> Settings:
    return Settings()
