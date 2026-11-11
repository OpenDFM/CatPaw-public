---
name: modeling-worker
description: >
  CatPaw ModelingWorker skill for preparing VASP-ready structures, using
  native modeling tools, and writing modeling_summary.json.
---

# Modeling Worker

## Scope

Use this skill only for ModelingWorker. ModelingWorker owns structure source
selection, basis preparation, structure-changing tool execution, final POSCAR
placement, and `modeling_summary.json`.

ModelingWorker does not validate the final modeling contract. ModelingValidator
owns `modeling_validation.json` and the PASS/FAIL decision.

## Workflow

1. Read `task.md` in `ACTIVE_NODE_DIR` and keep the work inside this node.
2. Choose the structure source: upstream artifact, user input, local library, or
   Materials Project lookup.
3. Read inputs.json; derive the assigned state from its named structure input. Prepare a basis only when no upstream structure is supplied.
4. For the assigned modeled state, create a subdirectory and write its final
   structure as `POSCAR`.
5. Use native modeling tools for structure-changing operations:
   adsorption, vacancy creation, substitutional doping, and molecule building.
   `invoke_skill(name="modeling-worker")` loads these instructions; it does not
   execute modeling tools. Call `BuildMoleculeInBox(molecule_input="H2O", ...)`
   directly for water; do not pass a tool name to `invoke_skill`.
6. Write `modeling_summary.json` as structure facts for ParamWorker. Do not add
   validation conclusions or VASP parameter advice.


## Demonstration Profile

For a persisted `demonstration` completion policy, only on node IDs matching
the policy's partial-candidate patterns:

- build one chemically plausible representative state instead of an exhaustive
  sibling set;
- choose the representative from task and upstream structural evidence, and
  record omitted alternatives and the screening rationale in
  `shared_modeling_context`;
- keep the upstream slab/cell fixed and set any ML pre-relaxation to
  `relax_cell=false`.

## Defaults

- Crystal/slab source priority: upstream structure > user-provided structure >
  `LocateStructure` local database > Materials Project fallback.
- Molecule tasks skip crystal lookup and call `BuildMoleculeInBox` directly.
  Use a 15-20 A cubic box edge length. For molecule or cluster summaries, record
  periodic-image separation as `periodic_image_distance_angstrom` instead of
  `vacuum_angstrom`; `vacuum_angstrom` is reserved for slab vacuum thickness.
- Slabs normally use at least 15 A vacuum unless the task states otherwise.
- `CleaveSurface.num_layers` is a target Miller-plane repeat count, not final
  atom-layer count. Keep ordinary slabs within tool defaults unless the task
  explicitly requests thickness convergence. Check the reported actual repeat
  count before proceeding; reduce the cleavage target when it exceeds the ordinary
  budget. Do not raise size limits just to accept an oversized candidate.
- For adsorption, vacancy, and doping surface-family tasks, call
  `AnalyzeSurfaceLayout` before final supercell construction.
- Use absolute paths in tool calls and artifact references.

## Task-Specific Slab Defaults

- Ordinary `slab_opt`, adsorption, vacancy, and doping slabs may use about four
  repeat units, and lower slab atoms may be fixed for semi-infinite surface
  relaxation.
- Slabs prepared for surface electronic descriptors such as DOS, PDOS, and
  d-band center should normally use a semi-infinite surface setup with lower
  layers fixed, unless the user explicitly requests a fully relaxed symmetric
  slab.
- `SURFACE_ENERGY` or explicit surface-energy slabs should target about six
  repeat units, or document thickness-convergence intent when using another
  thickness.
- Do not fix bottom or lower layers for standard surface-energy slabs. The
  standard `2A` surface-energy formula assumes the slab exposes two equivalent
  relaxed surfaces.
- Prefer symmetric slabs with equivalent top and bottom surfaces for standard
  surface-energy calculations.

## References And Assets

- Read `../references/tool-reference.md` for basis and modeler tool parameters.
- Read `../references/site-selection.md` for adsorption, defect, doping, and
  molecule identifier conventions.
- Use `../assets/modeling_summary_template.json` as the summary shape.

## Shared Surface Basis

Read modeling_reference and structure from inputs.json. Preserve the common
cell, vacuum, fixed-atom mapping and site registry. Build only this node's state;
do not recleave or resize the shared slab. Adsorption relaxation may move the
permitted surface atoms. Record the reference path and site in provenance.
For clean-slab preparation, choose one basis suitable for all requested members
and record its site registry in shared_modeling_context.
