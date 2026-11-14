# Fine-tuning the small tier with LoRA (free Colab GPU)

## 1. Train (Google Colab, free T4)

```python
# Pinned, tested versions (newer transformers 5.x releases changed APIs this script uses)
!pip install -q "transformers==4.46.3" "peft==0.13.2" "datasets==3.1.0" "accelerate==1.1.1"
# upload train_lora.py, then:
!python train_lora.py --max-train 3000
```

The script prints the base vs fine-tuned **token-F1** on 50 held-out questions. Record both numbers in your report.

## 2. Convert to GGUF (free, llama.cpp)

```bash
git clone --depth 1 https://github.com/ggml-org/llama.cpp
pip install -r llama.cpp/requirements.txt
python llama.cpp/convert_hf_to_gguf.py outputs/finsight-qwen-merged --outtype q8_0 --outfile finsight-qwen-q8_0.gguf
```

## 3. Serve with Ollama and plug into FinSight

```bash
cp finsight-qwen-q8_0.gguf finetune/
ollama create finsight-qwen -f finetune/Modelfile
export FINSIGHT_SMALL_MODEL=finsight-qwen
python -m evaluation.run_eval      # compare against the llama3.2 baseline
```

## What to report

| Model (small tier) | Token-F1 (financial-qa-10K) | FinSight answer_accuracy | p50 latency |
| --- | --- | --- | --- |
| Qwen2.5-0.5B base | | | |
| Qwen2.5-0.5B + LoRA | | | |
| llama3.2 (3B) baseline | | | |

Discuss: did a 0.5B fine-tuned model close the gap to a 3B general model? At what latency/memory saving?
