from google.colab import files
files.upload()
##########
!pip install -q kaggle
import os
import shutil

os.makedirs("/root/.kaggle", exist_ok=True)

shutil.move("kaggle.json", "/root/.kaggle/kaggle.json")

os.chmod("/root/.kaggle/kaggle.json", 600)
!kaggle datasets download -d kaggle/recipe-ingredients-dataset
!unzip -q recipe-ingredients-dataset.zip
#######
import json
with open("train.json") as f:
  recipes = json.load(f)

print(len(recipes))
recipes[2]
####
!pip install -q uv
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
########
from langchain_core.documents import Document
doc = []
for recipe in recipes:
  text = f"""
  recipe: {recipe['cuisine']}
  ingredient: {', '.join(recipe['ingredients'])}
  """
  doc.append(
      Document(page_content=text,
      metadata = {
          "cuisine": recipe["cuisine"],
          "ingredients": recipe["ingredients"]
      }
    )
  )

print(doc[0])
########
from langchain_text_splitters import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    chunk_size=300,
    chunk_overlap=50
)

chunks = splitter.split_documents(doc)

print(len(chunks))
#####
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from transformers import pipeline
import torch
import os

# -------------------------
# Load Embedding Model
# -------------------------
device = "cuda" if torch.cuda.is_available() else "cpu"

embedding = HuggingFaceEmbeddings(
    model_name="sentence-transformers/all-MiniLM-L6-v2",
    model_kwargs={"device": device}
)
# -------------------------
# Create and Save FAISS DB
# -------------------------
# Define the folder path for the FAISS index
db_path = "vector_db"

# Check if the database already exists locally, otherwise create it
if os.path.exists(db_path):
    db = FAISS.load_local(
        db_path,
        embedding,
        allow_dangerous_deserialization=True
    )
    print("Loaded FAISS database from local storage.")
else:
    print("Creating FAISS database from documents...")
    db = FAISS.from_documents(chunks, embedding)
    db.save_local(db_path)
    print("FAISS database created and saved locally.")

# -------------------------
# Load LLM
# -------------------------
generator = pipeline(
    "text-generation",
    model="Qwen/Qwen2.5-0.5B-Instruct",
    device=0 if torch.cuda.is_available() else -1,
)

print("=" * 60)
print("📚 Recipe RAG Chatbot")
print("Type 'exit' to quit.")
print("=" * 60)

while True:

    question = input("\nQuestion: ").strip()

    if question.lower() == "exit":
        print("Goodbye!")
        break

    # -------------------------
    # Retrieve relevant recipes
    # -------------------------
    docs = db.similarity_search(question, k=2)

    context = "\n\n".join(
        doc.page_content[:300]
        for doc in docs
    )

    # -------------------------
    # Prompt
    # -------------------------
    prompt = f"""
<|system|>
You are a helpful recipe recommendation assistant.

Answer ONLY using the recipe information provided below.

If the answer is not present in the context, reply exactly:
"I couldn't find a suitable recommendation."

Context:
{context}

<|user|>
{question}

<|assistant|>
"""

    # -------------------------
    # Generate answer
    # -------------------------
    output = generator(
        prompt,
        max_new_tokens=200,
        do_sample=False,
        temperature=0.2,
        pad_token_id=generator.tokenizer.eos_token_id
    )

    answer = output[0]["generated_text"].split("<|assistant|>")[-1].strip()

    print("\nAnswer:\n")
    print(answer)




