<div align="center">

# 🐾 CatPaw

**From a research question to a computational study.**

Multi-agent workflows for first-principles catalysis research with VASP.

[Quick Start](#quick-start) · [Dashboard](#dashboard) · [CatBench](#catbench) · [How It Works](#how-it-works)

</div>

CatPaw turns a natural-language research goal into a graph of calculations, then coordinates structure modeling, VASP setup, validation, SLURM submission, monitoring, and analysis. Ask it to optimize a molecule, explore an adsorption state, or build a catalytic reaction profile.

```text
“Calculate the adsorption energy of CO on Pt(111) at the top site.”
                                 ↓
              Plan → Model → Validate → Calculate → Analyze
                                 ↓
                 Structures, results, and a research report
```

- **Follow the chemistry.** Dependent calculations share structures and reference states; independent branches run in parallel.
- **Inspect the work.** Explore the task graph, structures, validation results, and calculation progress in the dashboard.
- **Join at key steps.** Enable human review to approve or revise the plan, models, parameters, and node results.
- **Build on experience.** Optional memory records planning, modeling, and parameter lessons for later workflows.

## Quick Start

### 1. Prepare your environment

Use Python 3.12+ on a Linux host with access to SLURM, a licensed VASP installation, and your POTCAR library. Have an LLM API connection ready; Materials Project access supports structure lookup.

From the repository root:

```bash
uv sync
source .venv/bin/activate
python -m src.utils.config init
```

You can also install with `pip install -e .`. The configuration command creates a local `config.yaml` from [config.example.yaml](config.example.yaml), preserving an existing configuration.

### 2. Connect your model and cluster

Edit these sections in `config.yaml`:

| Section | What to configure |
| --- | --- |
| `agents` | API URL, key, and model. The example shares one connection through a YAML anchor; each role can also have its own settings. |
| `materials_project.api_key` | Your Materials Project API key for structure lookup. |
| `paths.potcar_dir` | Your POTCAR library, organized as `<potcar_dir>/<tag>/POTCAR`. Default: `database/potcars`. |
| `hpc` | SLURM partition, VASP modules and command, CPU capacity, and resource profiles. |
| `weave` | Set `enabled: false` for local-only observation, or supply your Weave project and API key for execution traces. |

The supplied CPU resource profiles target one node per job with `omp_threads: 1`. Adjust them to your cluster, keeping a final profile with `max_atoms: null` as the fallback. CatPaw checks CPU capacity and KPAR/NCORE compatibility during resource planning.

Relative paths resolve from the repository root. Local settings and credentials live in the Git-ignored `config.yaml`. See [database preparation](database/README.md) for local structures and pseudopotential metadata.

### 3. Give CatPaw a task

```bash
python -m src.main "Calculate the adsorption energy of CO on Pt(111) at the top site" -w co_pt_demo
```

Or start from your own structure:

```bash
python -m src.main "Optimize this structure and analyze its electronic properties" -i ./Fe2O3.cif -w fe2o3_demo
```

Supported inputs include `POSCAR`, `.cif`, and `.xsd`. Running `python -m src.main` in an interactive terminal opens the quickstart wizard.

Results are saved under `workspace/`. Check a run from the terminal:

```bash
python -m src.monitor.service status co_pt_demo
```

## Dashboard

With the project environment activated, start the dashboard and execution service together:

```bash
python -m src.web.launcher
```

Open **http://127.0.0.1:8123** to create tasks, upload an initial structure, inspect the DAG, and review calculation results. The execution service runs workflows independently, so you can restart the dashboard while calculations continue.

Press **Ctrl+C** to stop both services; running calculations continue independently. The dashboard uses port `8123`, and its local execution service uses port `8124`.

**Settings** in the sidebar lets you choose a shared model connection or configure individual agents. Enter the Base URL, API Key, and Model together, or leave all three blank to use the server configuration. Saved overrides apply to new workflows and last until the page refreshes.

For remote access, use SSH forwarding to the loopback services; the services have no built-in authentication.

<details>
<summary>Custom paths and service operation</summary>

Set a custom workspace root or ports through the launcher:

```bash
python -m src.web.launcher --workspace-root /path/to/workspace --port 8123 --executor-port 8124
```

For separate service management, the original commands remain available. Use the same workspace root for both:

```bash
python -m src.execution.service --workspace-root /path/to/workspace --port 8124
python -m src.web.dashboard --workspace-root /path/to/workspace --executor-url http://127.0.0.1:8124
```

Run one execution service per workspace root. For systemd deployment, use separate units and `KillMode=process` on the execution unit to preserve workers during service restarts. After a machine restart or worker failure, inspect the workspace with `--resume ... --reconcile-only` before resuming.

For web-launched tasks, **Stop** terminates the workflow and cancels its queued/running SLURM jobs. **Delete** becomes available once the workflow and jobs have stopped. CLI-launched workspaces support inspection through the dashboard. After an interrupted control request, inspect the current task state before retrying.

Model overrides remain in page memory and the worker environment; supply them again when resuming later from the CLI. On shared filesystems without local file events, set `WATCHFILES_FORCE_POLLING=true` for dashboard updates.

</details>

## How It Works

The **Supervisor** plans a directed acyclic graph (DAG) of calculations. A Python scheduler follows dependencies and starts ready nodes. Each node has a **Coordinator** that delegates the scientific work:

```text
Supervisor → Task graph → Python scheduler
                                ↓
                         Node Coordinator
                         ├── ModelingWorker → ModelingValidator
                         ├── ParamWorker → ParamValidator
                         ├── CalculationRunner
                         └── Analyzer
```

For adsorption studies, bulk optimization feeds a shared clean-slab calculation, followed by independent adsorbed states. Molecular reference calculations form parallel branches. This gives related states a common structural basis for energy comparisons.

Each workspace stores the graph, node tasks, modeling and parameter summaries, validation records, and calculation results. These records support inspection and resuming work. See the [execution guide](src/graph/README.md) for scheduling, shared inputs, and artifact contracts.

## Human Review and Resume

Add `--review` to take part in a workflow:

```bash
python -m src.main "Compare a clean slab with an OH adsorbed state" -w oh_review --review
```

CatPaw pauses for `APPROVE` or `REVISE` at review steps and runs nodes serially. Use a persistent terminal such as `tmux` for CLI reviews, or enable human review when creating a task in the dashboard. Pending web reviews are saved in the workspace.

Inspect and resume an existing run:

```bash
python -m src.main --resume workspace/existing_run --reconcile-only
python -m src.main --resume workspace/existing_run
```

<details>
<summary>Review controls and restarting selected calculations</summary>

Resume inherits the workspace's stored review mode. Pass `--review` or `--no-review` to change it. Optional DAG and node-result review gates can be disabled with `CATPAW_REVIEW_DAG=0` and `CATPAW_REVIEW_NODE_RESULT=0`.

Revising a node result reruns the Analyzer while preserving VASP outputs. Revising the DAG at the plan gate stops the run for replanning.

To recalculate selected nodes and their dependent descendants, preview the affected set, then restart:

```bash
python -m src.main --resume workspace/existing_run --restart-nodes ads_start alloy_ads_start --reconcile-only
python -m src.main --resume workspace/existing_run --restart-nodes ads_start alloy_ads_start
```

Replace the example node IDs with those in your graph. Previous attempts are archived under `.restart_attempts/`; completed upstream work is retained. Selected calculations must be clear of active or unknown SLURM jobs before restarting.

</details>

## CatBench

**72 tasks · 9 categories · 3 dimensions of difficulty**

CatBench evaluates workflows from natural-language instructions to scientific results. Each category contains eight tasks, with reference answers and acceptance criteria.

| Area | Categories |
| --- | --- |
| Structure optimization | Molecules, bulk crystals, surfaces |
| Electronic properties | Bader charge, spin localization, band gaps, d-band centers |
| Catalytic energetics | Adsorption energies, reaction profiles |

Browse the [benchmark overview](benchmark/benchmark_overview.md) for all cases and their modeling, parameterization, and workflow difficulty labels. Each category contains paired `test/` and `answer/` directories.

## Optional Extras

**Bader analysis.** Install the executable from the [official Bader distribution](https://theory.cm.utexas.edu/henkelman/code/bader/) and place it on `PATH` in the analysis environment. On Linux, make it executable with `chmod +x bader`. Analysis scripts also accept `--bader-cmd /path/to/bader`. Bader is installed separately under GPL-3.0-or-later.

**Memory.** Enable `memory.dag`, `memory.modeling`, and `memory.parameter` independently in `config.yaml` to reuse lessons across workflows. All three categories start disabled.

**Execution traces.** Configure `weave.enabled`, `weave.project`, and `weave.api_key` to inspect agent and tool calls in Weave. Traces include model/tool inputs and outputs; scientific results remain in the workspace. See [trace configuration](src/graph/README.md#weave-observation-boundary).

<details>
<summary>Additional configuration controls</summary>

- `CATPAW_CONFIG` selects an alternate YAML configuration file.
- `workflow.max_node_parallel` sets concurrent DAG nodes (default: `2`).
- `VASP_DEFAULT_PARTITION` overrides `hpc.slurm_partition`.
- `llm.api` supplies the default API protocol; individual agents can override it.
- `llm.num_retries: 0` allows unlimited attempts for retryable API errors. Retry waits start at `llm.retry_min_wait` and cap at `llm.retry_max_wait` seconds.
- `CATPAW_MEMORY_READ=0` or `CATPAW_MEMORY_WRITE=0` disables that memory access globally; `CATPAW_MEMORY_ENABLED=0` disables all memory. Enable individual categories in YAML.

</details>

## Explore the Repository

| Path | Contents |
| --- | --- |
| [`src/agents/`](src/agents/) | Agent factories and role prompts |
| [`src/graph/`](src/graph/) | DAG scheduling and persistent workflow state |
| [`src/skills/`](src/skills/) | Scientific skills, scripts, and reference material |
| [`src/tools/`](src/tools/) | Modeling, VASP, and analysis tools |
| [`src/execution/`](src/execution/) · [`src/web/`](src/web/) | Workflow service and dashboard |
| [`database/`](database/README.md) | Structure index, templates, and parameter metadata |
| [`benchmark/`](benchmark/benchmark_overview.md) | CatBench tasks and reference answers |
| [`tests/`](tests/) | Automated tests for configuration, modeling, execution, and recovery |

For development, install the test dependencies and run the suite:

```bash
uv sync --group dev
uv run pytest
```
