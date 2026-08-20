# """
# uv init voice-agent
# cd voice-agent
# uv venv
# source .venv/bin/activate  # Windows: .venv\Scripts\activate
# uv add "torch<2.3.0" "torchaudio<2.3.0" "onnxruntime<1.20.0" fastapi uvicorn websockets qdrant-client sentence-transformers faster-whisper silero-vad kokoro soundfile numpy requests
# ollama pull llama3.2:3b
# uv pip install "torch==2.2.2" "numpy<2" qdrant-client sentence-transformers faster-whisper kokoro soundfile
# """

import json
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct
from fastembed import TextEmbedding

print("Loading ONNX embedding model...")
embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

client = QdrantClient(path="./qdrant_db")
COLLECTION_NAME = "india_tourism"

# BAAI/bge-small-en-v1.5 outputs 384-dimensional vectors
# Replace client.recreate_collection with this:
if client.collection_exists(collection_name=COLLECTION_NAME):
    client.delete_collection(collection_name=COLLECTION_NAME)

client.create_collection(
    collection_name=COLLECTION_NAME,
    vectors_config=VectorParams(size=384, distance=Distance.COSINE)
)

with open("india_tourism_dataset.json", "r", encoding="utf-8") as f:
    tourism_data = json.load(f)

documents = []
payloads = []

for item in tourism_data:
    name = item.get("Name", "Unknown")
    state = item.get("State", "India")
    location_type = item.get("Type", "Attraction")
    desc = item.get("Description", "")
    best_time = item.get("Best_Time", "Anytime")

    text = f"Destination: {name}, State: {state}. Type: {location_type}. Best time to visit: {best_time}. Description: {desc}"
    documents.append(text)
    payloads.append({
        "text": text,
        "name": str(name),
        "state": str(state)
    })

print(f"Generating ONNX embeddings for {len(documents)} documents...")
embeddings = list(embedder.embed(documents))

points = [
    PointStruct(
        id=idx,
        vector=embedding.tolist(),
        payload=payload 
    )
    for idx, (embedding, payload) in enumerate(zip(embeddings, payloads))
]

client.upsert(collection_name=COLLECTION_NAME, points=points)
print("Ingestion complete! Stored in ./qdrant_db")