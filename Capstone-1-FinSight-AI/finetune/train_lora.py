"""LoRA fine-tuning of a small open model for grounded financial Q&A (Module 6, Session 7).

Runs on the FREE Google Colab T4 GPU in ~25-35 minutes.
  1. Colab -> Runtime -> Change runtime type -> T4 GPU
  2. !pip install -q "transformers==4.46.3" "peft==0.13.2" "datasets==3.1.0" "accelerate==1.1.1"
  3. Upload this file and run:  !python train_lora.py --max-train 3000
  4. Download outputs/finsight-qwen-merged and convert to GGUF for Ollama (see finetune/README.md)

Base model : Qwen/Qwen2.5-0.5B-Instruct (Apache-2.0, free)
Dataset    : virattt/financial-qa-10K (question / answer / context from SEC 10-K filings, free on Hugging Face)
Task       : answer a question ONLY from the given context - exactly what FinSight's small tier must do.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter

import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    DataCollatorForSeq2Seq,
    Trainer,
    TrainingArguments,
)

SYSTEM = "Answer the question using only the context. Quote numbers exactly. Be concise."


def build_prompt(tok, context: str, question: str, answer: str | None = None) -> str:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
    ]
    prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return prompt + (answer + tok.eos_token if answer is not None else "")


def token_f1(pred: str, gold: str) -> float:
    p, g = re.findall(r"\w+", pred.lower()), re.findall(r"\w+", gold.lower())
    common = sum((Counter(p) & Counter(g)).values())
    if not common:
        return 0.0
    prec, rec = common / len(p), common / len(g)
    return 2 * prec * rec / (prec + rec)


@torch.no_grad()
def evaluate(model, tok, rows, max_new_tokens=96) -> float:
    model.eval()
    scores = []
    for r in rows:
        prompt = build_prompt(tok, r["context"], r["question"])
        ids = tok(prompt, return_tensors="pt").to(model.device)
        out = model.generate(**ids, max_new_tokens=max_new_tokens, do_sample=False, pad_token_id=tok.eos_token_id)
        pred = tok.decode(out[0][ids["input_ids"].shape[1] :], skip_special_tokens=True)
        scores.append(token_f1(pred, r["answer"]))
    return sum(scores) / len(scores)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="Qwen/Qwen2.5-0.5B-Instruct")
    ap.add_argument("--max-train", type=int, default=3000)
    ap.add_argument("--eval-n", type=int, default=50)
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--out", default="outputs")
    args = ap.parse_args()

    ds = load_dataset("virattt/financial-qa-10K", split="train").shuffle(seed=42)
    test_rows = ds.select(range(args.eval_n))
    train_rows = ds.select(range(args.eval_n, args.eval_n + args.max_train))

    tok = AutoTokenizer.from_pretrained(args.base)
    tok.pad_token = tok.pad_token or tok.eos_token
    dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    # GPU (Colab T4): let accelerate place the model. CPU / Apple MPS: plain fp32 on CPU (MPS lacks bf16 support).
    device_map = "auto" if torch.cuda.is_available() else None
    model = AutoModelForCausalLM.from_pretrained(args.base, torch_dtype=dtype, device_map=device_map)

    base_f1 = evaluate(model, tok, test_rows)
    print(f"Base model token-F1 on {args.eval_n} held-out questions: {base_f1:.3f}")

    def tokenize(r):
        # [BONUS] TODO - build the full prompt+answer with build_prompt(...), tokenize it (max_length=768)
        # and return input_ids, attention_mask and labels. Mask the prompt tokens in `labels` with -100
        # so the loss is computed ONLY on the answer tokens. Why does this matter? Explain in your report.
        raise NotImplementedError("BONUS: implement tokenize with prompt masking")

    train = train_rows.map(tokenize, remove_columns=train_rows.column_names)

    # [BONUS] TODO - define a LoraConfig for CAUSAL_LM. Choose r, lora_alpha, lora_dropout and
    # target_modules (Qwen2 attention: q_proj, k_proj, v_proj, o_proj; MLP: gate_proj, up_proj, down_proj).
    # Report the trainable-parameter percentage printed below and justify your choices.
    lora = None
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()  # ~1-2% of weights are trained

    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=f"{args.out}/checkpoints",
            num_train_epochs=args.epochs,
            per_device_train_batch_size=4,
            gradient_accumulation_steps=4,
            learning_rate=2e-4,
            lr_scheduler_type="cosine",
            warmup_ratio=0.05,
            logging_steps=20,
            save_strategy="no",
            fp16=torch.cuda.is_available(),
            use_cpu=not torch.cuda.is_available(),  # Trainer would otherwise pick Apple MPS
            report_to="none",
        ),
        train_dataset=train,
        data_collator=DataCollatorForSeq2Seq(tok, padding=True, label_pad_token_id=-100),
    )
    trainer.train()

    tuned_f1 = evaluate(model, tok, test_rows)
    print(f"\nToken-F1  base={base_f1:.3f}  fine-tuned={tuned_f1:.3f}  delta={tuned_f1 - base_f1:+.3f}")

    model.save_pretrained(f"{args.out}/finsight-qwen-lora")
    merged = model.merge_and_unload()
    merged.save_pretrained(f"{args.out}/finsight-qwen-merged")
    tok.save_pretrained(f"{args.out}/finsight-qwen-merged")
    print(f"Saved adapter and merged model under {args.out}/")


if __name__ == "__main__":
    main()
