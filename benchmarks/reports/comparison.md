# Model comparison (auto-generated)

| Model | Params | Quant | Overall raw | Sev-adj | Critical cases | Classify raw | Assistant raw | Classify med lat | Asst med TTFT | Peak VRAM | File size (bf16) | Boot |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Gemma 4 26B-A4B (baseline) | 25.2B MoE / 3.8B active | AWQ-4bit + int8 KV | 92.6 | 90.0 | 3 | 93.4 | 87.6 | 4.33s | 0.09s | 22.6 GB | - | 397s |
| Qwen3.5-9B | 9B dense | bf16 | 93.9 | 90.6 | 2 | 94.1 | 91.0 | 5.77s | 3.09s | 22.8 GB | 19.3 GB | 280s |
| Qwen3.5-4B | 4B dense | bf16 | 87.4 | 82.5 | 4 | 87.9 | 83.3 | 0.74s | 0.89s | 22.8 GB | 9.3 GB | 280s |
| Gemma 4 E4B | 4.5B eff (8B incl emb) | bf16 | 90.3 | 85.0 | 1 | 92.4 | 88.0 | 6.05s | 0.1s | 22.8 GB | 16.0 GB | 371s |
| Granite 4.2 3B | 3B dense | bf16 | 73.0 | 63.8 | 6 | 72.9 | 68.8 | 0.74s | 0.32s | 22.8 GB | 7.3 GB | 195s |
| LFM2.5-8B-A1B | 8.3B MoE / 1.5B active | bf16 | 76.2 | 68.9 | 5 | 73.0 | 80.8 | 0.34s | 0.06s | 22.2 GB | 17.0 GB | 255s |
| Ling-3.0-tiny | 7.9B total MoE (128 experts, top-8) | bf16 | 83.4 | 77.6 | 5 | 81.6 | 86.8 | 0.68s | 0.13s | 21.9 GB | 15.8 GB | 210s |
