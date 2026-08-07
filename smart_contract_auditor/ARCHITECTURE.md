# System Architecture: Multi-Agent Smart Contract Security Auditor CLI

This document outlines the architectural design and system workflow for the **Multi-Agent Smart Contract Security Auditor**—a local, terminal-native CLI tool (inspired by OpenCode/Claude Code). The tool runs directly on the user's local system, leveraging local environment configs, local static analysis tools, code-slicing pre-processors, and a courtroom-inspired multi-agent consensus pipeline to audit smart contracts with low false-positive rates under strict free-tier API rate limits.

---

## 1. High-Level System Architecture

The tool executes entirely via terminal commands, reading smart contracts directly from the user's local file system.

+-----------------------------------------------------------------------------------+
|                           1. CLI & LOCAL ENVIRONMENT                              |
|   Terminal CLI Entry Point (Typer/Click)  |  Local File Reader (.sol files)       |
|   Local Env Config (.env / API Keys)      |  Local Slither Executable Execution   |
+-----------------------------------------------------------------------------------+
|
v
+-----------------------------------------------------------------------------------+
|                     2. PRE-PROCESSING & CODE-SLICING LAYER                        |
|  +---------------------------------+     +-------------------------------------+  |
|  |   Slither JSON Runner           |     |  Noise Stripper & Severity Filter   |  |
|  |  (slither . --json output.json) | --> |  (Keeps High/Med, drops Info/Logs)  |  |
|  +---------------------------------+     +-------------------------------------+  |
|                                                            |                      |
|                                                            v                      |
|                                          +-------------------------------------+  |
|                                          |  Rule-Based Manager & Code Slicer   |  |
|                                          |  (Sends target fn lines 35-70 only) |  |
|                                          +-------------------------------------+  |
+-----------------------------------------------------------------------------------+
|
v
+-----------------------------------------------------------------------------------+
|               3. ROUTER, CACHING & MULTI-AGENT CONSENSUS LAYER                    |
|                                                                                   |
|    +-----------------------------+               +---------------------------+    |
|    | Local SQLite Hash Cache     |               |  Local Budget Counter     |    |
|    | (hash: contract+finding)    |               |  (Pre-checks RPM/Daily)   |    |
|    +-----------------------------+               +---------------------------+    |
|                   |                                            |                  |
|                   +----------------------+---------------------+                  |
|                                          |                                        |
|                                          v                                        |
|                       +------------------------------------+                      |
|                       |        Dynamic API Router          |                      |
|                       | (NVIDIA -> Mistral -> OpenRouter)  |                      |
|                       +------------------------------------+                      |
|                                          |                                        |
|                   +----------------------+----------------------+                 |
|                   |                                             |                 |
|                   v                                             v                 |
|        +---------------------+                       +---------------------+      |
|        |  Prosecutor Agent   |                       |   Defender Agent    |      |
|        | (Fast/Light Models) |                       | (Fast/Light Models) |      |
|        +---------------------+                       +---------------------+      |
|                   |                                             |                 |
|                   +----------------------+----------------------+                 |
|                                          |                                        |
|                                          v                                        |
|                       +------------------------------------+                      |
|                       |            Judge Agent             |                      |
|                       |   (High-Reasoning/Ultra Models)    |                      |
|                       +------------------------------------+                      |
|                                          |                                        |
|                                          v                                        |
|                       +------------------------------------+                      |
|                       |     Pydantic Schema Validator      |                      |
|                       |    (Bounded Retry / Fallback)      |                      |
|                       +------------------------------------+                      |
+-----------------------------------------------------------------------------------+
|
v
+-----------------------------------------------------------------------------------+
|                           4. TERMINAL OUTPUT & EXPORT                             |
|    Rich Terminal Streamed Logs  |  Local Markdown Audit Report Export             |
+-----------------------------------------------------------------------------------+


---

## 2. Core Architectural Components

