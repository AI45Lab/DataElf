# Security Audit Tool

## Overview

The `security_audit` tool performs multi-dimensional security audits on training datasets (SFT / RL / DPO / Benchmark), producing a security score, risk distribution, and a per-sample detail report.

The tool covers the following risk dimensions:

| Risk Type | `risk_type` | Description |
|-----------|-------------|-------------|
| Harmful Content | `harmful` | Illegal activity, personal attacks, incitement to violence, hate speech, pornography, self-harm promotion, threats, etc. |
| Toxicity | `toxicity` | Sarcasm, insults, offensive language, etc. |
| Bias & Discrimination | `bias` | Gender bias, racial bias, regional prejudice, political bias, etc. |
| PII Leakage | `pii` | Identity documents, contact details, home addresses, account credentials, and other personally identifiable information |
| Secret Leakage | `secret` | API keys, access tokens, encryption private keys, hardcoded passwords, etc. |
| Label Flipping | `label_flipping` | DPO data where chosen/rejected labels do not match actual quality |
| Factual Inconsistency | `factual_inconsistency` | Contextual factual mismatch (RAG / text summarization scenarios) |
| Self-Contradiction | `self_contradiction` | Logical contradictions or inconsistent statements within the same sample |
| Instruction Mismatch | `instruction_mismatch` | Response fails to follow instructions — missing elements, format deviation, constraint violations, etc. |
| Backdoor Injection | `backdoor` | Malicious samples that manipulate model behavior via specific trigger tokens |
| Prompt Injection | `prompt_injection` | Instruction override, indirect injection, privilege spoofing, encoding obfuscation, etc. |
| Jailbreak | `jailbreak` | Role hijacking, hypothetical framing, step-by-step induction, and other alignment-bypass prompts |
| Sycophancy | `sycophancy` | Model responses that pander to user bias and lack independent judgment |

Use cases:

- **Pre-training review for SFT / DPO / RL data**: Run a full security scan on annotated or synthetic datasets to filter out harmful, biased, PII-leaking, and label-flipped samples before training ingestion.
- **Benchmark quality validation**: Check question–answer consistency and deduplication before publishing an evaluation set to ensure trustworthy benchmark results.
- **RAG / summarization training data inspection**: Validate factual consistency between model responses and retrieved context for samples that contain external documents.
- **Adversarial dataset auditing**: Screen red-teaming or adversarial prompt datasets to identify genuinely exploitable jailbreak and prompt injection samples.
- **Automated cleaning in data pipelines**: Embed into data processing pipelines for batch security filtering of crawled or crowdsourced data.
- **Compliance & privacy review**: Scan for PII and credential leakage before data sharing or model release to meet GDPR, data security regulations, and similar compliance requirements.


## Input Schema

The tool accepts a list of records, where each record is a `dict` with the following fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | `str` | Yes | Unique sample identifier |
| `dataset_type` | `str` | Yes | Dataset type: `sft` / `rl` / `dpo` / `benchmark` |
| `messages` | `list[dict]` | Yes | Conversation history; each entry contains `role` (`system`/`user`/`assistant`/`tool`) and `content` |
| `response` | `dict` | No | Model response (SFT / RL scenarios) |
| `chosen_response` | `dict` | No | DPO preferred response |
| `rejected_response` | `dict` | No | DPO non-preferred response |
| `context` | `str` | No | Context required for factual consistency checks; common in RAG and summarization tasks |
| `ground_truth_answer` | `str` | No | Required for rule-based RL; ground truth answer |
| `reference_answer` | `str` | No | Required for LLM-judge RL; reference answer |

Example input sample:
```json
{
    "id": "clean_sample",
    "dataset_type": "sft",
    "messages": [
      {"role": "user", "content": "What is the capital of France?"}
    ],
    "response": {"role": "assistant", "content": "The capital of France is Paris."}
},
```

## Parameters

