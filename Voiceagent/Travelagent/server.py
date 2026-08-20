import json
import httpx
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from qdrant_client import QdrantClient
from fastembed import TextEmbedding

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI()

print("[INIT] Loading ONNX fastembed model...")
embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

db_path = BASE_DIR / "qdrant_db"
qdrant = QdrantClient(path=str(db_path))

def retrieve_context(query: str) -> str:
    try:
        vector = list(embedder.embed([query]))[0].tolist()
        if hasattr(qdrant, "query_points"):
            results = qdrant.query_points(
                collection_name="india_tourism",
                query=vector,
                limit=2
            ).points
        else:
            results = qdrant.search(
                collection_name="india_tourism",
                query_vector=vector,
                limit=2
            )
        return "\n".join([r.payload.get("text", "") for r in results if r.payload])
    except Exception as e:
        print(f"[ERROR] Qdrant retrieval failed: {e}")
        return ""

async def stream_rag_tokens(history: list, context: str):
    system_prompt = (
        "You are an Indian tourism voice agent. Answer concisely in 1 to 2 sentences max. "
        f"Use the following retrieved context if relevant:\n{context}"
    )

    # Format full message history for Ollama's chat API endpoint
    messages = [{"role": "system", "content": system_prompt}] + history

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            async with client.stream(
                "POST",
                "http://localhost:11434/api/chat",
                json={"model": "llama3.2:3b", "messages": messages, "stream": True}
            ) as response:
                if response.status_code != 200:
                    yield f"Ollama Error ({response.status_code}). Check model installation."
                    return

                buffer = ""
                async for line in response.aiter_lines():
                    if line:
                        try:
                            payload = json.loads(line)
                            token = payload.get("message", {}).get("content", "")
                            buffer += token
                            if any(p in token for p in [".", "?", "!", "\n"]):
                                sentence = buffer.strip()
                                if sentence:
                                    yield sentence
                                buffer = ""
                        except json.JSONDecodeError:
                            continue
                if buffer.strip():
                    yield buffer.strip()
        except Exception as e:
            print(f"[ERROR] Ollama request failed: {e}")
            yield "Could not connect to Ollama."

@app.get("/")
async def get_index():
    index_file = BASE_DIR / "index.html"
    with open(index_file, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("\n[WS] Client connected.")

    # Maintain session conversation history for this WebSocket connection
    conversation_history = []

    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            
            if data.get("type") == "user_text":
                user_text = data.get("text", "").strip()
                if not user_text:
                    continue

                print(f"[USER] {user_text}")
                
                # Append user message to memory
                conversation_history.append({"role": "user", "content": user_text})
                
                # Limit memory to last 6 turns (3 Q&A pairs) to prevent context overflow
                if len(conversation_history) > 6:
                    conversation_history = conversation_history[-6:]

                context = retrieve_context(user_text)
                full_agent_response = ""

                try:
                    async for sentence in stream_rag_tokens(conversation_history, context):
                        full_agent_response += sentence + " "
                        await websocket.send_json({"type": "llm_text", "text": sentence})

                    # Append complete agent response to memory
                    if full_agent_response.strip():
                        conversation_history.append({"role": "assistant", "content": full_agent_response.strip()})

                except Exception as stream_err:
                    print(f"[ERROR] Processing error: {stream_err}")

    except WebSocketDisconnect:
        print("[WS] Client disconnected.")

# run this on local for ui agent
# uv run uvicorn server:app --host 0.0.0.0 --port 8000
# open for http://localhost:8000/ and start conversation        