### A. Terminal CLI Engine & Local System Integration
* **Primary Interface:** Command-Line Interface (CLI) built for terminal-native execution (installable via `pip` or executable package).
* **Local File System Reader:** Scans target directories, resolves relative paths, and loads `.sol` source files directly from local storage.
* **Environment Manager:** Manages local configurations (e.g., loading `NVIDIA_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY` from a local `.env` or system environment).
* **Local Static Execution:** Triggers locally installed Slither binaries in JSON mode (`slither . --json output.json`) via sub-processes on the user's host machine.

### B. Pre-Processing & Context Reduction Engine
To prevent hitting free-tier token limits (TPM) and context window limits:
1. **Rule-Based Manager:** Operates entirely through deterministic Python logic—extracting line numbers, function scope, and AST bounds from Slither without consuming any LLM API calls.
2. **JSON Noise Stripping:** Strips away compiler logs, low-priority informational warnings, style checks, and AST metadata, retaining **only High and Medium severity** findings.
3. **AST Code Slicing:** Maps Slither's reported line numbers directly to the contract source code and extracts **only the target function + surrounding context** (e.g., lines 35–70).

### C. Quota Guardrails, Router & Model Sizing
To manage strict free-tier rate limits (RPM/TPM) across dynamic provider catalogs:
1. **Tiered Provider Hierarchy:**
   * **Primary: NVIDIA NIM (`build.nvidia.com`):** High throughput (~40 RPM ceiling, no daily credit cap).
   * **Secondary: Mistral AI (`La Plateforme Experiment`):** Large token allowance (~1B tokens/month) for failover.
   * **Tertiary: OpenRouter (`openrouter.ai` / `openrouter/free`):** Last-resort fallback for broad model diversity (20 RPM, 50–1,000 requests/day).
2. **Local Request & Quota Counter:** SQLite-backed counter tracks per-provider RPM and daily request counts locally. Prior to dispatching an API call, it checks the current budget and proactively switches providers *before* triggering HTTP 429 errors.
3. **Dynamic Model Discovery:** Avoids fragile hardcoded model IDs. Queries provider endpoints dynamically at startup (e.g., OpenRouter `/models?free=true`) or reads an external `models.json` config.
4. **Role-Based Model Sizing:**
   * **Prosecutor & Defender:** Assigned fast, lightweight models (e.g., `Nemotron-Nano-30B`, `Nemotron-Super`, `Gemma-26B`) to handle high call volumes cleanly.
   * **Judge:** Assigned heavy reasoning models (e.g., `Nemotron-3-Ultra-550B`, `Gemma-31B`) to synthesize the verdict.
5. **Terminal Degradation Alert:** When forced onto fallback tiers or degraded models, the CLI outputs:
   `[WARNING]: Primary API limit reached. Operating in Degraded Mode (Fallback Model Active). Results should be manually verified.`
6. **Optional Delay Buffers:** Configurable backoff delays (`time.sleep()`) between agent calls to prevent per-second bursting. Can be disabled via CLI flag `--fast`.

### D. Output Validation & Caching Layer
1. **Local Hash Caching:** Computes `SHA256(contract_source + slither_finding_id)` and checks `.audit_cache.db`. Identical local runs bypass LLM API calls entirely.
2. **Schema Validation & Bounded Retries:** Passes agent responses through strict `Pydantic` schemas. If a 7B/lightweight model returns malformed JSON, a bounded retry loop (max 2 retries) passes error context back to the model. If it repeatedly fails, it safely defaults to an `INCONCLUSIVE` finding rather than crashing the CLI.

### E. Multi-Agent Courtroom System