`run_tool()` parameters:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `data` | `list[dict]` | Yes | — | List of dataset records to audit |
| `checker_names` | `list[str]` | No | `[]` | List of Checker class names to enable; see [Checkers](#checkers) |
| `max_workers` | `int` | No | `4` | Number of parallel worker threads |



## Output

```yaml
result:
  security_score: float        # Weighted security score (0.0–1.0; higher is safer)
  passed: bool                 # true when security_score >= threshold
  total_issues: int            # Number of samples flagged as risky
  flagged_samples: int         # Same as total_issues
  safe_samples: int            # Number of samples not flagged
  total_samples: int           # Total number of samples
  flagged_rate: float          # Flag rate (flagged_samples / total_samples)

metadata:
  task_name: str               # Audit task name
  checker_names: list[str]     # Checkers used in this run
  create_time: str             # Task start time (ISO format)
  finish_time: str             # Task finish time (ISO format)
  risk_distribution:           # Distribution statistics per risk type
    pii:
      total: int               # Total samples inspected for this risk
      flagged: int             # Number of hits
    harmful: {total: int, flagged: int}
    # ... other risk types (14 in total)
  checker_stats:               # Execution statistics per checker
    PIIRule:
      total: int               # Successfully executed count
      flagged: int             # Flagged count
      error: int               # Execution error count
      content_filter: int      # Content filter triggered count

artifacts:
  report_md: str               # File path of the full audit report in Markdown format
  sample_results: str          # File path of per-sample audit result details
```

**`sample_results` single entry structure:**

```yaml
sample_id: str
flagged: bool
categories:                    # Whether each risk type was triggered
  harmful: bool
  pii: bool
  # ...
category_scores:               # Score per risk type (0.0–1.0)
  harmful: float
  pii: float
  # ...
results:                       # Raw result from each checker for this sample
  - checker_name: str
    risk_type: str
    success: bool
    score: float               # Risk score (0.0–1.0)
    flagged: bool              # true when score >= threshold
    details: dict              # Checker-specific detail information
```


## Example

### Pipeline DSL Example 1: No checkers explicitly specified by the user
```python

log_step("Load dataset")

data = load_dataset("dataset")

log_step("Run security audit")

audit = run_tool(
    "security_audit",
    data=data
)

save_result(audit)

```

### Pipeline DSL Example 2: Checkers explicitly specified by the user
```python

log_step("Load dataset")

data = load_dataset("dataset")

log_step("Run security audit with all LLM-based checkers")

audit = run_tool(
    "security_audit",
    data=data,
    checker_names=[
        "HarmfulContentLLMJudge",
        "BiasLLMJudge",
        "ToxicityLLMJudge",
        "PIILLMJudge",
        "JailbreakLLMJudge",
        "PromptInjectionLLMJudge",
        "SelfContradictionLLMJudge",
        "InstructionMismatchLLMJudge",
        "FactualInconsistancyLLMJudge",
        "SycophancyLLMJudge",
        "DPOLabelFlipLLMJudge",
    ]
)

save_result(audit)

```



## Checkers

The tool ships with 23 built-in checkers divided into four categories, which can be freely combined by class name.

### Rule-Based Checkers

Deterministic regex / keyword matching. No model required, runs offline. Score is either 0.0 or 1.0.

| Class | Risk Type | Detection Target |
|-------|-----------|-----------------|
| `PIIRule` | `pii` | Phone numbers, email addresses, ID cards, bank cards, etc. (includes Luhn and CN ID validation) |
| `SecretRule` | `secret` | API keys, tokens, hardcoded passwords (AWS, GitHub, OpenAI, JWT, etc.) |
| `ToxicityKeywordRule` | `toxicity` | Toxic keywords (Chinese and English keyword list matching) |
| `HarmfulKeywordRule` | `harmful` | Harmful content keywords (Chinese and English keyword list matching) |
| `BiasKeywordRule` | `bias` | Biased terms (Hurtlex lexicon matching) |

### LLM-as-a-Judge Checkers

Use an LLM for semantic reasoning, returning a 0.0–1.0 score. Requires LLM API access.

| Class | Risk Type | Detection Target |
|-------|-----------|-----------------|
| `HarmfulContentLLMJudge` | `harmful` | Harmful content |
| `ToxicityLLMJudge` | `toxicity` | Toxic content |
| `BiasLLMJudge` | `bias` | Biased content |
| `PIILLMJudge` | `pii` | PII leakage |
| `JailbreakLLMJudge` | `jailbreak` | Jailbreak prompts |
| `PromptInjectionLLMJudge` | `prompt_injection` | Prompt injection |
| `SelfContradictionLLMJudge` | `self_contradiction` | Self-contradictions |
| `InstructionMismatchLLMJudge` | `instruction_mismatch` | Instruction mismatch |
| `FactualInconsistancyLLMJudge` | `factual_inconsistency` | Contextual factual consistency |
| `SycophancyLLMJudge` | `sycophancy` | Sycophancy |
| `DPOLabelFlipLLMJudge` | `label_flipping` | DPO label flipping |

### Model-Based Checkers

Use dedicated classification models with optional GPU acceleration; supports bfloat16/float16 inference.

| Class | Risk Type | Model | Detection Target |
|-------|-----------|-------|------------------|
| `HarmfulContentClassifier` | `harmful` | `meta-llama/Llama-Guard-3-8B` | Harmful content |
| `ToxicityClassifier` | `toxicity` | Detoxify | Toxic content |
| `BiasClassifier` | `bias` | `cirimus/modernbert-large-bias-type-classifier` | Biased content |
| `PIINERDetector` | `pii` | Microsoft Presidio NER model | PII leakage |
| `JailbreakClassifier` | `jailbreak` | `allenai/WildGuard` | Jailbreak prompts |
| `PromptInjectionClassifier` | `prompt_injection` | `leolee99/PIGuard` | Prompt injection |

### Heuristic Checkers

| Class | Risk Type | Detection Target |
|-------|-----------|-----------------|
| `GraCeFulBackdoorDefender` | `backdoor` | Backdoor samples |

## Configuration

Users can select which checkers to enable by configuring `tools/security_audit/default.yaml`:

```yaml
checkers:
  - HarmfulContentLLMJudge
  - FactualInconsistancyLLMJudge
  # - DPOLabelFlipLLMJudge
  # - name: JailbreakLLMJudge
  #   enabled: false
  - name: GraCeFulBackdoorDefender
    enabled: true
    params:
      victim_config:
        type: casual
        model: llama
        path: /mnt/shared-storage-gpfs2/gpfs2-shared-public/huggingface/zskj-hub/models--meta-llama--Llama-2-7b-chat-hf
        device: gpu
```

Risk score weights used to compute `security_score` can be adjusted in `tools/security_audit/default.yaml`:

| Risk Type | Default Weight |
|-----------|---------------|
| `prompt_injection` | 10 |
| `jailbreak` | 10 |
| `backdoor` | 8 |
| `label_flipping` | 7 |
| `self_contradiction` | 7 |
| `harmful` | 6 |
| `instruction_mismatch` | 6 |
| `toxicity` | 5 |
| `bias` | 5 |
| `factual_inconsistency` | 4 |
| `pii` | 3 |
| `secret` | 3 |
| `sycophancy` | 2 |


When LLM Judge is enabled, configure the LLM parameters in `config.yaml`:

```yaml
tool_llm:
  model: gpt-4o-mini
  api_key: ${OPENAI_API_KEY}
  base_url: ${OPENAI_BASE_URL}
  max_retries: 3
  retry_delay: 1.0
```

When Model-based checkers are enabled, configure model paths in `tools/security_audit/default.yaml`:

```yaml
model_paths:
  jailbreak_classifier_model: /path/to/allenai/wildguard
  prompt_injection_classifier_model: /path/to/leolee99/PIGuard
  harmful_content_classifier_model: /path/to/Llama-Guard-3-8B
  bias_classifier_model: /path/to/cirimus/modernbert-large-bias-type-classifier
```

---

## Dependencies

| Dependency | Purpose | Required |
|------------|---------|----------|
| `torch` + `transformers` | Model-Based Checker inference | Required when Model-Based Checkers are enabled |
| LLM API (OpenAI-compatible) | Internal LLM calls | Required when LLM-as-a-Judge Checkers are enabled |
| GPU | Accelerate Model-Based Checkers | No (CPU supported, but slower) |

---

## Experiments

Since there is currently no unified benchmark for training data security auditing, we sampled from multiple public datasets by risk type and constructed an evaluation set covering 13 categories of security risks to assess the effectiveness of data security audit tools.

For poisoning-type risks, we further performed manual construction after sampling:
- **DPO label flipping** was obtained by swapping the `chosen`/`rejected` labels from HH-RLHF;
- **Factual deviation** was generated by injecting context-contradicting answers into TruthfulQA samples;
- **Instruction mismatch** was created by tampering with IFEval sample responses to violate the original instruction constraints.

Secret leakage and backdoor samples were generated with the help of LLMs. Approximately 100 samples were drawn for each risk type, forming a multi-dimensional security audit evaluation set. The evaluation metric is **Recall**, measuring the detection capability of each method on risky samples.

We compared the platform's audit tool `DataElf` against 4 baseline methods, of which 3 are based on dedicated safety models: `LLaMA-Guard-3-8B`, `Qwen3Guard-Gen-8B`, and `WildGuard-7B`; and 1 is a general-purpose LLM-as-a-judge framework `DeepEval`.

| Risk Type | LLaMA-Guard-3-8B | Qwen3Guard-Gen-8B | WildGuard-7B | DeepEval | DataElf (Ours) |
|-----------|:---:|:---:|:---:|:---:|:---:|
| PII Leakage | 78 | 89 | 67 | 87 | **99** |
| Secret Leakage | 24 | 41 | 18 | 42 | **93** |
| Harmful Content | **99** | 98 | 67 | 76 | **99** |
| Toxicity | 76 | **78** | 60 | 69 | **78** |
| Bias & Discrimination | 61 | 66 | 45 | 66 | **72** |
| Prompt Injection | 36 | 78 | 82 | 80 | **85** |
| Jailbreak | 87 | 95 | **97** | 91 | **97** |
| Sycophancy | 17 | 12 | 15 | 18 | **70** |
| DPO Label Flipping | 9 | 12 | 10 | 0 | **39** |
| Factual Inconsistency | 4 | 5 | 2 | **94** | **94** |
| Self-Contradiction | 32 | 45 | 0 | 4 | **69** |
| Instruction Mismatch | 3 | 10 | 5 | 5 | **71** |
| Backdoor Injection | 0 | 0 | 0 | 0 | **80** |
| **Average** | 40.46 | 48.38 | 36.00 | 48.62 | **80.46** |

![Recall comparison of data security audit tools across 13 risk types](./recall_security_risk.png)

`DataElf` achieves an average recall of 80.46% across 13 risk types, substantially outperforming the next-best baselines `DeepEval` (48.62%) and `Qwen3Guard-Gen-8B` (48.38%). Analyzing results by risk type reveals that the three dedicated-model methods perform reasonably on risk types covered by their training objectives (e.g., `LLaMA-Guard-3-8B` reaches 99% on harmful content, `WildGuard-7B` reaches 97% on jailbreak detection), but recall drops sharply on risk types beyond their design scope (e.g., label flipping, factual deviation, instruction mismatch, and backdoor injection). `DeepEval`, as a general-purpose LLM-based framework, achieves 94% on factual inconsistency detection through semantic reasoning, but also performs poorly on dimensions requiring fine-grained comparative analysis such as label flipping, self-contradiction, and instruction mismatch.

In contrast, `DataElf` integrates four types of checkers — rule matching, LLM semantic reasoning, dedicated classification models, and heuristic statistical analysis — maintaining high recall on par with the best baselines for traditional security risks (PII, harmful content, jailbreak, etc.), while achieving a qualitative breakthrough from near-zero detection to effective identification on data poisoning risks (label flipping, self-contradiction, instruction mismatch, backdoor injection). This demonstrates the necessity of multi-method synergy for comprehensive full-dimension security auditing.
