---
license: apache-2.0
base_model: Qwen/Qwen3-Reranker-0.6B
library_name: mlx
tags:
- sentence-transformers
- text-ranking
- reranker
- mlx
pipeline_tag: text-ranking
---

# mlx-community/Qwen3-Reranker-0.6B-4bit

This model was converted to MLX format from
[Qwen/Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)
using **mlx-lm 0.31.3**.

- Quantization: **affine 4-bit, group_size=64** (~4.5 bits/weight)
- On-disk size: ~331 MB
- Task: text reranking (cross-encoder, yes/no relevance scoring)

## Scoring recipe

Qwen3-Reranker is a causal LM used as a reranker: the relevance score of a
`(query, document)` pair is `softmax([logit("no"), logit("yes")])[1]` at the
last position of the prompt below.

```python
import mlx.core as mx
from mlx_lm import load

model, tok = load("mlx-community/Qwen3-Reranker-0.6B-4bit")
hf = getattr(tok, "_tokenizer", tok)

INSTRUCT = "Given a web search query, retrieve relevant passages that answer the query"
PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements '
          'based on the Query and the Instruct provided. Note that the answer can '
          'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
true_id, false_id = hf.convert_tokens_to_ids("yes"), hf.convert_tokens_to_ids("no")
pre, suf = hf.encode(PREFIX, add_special_tokens=False), hf.encode(SUFFIX, add_special_tokens=False)

def rerank_score(query, doc):
    content = f"<Instruct>: {INSTRUCT}\n<Query>: {query}\n<Document>: {doc}"
    ids = pre + hf.encode(content, add_special_tokens=False) + suf
    logits = model(mx.array([ids]))[:, -1, :]
    pair = mx.stack([logits[0, false_id], logits[0, true_id]])
    return float(mx.exp((pair - mx.logsumexp(pair))[1]))

print(rerank_score("What is the capital of China?", "The capital of China is Beijing."))
```
