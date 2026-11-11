# Reaction DAG Decomposition

## Template Lookup

Read `src/skills/dag-orchestration/assets/manifest.yaml` to list templates. If a template matches, read its YAML file and use its `methodology` section to choose DAG nodes, dependencies, node boundaries, references, and high-level task context. Treat `worked_example` as illustrative only; do not copy its chemistry, node names, or wording unless they match the user request.

If no template matches, decompose manually from calculation chemistry knowledge and note in `task.md` that the decomposition is manual.

If the main ambiguity is node boundary selection rather than template choice, also read `src/skills/dag-orchestration/references/dag-few-shot-examples.md` and follow its grouping patterns.

## Node Mapping

Decompose reaction-profile objectives into the minimum set of calculation
evidence nodes needed for the requested states and references.

- Use clean_slab_opt as the shared basis, then independent nodes for requested
  adsorbed states. Follow SKILL.md for named input bindings.
- Include independent molecular references required by stoichiometry and the
  selected electrochemical convention. Do not make them wait for clean slab.
- Use `scf` or `dos_calc` only when the requested evidence requires those
  electronic stages. Surface intermediate geometry optimization remains
  `slab_opt`; molecular reference optimization remains `mol_opt`.

## Analysis Boundary

Do not create thermodynamic-analysis, free-energy, limiting-potential,
reaction-profile, or report DAG nodes. Cross-node energy algebra, potential
dependence, conservation checks, and final narrative synthesis happen after
calculation evidence is complete.

During expansion, append only missing calculation references or missing surface
states from the original objective. Do not append a separate analysis node to
repair an incomplete reaction profile.

## Bulk Precursor Rule

Insert a `bulk_opt` node before surface nodes when:

- a slab will be cleaved from a downloaded bulk structure;
- DFT+U, a non-default functional, or user-specified settings may make Materials Project geometry inconsistent with the run;
- the user asks for comparable final energies across a surface pathway.

Skip `bulk_opt` only when:

- the user supplied a pre-optimized structure or CONTCAR and asked to use it;
- the task is gas-phase only;
- the user explicitly requests a direct slab test from a known structure.

## task.md Content

Each node task should include:

- title and local calculation goal;
- structure source: user input, upstream artifact, local library, or cloud lookup; keep this to provenance and do not inject empirical constants or guessed geometry details;
- dependency preconditions by `node_id`;
- template-derived intermediates or reference molecules if relevant;
- expected artifacts and completion criteria, written qualitatively unless the user explicitly provided numeric targets.

Do not include concrete VASP settings such as ENCUT, KPOINTS, LDAU, or queue resources.
Do not invent convergence thresholds or recommended structure parameters in `task.md`.

## Parameter Consistency

Supervisor does not choose parameters. For downstream waves that need comparable energies, rely on upstream artifact files and runner-provided node context rather than restating parameter guidance inside `task.md`.

Shared structure and parameter references enforce comparability across nodes.
Do not merge states to compensate for missing provenance.
