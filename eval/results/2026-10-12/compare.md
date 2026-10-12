# Model comparison on 20 samples

| Measure | `qwen3.5:latest` | `qwen3.5:35b` |
|---|---|---|
| extraction field accuracy | 1.0 | 1.0 |
| fields | 200/200 | 200/200 |
| valid json | 40/40 | 40/40 |
| planted caught | 5/5 | 5/5 |
| false mismatch flags | 0/15 | 0/15 |
| pattern findings planted | 4/5 | 5/5 |
| pattern findings clean | 0/15 | 1/15 |
| validation pass | 17/19 | 20/20 |
| retries | 0 | 0 |
| avg seconds | 113.3 | 38.3 |

Decision (E10 rule): **qwen3.5:35b** (smaller model meets the rule: False).
