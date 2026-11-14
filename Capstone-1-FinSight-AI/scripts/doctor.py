"""Environment preflight check - run this FIRST, before writing any code.

Usage:  python -m scripts.doctor

Checks Python, packages, Ollama, the required models, RAM, GPU and Docker, and prints
exactly what to fix. Exit code 0 = ready for Week 1-2, 1 = something must be fixed.
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import subprocess
import sys

import httpx

from finsight.config import get_settings

# Windows consoles/pipes may default to cp1252; never crash on ₹, ✅ or box characters.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
problems: list[str] = []


def line(status: str, name: str, detail: str) -> None:
    print(f"  {status} {name:<22} {detail}")
    if status == FAIL:
        problems.append(name)


def check_python() -> None:
    v = sys.version_info
    if (3, 11) <= (v.major, v.minor) < (3, 13):
        line(OK, "Python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        line(FAIL, "Python", f"{v.major}.{v.minor} found - install Python 3.12 and recreate the venv")


def check_packages() -> None:
    missing = []
    for mod in ["fastapi", "chromadb", "rank_bm25", "pdfplumber", "httpx", "prometheus_client", "streamlit", "pytest"]:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    if missing:
        line(FAIL, "Python packages", f"missing {missing} - run: pip install -r requirements-dev.txt")
    else:
        line(OK, "Python packages", "core + dev requirements installed")
    try:
        importlib.import_module("langchain_classic")
        line(OK, "LangChain (Week 2)", "installed")
    except Exception:
        line(WARN, "LangChain (Week 2)", "not installed yet - needed in Week 2: pip install -r requirements-experiments.txt")


def check_extras(s) -> None:
    """Optional extras fall back silently at runtime - make their real state visible here."""
    try:
        importlib.import_module("sentence_transformers")
    except Exception:
        line(WARN, "Optional ML extras", "not installed (fine) - re-ranker/FinBERT/spaCy off. See requirements-ml.txt")
        return
    from finsight.analytics.entities import entity_backend
    from finsight.analytics.sentiment import analyze_sentiment
    from finsight.retrieval.hybrid import Reranker

    reranker = Reranker(s.reranker_model, enabled=True).active
    finbert = analyze_sentiment("Profit grew strongly.")["backend"] == "finbert"
    detail = f"re-ranker={'on' if reranker else 'OFF'}, FinBERT={'on' if finbert else 'OFF'}, NER={entity_backend()}"
    if reranker and finbert:
        line(OK, "Optional ML extras", detail)
    else:
        tip = " - Intel Mac? pip install -r requirements-ml-intel-mac.txt" if platform.machine() == "x86_64" else ""
        line(WARN, "Optional ML extras", f"installed but not working: {detail}{tip}")


def check_ollama(s) -> list[str]:
    try:
        tags = httpx.get(f"{s.ollama_url}/api/tags", timeout=5).json()
    except Exception:
        line(
            FAIL, "Ollama server", f"not reachable at {s.ollama_url} - install from ollama.com and start the app / `ollama serve`"
        )
        return []
    names = [m["name"] for m in tags.get("models", [])]
    line(OK, "Ollama server", f"reachable at {s.ollama_url}")
    return names


def check_models(s, names: list[str]) -> None:
    def has(model: str) -> bool:
        base = model if ":" in model else f"{model}:latest"
        return base in names or model in names

    needed = [("embedding model", s.embed_model)]
    if s.small_provider == "ollama":
        needed.append(("small model", s.small_model))
    if s.strong_provider == "ollama":
        needed.append(("strong model", s.strong_model))
    for label, model in needed:
        if has(model):
            line(OK, label, model)
        else:
            line(FAIL, label, f"'{model}' not pulled - run: ollama pull {model}")
    groq_models = [m for prov, m in ((s.small_provider, s.small_model), (s.strong_provider, s.strong_model)) if prov == "groq"]
    if groq_models:
        check_groq(s, groq_models)


def check_groq(s, models: list[str]) -> None:
    """Validate the Groq key and model names with the free /models endpoint (uses no tokens)."""
    if not s.groq_api_key:
        line(FAIL, "Groq API key", "FINSIGHT_GROQ_API_KEY is empty (free key: console.groq.com)")
        return
    try:
        r = httpx.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {s.groq_api_key}"}, timeout=15)
    except Exception as exc:
        line(WARN, "Groq API", f"could not reach api.groq.com ({type(exc).__name__}) - check your internet connection")
        return
    if r.status_code == 401:
        line(FAIL, "Groq API key", "rejected by Groq (401) - copy a fresh key from console.groq.com into FINSIGHT_GROQ_API_KEY")
        return
    if r.status_code != 200:
        line(WARN, "Groq API", f"unexpected HTTP {r.status_code} from Groq")
        return
    available = {m["id"] for m in r.json().get("data", [])}
    line(OK, "Groq API key", "valid")
    for model in models:
        if model in available:
            line(OK, "Groq model", model)
        else:
            line(FAIL, "Groq model", f"'{model}' is not offered by Groq any more - pick one from console.groq.com/docs/models")


def check_hardware(s) -> None:
    ram_gb = None
    try:
        if platform.system() == "Darwin":
            ram_gb = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / 1e9
        elif hasattr(os, "sysconf"):
            ram_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except Exception:
        pass
    if ram_gb is not None:
        status = OK if ram_gb >= 15 else WARN
        tip = "" if ram_gb >= 15 else " - use llama3.2:1b locally and the free Groq tier for the strong model"
        line(status, "RAM", f"{ram_gb:.0f} GB{tip}")

    apple_silicon = platform.system() == "Darwin" and platform.machine() == "arm64"
    nvidia = shutil.which("nvidia-smi") is not None
    if apple_silicon or nvidia:
        line(OK, "GPU acceleration", "Apple Silicon (Metal)" if apple_silicon else "NVIDIA GPU detected")
    elif s.strong_provider == "groq":
        line(OK, "GPU acceleration", "none, but the strong tier uses Groq - fine")
    else:
        line(WARN, "GPU acceleration", "none - local 8B answers take ~50 s on CPU. Recommended: FINSIGHT_STRONG_PROVIDER=groq")


def check_ollama_speed(s, names: list[str]) -> None:
    if s.small_provider != "ollama" or not names:
        return
    try:
        r = httpx.post(
            f"{s.ollama_url}/api/chat",
            json={
                "model": s.small_model,
                "stream": False,
                "messages": [{"role": "user", "content": "Say OK."}],
                "options": {"num_predict": 5},
            },
            timeout=s.llm_timeout_s,
        ).json()
        secs = r.get("total_duration", 0) / 1e9
        line(OK if secs < 30 else WARN, "small model speed", f"{secs:.1f}s for a tiny prompt (first call includes model load)")
    except Exception as exc:
        line(WARN, "small model speed", f"test call failed: {exc}")


def check_docker() -> None:
    if not shutil.which("docker"):
        line(WARN, "Docker (Week 3)", "not installed yet - needed in Week 3: Docker Desktop")
        return
    ok = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    line(
        OK if ok else WARN,
        "Docker (Week 3)",
        "daemon running" if ok else "installed but not running - start Docker Desktop in Week 3",
    )
    line(
        OK if shutil.which("minikube") else WARN,
        "minikube (Week 3)",
        "installed" if shutil.which("minikube") else "not installed yet - needed in Week 3",
    )


def main() -> None:
    s = get_settings()
    print("\nFinSight AI - environment check\n")
    check_python()
    check_packages()
    check_extras(s)
    names = check_ollama(s)
    if names or s.small_provider != "ollama":
        check_models(s, names)
    check_hardware(s)
    check_ollama_speed(s, names)
    check_docker()
    if problems:
        print(f"\n  Fix these before you start: {', '.join(problems)}\n")
        sys.exit(1)
    print("\n  Environment ready. Next: python -m scripts.progress\n")


if __name__ == "__main__":
    main()
