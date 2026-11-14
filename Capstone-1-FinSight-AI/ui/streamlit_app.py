"""FinSight AI - analyst UI (Streamlit, free & open source).

Run:  streamlit run ui/streamlit_app.py
Talks to the FastAPI backend at FINSIGHT_API_URL (default http://localhost:8000).
"""

import os

import httpx
import streamlit as st

API_URL = os.getenv("FINSIGHT_API_URL", "http://localhost:8000")
HEADERS = {"X-API-Key": os.getenv("FINSIGHT_API_KEY", "")}

st.set_page_config(page_title="FinSight AI", page_icon="📊", layout="wide")


def api(method: str, path: str, **kwargs):
    try:
        r = httpx.request(method, f"{API_URL}{path}", headers=HEADERS, timeout=300, **kwargs)
    except httpx.HTTPError as exc:
        st.error(f"Cannot reach the FinSight API at {API_URL}: {exc}")
        return None
    if r.status_code >= 400:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
        st.error(f"{r.status_code}: {detail}")
        return None
    return r.json()


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("📊 FinSight AI")
    st.caption("Compliance-aware financial research copilot")
    ready = api("GET", "/ready")
    if ready and ready.get("status") == "ready":
        st.success(f"API ready · {ready['indexed_chunks']} chunks indexed")
        st.caption(
            f"Small: `{ready['models']['small']}`  \nStrong: `{ready['models']['strong']}`  \n"
            f"Reranker: {'on' if ready['reranker'] else 'off'} · NER: {ready['entity_backend']}"
        )

    st.subheader("Documents")
    upload = st.file_uploader("Upload PDF / Markdown / TXT", type=["pdf", "md", "txt"])
    if upload and st.button("Ingest document", width="stretch"):
        with st.spinner("Parsing, chunking and embedding..."):
            info = api("POST", "/v1/documents", files={"file": (upload.name, upload.getvalue())})
        if info:
            st.success(f"Indexed {info['title']} - {info['chunks']} chunks in {info['ingest_seconds']}s")

    docs = api("GET", "/v1/documents") or []
    titles = {d["title"]: d["doc_id"] for d in docs}
    selected = st.multiselect("Search only in", list(titles), help="Leave empty to search all documents")
    for d in docs:
        st.caption(f"• {d['title']} ({d['pages']} pages, {d['chunks']} chunks)")

    st.subheader("Options")
    use_cache = st.toggle("Semantic cache", value=True)
    tier = st.radio("Model", ["auto (router)", "small", "strong"], horizontal=True)

tab_chat, tab_analytics = st.tabs(["💬 Research chat", "🔍 Text analytics"])

# ---------------------------------------------------------------- chat
with tab_chat:
    if "history" not in st.session_state:
        st.session_state.history = []

    for turn in st.session_state.history:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn.get("meta"):
                st.caption(turn["meta"])
            for c in turn.get("citations", []):
                with st.expander(f"[{c['n']}] {c['title']} - page {c['page']}" + (f" · {c['section']}" if c["section"] else "")):
                    st.markdown(c["snippet"])
                    st.caption(f"scores: {c['scores']}")

    if question := st.chat_input("Ask about annual reports, results or regulatory circulars..."):
        st.session_state.history.append({"role": "user", "content": question})
        prior = [{"role": t["role"], "content": t["content"]} for t in st.session_state.history[:-1]][-6:]
        payload = {
            "question": question,
            "use_cache": use_cache,
            "doc_ids": [titles[t] for t in selected] or None,
            "history": prior or None,
        }
        if tier != "auto (router)":
            payload["force_tier"] = tier
        with st.spinner("Retrieving and reasoning..."):
            res = api("POST", "/v1/query", json=payload)
        if res:
            meta = (
                f"{'⚡ cache hit · ' if res['cache_hit'] else ''}model `{res['model'] or '-'}` ({res['tier'] or '-'}) · "
                f"{res['latency_ms']} ms · {'✅ grounded' if res['grounded'] else '⚠️ unverified'}"
                + (f" · 🔒 PII masked: {res['pii_masked']}" if res["pii_masked"] else "")
                + (f"  \n↪ interpreted as: _{res['rewritten_question']}_" if res.get("rewritten_question") else "")
            )
            st.session_state.history.append(
                {"role": "assistant", "content": res["answer"], "meta": meta, "citations": res["citations"]}
            )
        st.rerun()

# ---------------------------------------------------------------- analytics
with tab_analytics:
    st.write("Run financial NER and sentiment on any paragraph (e.g. a results press release).")
    text = st.text_area("Text", height=180, placeholder="Paste a paragraph from a results announcement...")
    c1, c2 = st.columns(2)
    if c1.button("Analyze sentiment", width="stretch") and text:
        res = api("POST", "/v1/analyze/sentiment", json={"text": text})
        if res:
            st.metric("Sentiment", res["label"].title(), f"confidence {res['score']}")
            st.caption(f"backend: {res['backend']}")
    if c2.button("Extract entities", width="stretch") and text:
        res = api("POST", "/v1/analyze/entities", json={"text": text})
        if res:
            st.dataframe(res["entities"], width="stretch")
            st.caption(f"backend: {res['backend']}")
