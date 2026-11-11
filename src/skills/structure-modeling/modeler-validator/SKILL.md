---
name: modeler-validator
description: >
  CatPaw ModelerValidator skill for checking modeling_summary.json, POSCAR
  artifacts, structure relations, and writing modeling_validation.json.
---

# Modeler Validator

## Scope

Use this skill only for ModelerValidator. ModelerValidator independently checks
whether ModelerWorker produced valid modeling artifacts before ParamWorker runs.

ModelerValidator is read-only for structures and summaries. It only writes
`modeling_validation.json`.

## Workflow

1. Read `task.md` and `modeling_summary.json` from `ACTIVE_NODE_DIR`.
2. Confirm `modeling_summary.json` is valid JSON and contains only structure
   facts, not validation conclusions or VASP parameter advice.
3. Build a compact structure list from `modeling_summary.json` and task context.
4. Call `ValidateStructureRelations` for every slab, including a clean slab with
   one structure and `relations=[]`, so thickness checks run before calculations.
   Include explicit reference/target relations for adsorption, vacancy and doping.
5. Call `ValidateStructure` for standalone POSCAR checks and molecule spacing.
   For adsorption, supply `operation_type="adsorption"`, the clean `slab_poscar`
   and its atom count as `pre_atom_count`; inspect the returned per-atom neighbors
   using `../references/validation-guide.md` before deciding PASS or requesting repair.
6. Write exactly one PASS/FAIL JSON object to `modeling_validation.json` and
   return the same JSON object to Coordinator.

## Decision Rules

- PASS only when required POSCAR files exist, parse, match the task semantics,
  and satisfy the relevant relation checks.
- Under an explicit demonstration policy on a matching representative node,
  accept one validated representative structure even when task.md lists
  alternative sibling candidates. Confirm modeling_summary.json records which
  alternatives were omitted and why.
- When multiple slab structures are directly compared within one surface
  family, treat slab-family alignment as mandatory: same in-plane cell,
  layer/repeat count, fixed-layer convention, vacuum convention, and active-site
  registry across siblings.
- If `ValidateStructureRelations` reports slab thickness or large in-plane
  warnings for a routine directly compared surface family, FAIL unless the task
  explicitly justifies the larger model. Default routine surface families should
  not exceed 5 slab repeats/layers.
- FAIL when a Worker-side repair can fix the issue; include concise
  `next_worker_input`.
- Set `terminal_blocker=true` only for missing upstream data, missing
  credentials, impossible task scope, or clarification needs that ModelerWorker
  cannot repair.
- Do not repair files, rewrite structures, call construction tools, or edit
  `modeling_summary.json`.

## References

- Read `../references/validation-guide.md` for detailed checks and JSON output.
- Read `../references/site-selection.md` when validating adsorption, vacancy,
  doping, or molecule semantics.

## Cross-Node Surface Relations

Read inputs.json. Include the named structure reference in
ValidateStructureRelations when validating adsorption, vacancy or doping.
Compare the member against that shared basis even though it is in another node.
Check cell, constraints and site provenance; shared preparation does not require
identical relaxed coordinates for different adsorbed states.
