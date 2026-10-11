# Evaluation

`run_cases.py` runs the workflow on test inputs and records expected versus actual behaviour.
It always works on a **copy** of `loans.db` (made with SQLite's backup API; the real file is
only read) and a temporary uploads folder, both deleted afterwards, so the real database never
receives test rows.

## Before you run it

```powershell
python make_samples.py                         # writes samples/ (20 applicants, answer key)
python train_ebm.py --target-means default     # a trained model in models/
```

The LLM cases also need the school server settings in `.env` (see the main README).

## The six cases

```powershell
python eval/run_cases.py                    # uses LLM_MODEL_ACTIVE
python eval/run_cases.py --model qwen3.5:35b
```

| Case | Requirement |
|---|---|
| Clean sample, then a scripted officer approval | 1. Representative successful case |
| Planted income mismatch | 2. Different input condition / branch |
| Text file renamed to `.pdf` | 3. Invalid or failing case |
| Missing bank statement | Extra: failing case |
| LLM turned off | Extra: fallback path |
| Interrupted inside the risk score step, then resumed | Extra: recovery path |

If the LLM server can't be reached, the first two cases are recorded as "not run" and the
others still run.

## Comparing two models

```powershell
python eval/run_cases.py --compare qwen3.5:latest,qwen3.5:35b --samples all
python eval/run_cases.py --compare qwen3.5:latest,qwen3.5:35b --samples S01,S04,S10
```

List the smaller model first. The script applies the decision rule from `CLAUDE2.md` (E10):
use the smaller model if it is within 5 points on extraction accuracy, catches at least as many
planted mismatches, and its validation pass rate is not more than 5 points lower. Running all
20 samples with two models takes roughly an hour.

## Results

Each run writes `eval/results/<YYYY-MM-DD>/`: `cases.md` and `cases.json`, or `compare.md`
and `compare.json`. The results hold only the fictional sample applicants, so they can be
committed. Interpretations in `cases.md` are drafts for the team to review.
