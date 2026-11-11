---
name: report-writing
description: >
  CatPaw Reporter skill. Use when Reporter synthesizes completed DAG evidence
  into final_report.md.
---

# Report Writing

## Scope

This skill belongs to Reporter only. It turns completed DAG node evidence into
one workspace-level `final_report.md`. When a workspace-level
`benchmark_result.json` file already exists, Reporter may also fill that file's
placeholders for benchmark evaluation. It does not inspect raw VASP
outputs, rerun calculations, update DAG state, dispatch agents, or modify
node-level handoff files.

## Required Report Structure

Write `final_report.md` with exactly these top-level sections:

```markdown
# Final Computational Report

## Direct answer to the user question

## Manuscript-style analysis

## Supporting information

## Evidence table
```

## Direct Answer

Open with one concise paragraph that answers the original research objective as
directly as the available evidence allows. If the evidence only supports part of
the requested conclusion, answer that supported part and state the missing
evidence needed for the rest. Do not delay the answer until later sections.

## Manuscript-Style Analysis

Write polished scientific prose, not a workflow log. Start from the computed
systems and the scientific question, report key numerical results with units,
compare related calculations when available, and connect conclusions to
structural, energetic, electronic, magnetic, charge, or DOS evidence. Clearly
separate direct evidence from interpretation.

## Demonstration-Grade Evidence

When any successful node records `analysis.quality_tier=demonstration`:

- label the overall result as workflow-demonstration or screening grade;
- distinguish strictly converged and demonstration-accepted states;
- list excluded competitors and their failure/non-convergence reasons;
- do not claim a publication-grade pathway ranking, mechanism, or catalyst
  superiority from incomplete candidate coverage;
- preserve the numerical limits and exclusions in the final evidence table.

Do not infer orbital-specific occupation, bonding character, oxidation states,
charge transfer, or catalytic mechanisms from total energy or total/site
magnetization alone. Make those claims only when supported by DOS/PDOS, Bader
charge, spin density, charge density, or equivalent analysis evidence. Otherwise
state the limitation explicitly.

## Supporting Information

Write compact methods-style paragraphs. Include VASP, PAW/POTCAR, exchange
correlation functional, cutoff energy, vdW correction, slab/vacuum/fixed-layer
setup, k-point mesh, SCF and force thresholds, spin polarization, DFT+U, DOS,
Bader, or related settings only when supported by the provided evidence. Group
shared settings once and describe node-specific differences only when supported.
Do not fill missing settings with generic VASP defaults.

For method-level wording, describe these calculations as VASP/PAW when the
evidence supports VASP inputs or outputs. Do not label the result as DNP,
Gaussian-basis, localized-basis, or another non-VASP method level unless the
listed evidence explicitly supports that label.

## Benchmark Result JSON

If the workspace root contains `benchmark_result.json`, update that existing
file after reading the same listed evidence used for `final_report.md`.

- Fill only existing benchmark result keys with evidence-supported JSON values.
- Use numbers for scalar metrics, strings only for requested labels such as an
  element symbol.
- Do not add keys, units, status fields, evidence fields, comments, arrays, or
  nested objects.
- Leave unsupported or ambiguous metrics as `null`.
- Prefer `node_result.json` evidence; use `runner_result.json` only as fallback
  for missing calculation-level values.

## Thermodynamic Quantities

When the completed node evidence contains the required total energies and
structural metadata, Reporter may evaluate standard cross-node thermodynamic
quantities requested by the user. Cite every input energy and structural
quantity in the evidence table.

- Surface energy: use `gamma = (E_slab - N * E_bulk/unit) / (2A)` for the
  standard two-equivalent-surface convention, and convert with
  `1 eV/A^2 = 16.0217 J/m^2`. If the evidence shows an asymmetric or otherwise
  nonstandard slab convention, state the limitation or the explicit convention
  instead of silently applying the standard formula.
- Adsorption energy: use `E_ads = E(slab+adsorbate) - E(clean_slab) -
  E(gas_adsorbate)` unless the user specified a different sign convention.
  Under this convention, negative values indicate exothermic adsorption.
- Vacancy formation energy: use `E_vac = E(defective) - E(clean) +
  n * mu_removed`, with the removed-species chemical potential taken only from
  explicit completed evidence, such as an O2 molecular reference or a bulk
  per-atom reference.
- OER AEM/CHE profiles: combine clean, OH, O, and OOH surface-state energies
  with the completed molecular references required by the selected convention.
  State the convention used and avoid reporting overpotentials when any required
  intermediate or reference is missing.

## Evidence Table

Provide a compact Markdown table with columns:

```markdown
| Claim or quantity | Value or statement | Source |
```

Every primary numerical result and every major interpretation in the report
should have a source entry pointing to the relevant node evidence file.
