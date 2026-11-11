---
name: param-worker
description: >
  CatPaw parameter-planning skill for ParamWorker. Use when ParamWorker
  creates or revises parameter_summary.json, chooses INCAR/KPOINTS/POTCAR
  settings, writes analysis_contract entries, or generates static VASP input
  files for bulk, slab, molecule, SCF, and DOS calculations.
---

# ParamWorker Parameter Planning

## Core Workflow

1. Read `modeling_summary.json` from the active node directory.
2. Identify subdirectories, structure kinds, calculation intents, optional task_type hints, structure sources, and shared modeling context.
3. Choose the final canonical `task_type` for each calculation entry from task.md, dependency context, modeling facts, and template assets. Treat `task_type_hint` as advisory only.
4. Read the matching calculation template asset, then use POTCAR/ENCUT, DFT+U, and complexity tools to form parameter decisions.
5. Write `parameter_summary.json` for the assigned scientific state. Do not put validation results in this file; ParamValidator writes `parameter_validation.json`.
6. Include one `analysis_contract` object for every calculation entry.
7. Generate INCAR, KPOINTS, and POTCAR through native tools.
8. Record submission resource parameters for CalculationRunner, then leave sbatch generation, submission, and monitoring to CalculationRunner.

ParamWorker produces the execution contract and static VASP input files. CalculationRunner performs submission and monitoring. Treat this boundary as strict.

## Defaults

- Slab relaxation usually uses `ISIF=2` to avoid changing the vacuum.
- KPOINTS default to `CreateKpoints` automatic density; avoid manual high-density grids without a reason.
- Molecule references use `ISMEAR=0`, `SIGMA=0.05`, `ISYM=0`, and Gamma-only KPOINTS.
- Call `DftPlusUEvaluator` for transition-metal or rare-earth systems, especially oxides and sulfides.
- Use `PotcarEncutEvaluator` to choose pseudopotential tags and derive the ENCUT basis instead of searching POTCAR directories or reading POTCAR files directly.
- Use `SystemComplexityEvaluator` to select the configured resource profile and choose `NCORE`, `KPAR`, `total_cpus`, `num_dcus`, and `omp_threads` instead of probing cluster runtime state from the shell. Copy its structured plan exactly and record the profile name as `resource_profile`.
- ParamWorker owns the canonical `task_type` in `parameter_summary.json`; ModelingWorker's `task_type_hint` is not authoritative.

## Demonstration Profile

- Apply template `completion_profile_overrides.demonstration` only when the
  Coordinator supplies the persisted demonstration policy.
- Keep Hamiltonian/comparability settings such as ENCUT, KPOINTS, POTCAR,
  spin, DFT+U, and vdW unchanged; only use the bounded slab-relaxation override.
- Record the completion profile and every override in
  `parameter_summary.json` so validation and reporting can distinguish it from
  the strict template.

## Gotchas

- **ISIF must match the task type.** Only `bulk_opt` uses `ISIF=3` (full cell + ionic relaxation). Slab relaxation and molecule optimization must use `ISIF=2` — using `ISIF=3` will collapse the vacuum or deform the box, producing physically wrong results. When adapting an existing INCAR (e.g. reusing a bulk_opt INCAR for a slab job), always reset ISIF to the value required by the target task-type template. This is the single most common parameter-carry-over mistake.
- Do not omit `analysis_contract`; Analyzer needs it for node result analysis and reaction-path aggregation.
- Add `charge` to `analysis_contract.suggested_analyses` only when the user objective explicitly asks for Bader, charge transfer, oxidation/charge states, or charge-density analysis. Do not add `charge` merely because `LAECHG` or `CHGCAR` files are produced.
- For DOS, PDOS, d-band center, or DOS-derived band-gap objectives, use the `dos_calc` template for the final evidence node and put `dos` in that node's `suggested_analyses`. The prerequisite `scf` node normally needs only `summary` and `diagnosis` unless charge analysis was also requested.
- Do not introduce extreme precision (for example |EDIFFG| < 0.03, K-point density exceeding [6,6,6]) unless the user or upstream artifacts require it.
- Keep comparable reaction-path structures aligned in key Hamiltonian and precision parameters.
- Generate INCAR/KPOINTS/POTCAR only through CreateIncar, CreateKpoints, and CreatePotcar; do not handwrite them.
- Do not submit jobs or monitor jobs from ParamWorker.
- Do not check `which vasp`, `vasp_std`, `module load`, scheduler modules, or POTCAR filesystem locations from ParamWorker.
- Do not full-read `POTCAR`, `CHGCAR`, or `WAVECAR`; if a lightweight metadata sanity check is absolutely necessary, prefer a one-line probe such as `grep -m 1 "PBE" POTCAR`.
- If you find yourself planning shell commands like `sbatch`, `echo INCAR`, or manual pseudopotential searches, stop; submission belongs to CalculationRunner and input generation belongs to native tools.

## References And Assets

- Read `src/skills/parameter-planning/references/parameter-planning-notes.md` for cross-template high-risk parameter notes.
- Read `src/skills/parameter-planning/references/parameter-input-contract.md` for the ParamWorker input-generation contract.
- Use `src/skills/parameter-planning/assets/parameter_summary_template.json` when writing `parameter_summary.json`.
- Use `src/skills/parameter-planning/assets/calculation_templates/index.yaml` to check global defaults and select canonical task templates, then read the matching task-type YAML.
- Unless the user provides special requirements, prioritize recommended parameters in the corresponding task-type YAML file.

## Shared Inputs

Read INPUTS_PATH (ACTIVE_NODE_DIR/inputs.json). A parameter_reference is the
validated clean-slab parameter summary for this same-stage family. Reuse its
Hamiltonian, precision and surface k-mesh. Adapt element-indexed arrays and
POTCAR ordering to this member, and choose resources from its own size.
Do not modify the reference. When preparing clean, consider the elements and
coverage of all requested family members when selecting the common basis.
A reference change affecting comparability requires a new upstream calculation
and explicit downstream invalidation, not a local override.
Scientific stage dependencies live only in the DAG, not summary phase fields.
