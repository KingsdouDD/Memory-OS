---
license: mit
language:
- multilingual
- en
- zh
- de
- es
- fr
- it
- ja
- ko
- pt
- ru
pipeline_tag: feature-extraction
tags:
- mlx
- embedding
- text-embeddings-inference
- retrieval
- sentence-transformers
- 8bit
- quantized
library_name: mlx
base_model: BAAI/bge-m3
---

# BGE-M3 MLX (8-bit Quantized)

This is the [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) model converted to MLX format with 8-bit quantization for Apple Silicon.

## Model Description

BGE-M3 is a versatile embedding model capable of:
- Dense retrieval
- Sparse retrieval
- Multi-vector (ColBERT) retrieval

This 8-bit quantized version offers the best quality among quantized variants.

## Model Details

| Property | Value |
|----------|-------|
| Architecture | XLM-RoBERTa |
| Precision | 8-bit (affine quantization) |
| Embedding Dimension | 1024 |
| Max Sequence Length | 8192 |
| Model Size | ~592 MB |
| Quantization Group Size | 64 |
| Languages | 100+ languages |

## Size Comparison

| Version | Size | Compression |
|---------|------|-------------|
| FP16 | 1.1 GB | - |
| **8-bit** | **592 MB** | **46%** |
| 6-bit | 457 MB | 58% |
| 4-bit | 321 MB | 71% |

## Usage

### With MLX

```python
from mlx_embeddings.utils import load_model, load_tokenizer
import mlx.core as mx

model_path = "mlx-community/bge-m3-mlx-8bit"

# Load model and tokenizer
model = load_model(model_path)
tokenizer = load_tokenizer(model_path)

# Generate embeddings
text = "Hello, world!"
tokens = tokenizer.encode(text)
input_ids = mx.array([tokens])
output = model(input_ids)
embedding = output.last_hidden_state.mean(axis=1)  # Mean pooling

print(f"Embedding shape: {embedding.shape}")  # (1, 1024)
```

### With oMLX

```bash
curl http://127.0.0.1:8000/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model": "bge-m3-mlx-8bit", "input": "Your text here"}'
```

## Quantization Details

- **Method**: Affine quantization
- **Bits per weight**: 8
- **Group size**: 64
- **Source**: Converted from MLX FP16 version

## Recommended Use Cases

- Quality-critical applications
- Production deployments where quality is prioritized
- Best choice when memory allows ~600MB

## License

MIT license (inherited from BAAI/bge-m3)

## Citation

```bibtex
@article{bge_m3,
  title={BGE M3-Embedding: Accurate, Efficient and Versatile Text Embedding},
  author={Chen, Jianlv and Xiao, Shitao and Zhang, Peitian and Luo, Kun and Zhang, Zheng},
  journal={arXiv preprint arXiv:2402.03216},
  year={2024}
}
```

## Disclaimer

This is an unofficial MLX conversion of the BAAI/bge-m3 model. For the original model, see [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3).
