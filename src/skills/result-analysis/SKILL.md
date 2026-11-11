---
name: result-analysis
description: >
  CatPaw Analyzer skill. Use when Analyzer classifies node-level calculation
  outcomes, runs supported charge or DOS analysis scripts, or writes
  node_result.json.
---

# Result Analysis

## Scope

This skill belongs to Analyzer only. It turns one active DAG node's runner,
validation, and optional analysis evidence into `node_result.json`. It does not
submit jobs, monitor jobs, repair inputs, update DAG state, or write
`final_report.md`.

## Workflow

1. Read `task.md`, available modeling/parameter handoff files, and `runner_result.json` when it exists.
2. For completed calculation directories, call `SummarizeCalculationResult`; prefer cached `runner_result.json` evidence over rereading large outputs.
3. If the node stopped before running jobs, use the latest modeling or parameter validation result to write a blocked or failed result without rerunning validator checks.
4. Use each calculation's `analysis_contract.suggested_analyses` to decide whether optional scripts are needed.
5. Follow the result contract's analysis mode: diagnose may return a repair proposal without a terminal file; otherwise classify success/blocked/failed and write node_result.json.
6. WriteNodeResult derives recovery_attempted from the persisted recovery_attempts ledger. Report the attempted repair and its outcome, including any validation blocker.

## Status Rules

- `success`: required calculations converged, structure health is acceptable, or available evidence satisfies the node objective.
- `blocked`: required upstream data, credentials, resources, terminal calculation evidence, or primary-objective analysis files are missing.
- `failed`: completed jobs failed, did not converge, showed structure collapse/severe overlap, or produced evidence that invalidates the node objective.


### Demonstration profile

When the persisted completion policy is `demonstration` and
`runner_result.json:completion_assessment.objective_satisfied=true`:

- success may use one goal-relevant representative calculation even when
  optional sibling candidates failed;
- independently check the selected calculation's structure and magnetization;
  reject low-spin/high-spin branch changes or geometry problems that make the
  evidence scientifically misleading;
- copy the assessment's quality tier, acceptance basis, selected physical
  subdirectory, and excluded calculations into `analysis`;
- expose the selected representative `CONTCAR` as the downstream
  `final_structure` artifact;
- state that approximate evidence is screening/demo grade, not publication
  quality.

## Optional Analyses

Map each `suggested_analyses` value from `analysis_contract` to the action below.

Before running a script, resolve the CatPaw repository root:

1. Use `PROJECT_ROOT` from the task message when it is provided.
2. Otherwise, walk upward from `ACTIVE_NODE_DIR` until
   `src/skills/result-analysis/scripts` exists. In the normal workspace layout
   `<repo>/workspace/<run>/<node>`, this is three parents above
   `ACTIVE_NODE_DIR`.
3. If the scripts directory cannot be located, mark the node `blocked` when the
   missing script is required for the node objective. Do not search broad home
   directories or use a hard-coded developer-machine path.

| `suggested_analyses` | Action | Command |
|---|---|---|
| `summary` | Use `runner_result.json` and `SummarizeCalculationResult`. | none |
| `diagnosis` | Use `runner_result.json`, `SummarizeCalculationResult`, and narrow output snippets only if needed. | none |
| `charge` | Run Bader charge analysis. Requires `CHGCAR`, `AECCAR0`, `AECCAR2`, `POTCAR`, `POSCAR` in the subdirectory. | `python <repo-root>/src/skills/result-analysis/scripts/analyze_bader_charge.py --work-dir <abs-subdir> --json` |
| `dos` | Run DOS analysis. Requires `vasprun.xml` in the subdirectory. | `python <repo-root>/src/skills/result-analysis/scripts/analyze_dos.py --work-dir <abs-subdir> --json` |

Execute scripts directly with TerminalTool. With `--json`, each script prints exactly one JSON object to stdout. Add parsed successful results to the matching calculation entry or `analysis.key_findings`.

For d-band center objectives:

- If the objective asks for a slab surface d-band center and no explicit atom
  indices are provided, call `analyze_dos.py` with
  `--site-selection top-layer`. Add `--target-element <symbol>` when the target
  element is stated.
- If explicit 0-based atom indices are provided in `analysis_contract` or node
  context, call `analyze_dos.py --site-indices <comma-separated-indices>`.
- Use the script's selected-site aggregate result for the requested surface or
  dopant quantity. Do not manually infer layer indices or average per-site
  values in the LLM response.
- For the d-band center answer, report the **full-band** value
  (`selected_site_full_d_band_center_rel_fermi_eV`, or
  `full_d_band_centers_rel_fermi_eV` when no site selection is used). This is the
  first moment of the full d-PDOS (occupied + unoccupied), the standard
  Hammer-Nørskov convention that all literature reference values use. The
  occupied-d-PDOS fields (`d_band_centers_rel_fermi_eV`,
  `selected_site_d_band_center_rel_fermi_eV`) are a secondary diagnostic only;
  do not report them as the d-band center. State that the full-band convention
  is being used.

## Evidence For Reporter

For thermodynamic workflows such as surface energy, adsorption energy, vacancy
formation, or reaction profiles, Analyzer should expose node-local evidence in
`node_result.json`: converged total energies, calculation labels/subdirectories,
basic structural metadata available from summaries, and any geometry-health
limitations. Do not evaluate cross-node thermodynamic formulas in Analyzer;
Reporter combines completed node evidence into the final workspace answer.

## Boundaries

- Do not write Python code, inline scripts, subprocess wrappers, or manual parsers for physical quantities already covered by `src/skills/result-analysis/scripts/`.
- `node_result.json` is the Analyzer output and the DAG node completion contract.
- Describe the method level as VASP/PAW when supported by the calculation
  evidence. Do not introduce DNP, Gaussian-basis, localized-basis, or other
  non-VASP method labels unless they are explicitly present in the evidence.
- Do not bypass validators when recording modeling or parameter repair suggestions; quote their JSON conclusions rather than rerunning their checks.
- Do not run charge or DOS scripts unless the required files exist and the analysis_contract asks for them.
- Do not run Bader charge analysis for a d-band/DOS objective unless the user
  also asked for charge evidence.
- If optional charge or DOS files are missing, record the missing files in `analysis.limitations`; use `blocked` only when that optional analysis is the node's primary objective.
- If `charge` is the node's primary objective, success requires the Bader script
  to return `status="success"` and the calculation subdirectory to contain a
  non-empty `ACF.dat`. A script path error, nonzero script exit, non-success
  JSON, or missing/empty `ACF.dat` is a blocked or failed node, not a successful
  result.
- Do not run `bader` directly as a fallback for the supported script. The script
  is responsible for `AECCAR0 + AECCAR2` reference summing, POTCAR ZVAL mapping,
  JSON normalization, and error handling.
- Do not pass `--site-indices` to `analyze_dos.py` unless explicit 0-based site
  indices are provided by `analysis_contract` or node context. For surface-layer
  requests, prefer the script's deterministic `--site-selection` option instead
  of guessing atom indices.
- Do not full-read large generated VASP files. If a quick metadata check is needed, use a narrow command such as `grep -m 1 "PBE" POTCAR` or `head -10 DOSCAR`.

## References

- Read `src/skills/result-analysis/references/result-contract.md` for the current `node_result.json` contract.