| Agent Role | Primary Duty | Core Responsibilities |
| :--- | :--- | :--- |
| **1. Manager (Python)** | Orchestrator | Pure rule-based code slicer. Pre-screens AST structure and extracts function blocks without calling APIs. |
| **2. Prosecutor Agent** | Auditor | Receives the sliced code blocks (e.g., lines 35–70) + Slither warnings; formulates formal "charges" (candidate bugs). |
| **3. Defender Agent** | Critic | Challenges Prosecutor findings. Inspects local variable states, safety modifiers, and guard conditions to filter out false positives. |
| **4. Judge Agent** | Adjudicator | Evaluates arguments from both Prosecutor and Defender against raw code, delivers the final verdict (`CONFIRMED`, `FALSE_POSITIVE`, `INCONCLUSIVE`), provides logical reasoning, and outputs patch recommendations. |

---

## 3. Data Flow Sequence

1. **CLI Trigger:** User invokes the tool in their terminal: `smart-audit run ./contracts/Vault.sol`.
2. **Local File & Env Load:** The CLI engine validates local path access and loads API credentials (`.env`).
3. **Local Slither Execution:** The CLI executes Slither locally in JSON output mode (`slither . --json output.json`).
4. **Noise Stripping & Code Slicing:** The rule-based Manager filters Slither JSON for High/Medium severity and extracts target function code snippets (e.g., lines 35–70).
5. **Cache & Budget Check:** The CLI checks `hash(contract_source + finding)` in `.audit_cache.db`. If missed, `api_router.py` checks local RPM/daily quota counters.
6. **Indictment:** The **Prosecutor Agent** (routed to fast model on NVIDIA NIM) receives sliced code and generates candidate charges.
7. **Defense & Rebuttal:** The **Defender Agent** scrutinizes each charge, arguing why specific flags are false positives or protected by existing logic.
8. **Verdict & Local Export:** The **Judge Agent** (routed to reasoning model) evaluates both arguments, outputs a `Pydantic`-validated JSON verdict, streams results to the terminal window via Rich, and exports `audit_report.md` to the local directory.

---

## 4. Project Folder Structure

```text
smart_contract_auditor/
├── .env.example                 # Template for API keys (NVIDIA_API_KEY, MISTRAL_API_KEY, OPENROUTER_API_KEY)
├── README.md                    # Project overview and CLI usage guide
├── ARCHITECTURE.md              # System design and pipeline specifications
├── requirements.txt             # Dependency management (Typer, Rich, Slither, Pydantic, LangChain)
├── pyproject.toml               # Package build metadata for pip installation
│
└── smart_audit/                 # Main source code directory
    ├── __init__.py
    ├── cli.py                   # CLI entry point (Typer/Click interface commands & flags)
    │
    ├── preprocessor/            # Rule-based pre-processing & context reduction modules
    │   ├── __init__.py
    │   ├── slither_runner.py    # Local Slither subprocess runner (JSON mode)
    │   ├── json_filter.py       # Strips low/info noise from Slither logs
    │   └── code_slicer.py       # Rule-based AST code chunk extractor (e.g., lines 35-70)
    │
    ├── router/                  # API routing, budget management & resilience
    │   ├── __init__.py
    │   ├── api_router.py        # Tiered failover router (NVIDIA -> Mistral -> OpenRouter)
    │   ├── budget_tracker.py    # SQLite local RPM and daily quota tracker
    │   ├── model_discovery.py   # Dynamic free model catalog loader
    │   └── validator.py         # Pydantic schema validation & bounded retry handler
    │
    ├── agents/                  # Multi-agent courtroom implementation
    │   ├── __init__.py
    │   ├── prosecutor.py        # Prosecutor Agent (Auditor - Light Model)
    │   ├── defender.py          # Defender Agent (Critic - Light Model)
    │   ├── judge.py             # Judge Agent (Adjudicator - Heavy Reasoning Model)
    │   └── schemas.py           # Pydantic structured output definitions
    │
    ├── cache/                   # Local caching system
    │   ├── __init__.py
    │   └── hash_cache.py        # SQLite cache keyed on hash(code_slice + finding_id)
    │
    └── utils/                   # Helper modules
        ├── __init__.py
        ├── terminal_ui.py       # Rich terminal formatting, tables, and streaming UI
        └── report_generator.py  # Generates local Markdown/JSON audit reports