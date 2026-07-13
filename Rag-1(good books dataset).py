# on Colab
!pip install -q uv
!uv init goodreads-rag
%cd goodreads-rag
import pandas as pd
# import csv

# Increase the field size limit to handle potentially very large fields
# csv.field_size_limit(131072 * 10) # Increasing by a factor of 10

df = pd.read_csv("data/cleaned_goodreads_books_dataset.csv")

print(df.head())
# load the document into a langchain document
import pandas as pd
from langchain_core.documents import Document
df = pd.read_csv('data/cleaned_goodreads_books_dataset.csv')
documents = []
for _,row in df.iterrows():
    text = f"""
Title: {row['title']}
Author: {row['author']}
Genre: {row['genres']}
Rating: {row['rating']}
Description: {row['description']}
Popular Reviews: {row['popularReviews']}
"""
    documents.append(
        Document(
            page_content=text,
            metadata = {
                "title": row['title'],
                "author": row['author'],
                "rating": row["rating"]
            }
        )
    )

print(len(documents))
import pandas as pd
import csv

# Increase the field size limit to handle potentially very large fields
csv.field_size_limit(131072 * 10) # Increasing by a factor of 10


!uv pip install \
pandas \
langchain \
langchain-community \
langchain-huggingface \
sentence-transformers \
faiss-cpu \
transformers \
accelerate \
torch \
ollama \
tqdm \
pydantic \
bitsandbytes

##### Query #######
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from transformers import pipeline
import torch

# -------------------------
# Load Embedding Model
# -------------------------
embedding = HuggingFaceEmbeddings(
    model_name="sentence-transformers/all-MiniLM-L6-v2"
)
db = FAISS.from_documents(documents, embedding)

# Save the FAISS index to the local directory
db.save_local("vector_db")


# -------------------------
# Load TinyLlama
# -------------------------
generator = pipeline(
    "text-generation",
    model="TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    device=0 if torch.cuda.is_available() else -1,
)

# -------------------------
# Query
# -------------------------
query = "Recommend some magic books"

docs = db.similarity_search(query, k=5)

# Limit context length
context = docs[0].page_content[:500]

prompt = f"""
You are a helpful book recommendation assistant.

Use ONLY the context below.

Context:
{context}

Question:
{query}

Answer:
"""

# -------------------------
# Generate
# -------------------------
result = generator(
    prompt,
    max_new_tokens=200,
    do_sample=False,
    truncation=True,
)

print(result[0]["generated_text"])

########### Intercative chat ########
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from transformers import pipeline
import torch

# -------------------------
# Load Embedding Model
# -------------------------
embedding = HuggingFaceEmbeddings(
    model_name="sentence-transformers/all-MiniLM-L6-v2",
    model_kwargs={"device": "cpu"}  # Keep embeddings on CPU
)

# -------------------------
# Load FAISS DB
# -------------------------
db = FAISS.load_local(
    "vector_db",
    embedding,
    allow_dangerous_deserialization=True
)

# -------------------------
# Load TinyLlama
# -------------------------
generator = pipeline(
    "text-generation",
    model="TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    device=0 if torch.cuda.is_available() else -1,
)

print("=" * 60)
print("📚 Goodreads RAG Chatbot")
print("Type 'exit' to quit.")
print("=" * 60)

while True:

    question = input("\nQuestion: ").strip()

    if question.lower() == "exit":
        print("Goodbye!")
        break

    # Retrieve only 2 documents
    docs = db.similarity_search(question, k=2)

    # Keep only the first 300 characters of each document
    context = "\n\n".join(
        doc.page_content[:300]
        for doc in docs
    )

    prompt = f"""
You are a helpful Goodreads book recommendation assistant.

Answer ONLY using the books provided below.

If the answer is not available in the context, reply:
"I couldn't find a suitable recommendation."

Context:
{context}

Question:
{question}

Answer:
"""

    response = generator(
        prompt,
        max_new_tokens=200,
        do_sample=False,
        return_full_text=False,
    )

    print("\nAnswer:\n")
    print(response[0]["generated_text"])
