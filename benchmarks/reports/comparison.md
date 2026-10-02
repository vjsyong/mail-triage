# Model comparison (auto-generated)

| Model | Params | Quant | Overall raw | Sev-adj | Critical cases | Classify raw | Assistant raw | Classify med lat | Asst med TTFT | Peak VRAM | File size (bf16) | Boot |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Gemma 4 26B-A4B (baseline) | 25.2B MoE / 3.8B active | AWQ-4bit + int8 KV | 92.6 | 90.0 | 3 | 93.4 | 87.6 | 4.33s | 0.09s | - | - | - |
| Qwen3.5-4B | 4B dense | bf16 | 87.1 | 82.0 | 4 | 87.9 | 82.3 | 0.74s | 2.0s | 22.6 GB | 9.3 GB | - |
| Qwen3.5-9B | 9B dense | bf16 | - | - | 0 | - | - | - | - | 21.0 GB | 19.3 GB | - |
