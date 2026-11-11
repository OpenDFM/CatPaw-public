---
name: dag-orchestration
description: Design calculation DAGs with explicit artifact dependencies and shared surface preparation.
---

# DAG Orchestration

## Node Boundaries

One node is one scientific state at one calculation stage. Its Coordinator owns
modeling, validation, parameter generation, submission, and analysis. Different
states stay separate even when they share settings. Recovery attempts belong to
the same logical node. Reporting and energy algebra happen after the DAG.

Canonical task types: `bulk_opt`, `slab_opt`, `mol_opt`, `scf`, `dos_calc`.
Use `bulk_opt` for bulk relaxation, `slab_opt` for one slab state, `mol_opt` for
one isolated molecule, `scf` for fixed-geometry charge/energy evidence, and
`dos_calc` for DOS/PDOS evidence. Do not invent aliases or helper calculation types.

## Decomposition Defaults

| Objective | Calculation DAG |
|---|---|
| Optimize a molecule or bulk | one matching opt node |
| Surface from bulk | bulk_opt -> clean_slab_opt |
| Adsorption / reaction profile | bulk_opt -> clean_slab_opt -> independent adsorbed-state opt nodes; independent molecule references |
| Vacancy / doping comparison | bulk_opt -> clean_slab_opt -> independent modified-state opt nodes |
| Bader / charge / spin localization | relevant opt -> scf |
| DOS / PDOS / d-band center / DOS-derived gap | relevant opt -> scf -> dos_calc |
| Surface energy | bulk_opt -> slab_opt; follow SURFACE_ENERGY |

Only add electronic stages requested by the objective. An optimization already
computes an energy; do not automatically add SCF to every adsorption task.
Use a supplied compatible optimized structure when available instead of
repeating its optimization. Structure retrieval is internal work.

## Shared Surface Preparation

Default to optimizing the clean slab before building adsorption states. That
node prepares and validates the common cell, fixed layers, vacuum and site
registry, and records the modeling and parameter summaries for the family.
Describe all requested family members in the clean node's context so its workers
can select a common basis suitable for the entire family; execute only clean.

Each adsorbed, defective or doped state consumes that same completed clean
structure and summaries through named inputs. Members may relax the permitted
substrate atoms independently. Sharing a basis does not freeze the whole slab.
Different adsorbates/sites/orientations are separate nodes when requested.
Do not chain intermediates in chemical reaction order unless a calculation
actually consumes the preceding state's output. Gas references run independently.

## Input Bindings

`inputs` maps a role to `node_id/artifact_key`; each binding creates a dependency.
Use `depends_on` only for additional ordering/evidence dependencies, without
repeating input-source edges. Example adsorbate node:

```json
{
  "node_id": "oh_slab_opt",
  "inputs": {
    "structure": "clean_slab_opt/final_structure",
    "modeling_reference": "clean_slab_opt/modeling_summary",
    "parameter_reference": "clean_slab_opt/parameter_summary"
  },
  "depends_on": []
}
```

For SCF/DOS, bind the corresponding state's `final_structure` and required
`final_chgcar`, `final_wavecar`, or `final_outcar` explicitly. Bind
`parameter_reference` only for the same-stage comparable surface family;
cross-stage parameter transfer uses the upstream summary as context and must
apply the destination task template. All optimized nodes publish
`final_structure`, even if the physical run uses a recovery directory.

## Task Content

Each node includes node_id, title, goal, workspace_subdir, task_md_content,
and optional inputs/depends_on. task.md contains one `task_type: ...` line,
objective, structure provenance, expected artifacts, and completion criteria.
Do not include VASP settings, queue resources, invented geometry or thresholds,
thermodynamic formulas, or reporting tasks. Supervisor chooses scientific scope;
specialized workers choose structures and parameters.

## Templates and Expansion

Use assets/manifest.yaml and the matching template for supported reaction
families. references/reaction-decomposition.md covers reference selection;
references/dag-few-shot-examples.md illustrates boundaries.
At scheduler quiescence, append only missing required states/references. Never
merge, rename, or replace existing nodes, or add unrelated exploratory work.
Current rules override historical memory examples that merge surface families.
