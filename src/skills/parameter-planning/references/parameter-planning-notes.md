# Parameter Planning Notes

These notes cover cross-template decisions that are easy to misapply. Task-specific INCAR/KPOINTS/POTCAR baselines live in `../assets/calculation_templates/`; do not duplicate them here.

## DFT+U Screening

Call `DftPlusUEvaluator` when the structure may contain strongly correlated transition-metal or rare-earth chemistry.

- Do not add DFT+U only because a transition metal is present; metallic elements and alloys without anions usually do not need it.
- Oxides, sulfides, selenides, and oxyhydroxides containing transition metals or rare-earth elements should be evaluated for DFT+U.
- If DFT+U is enabled, record the rationale in `parameter_summary.json` and keep comparable reaction-family calculations consistent.

## Chained Electronic Calculations

For chained SCF, DOS, or charge-density calculations, parameter choices must preserve compatibility with the upstream calculation that produced the inherited electronic files.

- Use phase order:
  1. structural optimization;
  2. SCF from optimized `CONTCAR`;
  3. DOS or charge analysis from the SCF `CONTCAR` and `CHGCAR`.
- DOS must inherit `CHGCAR` from a converged SCF calculation, not directly from a relaxation.
- Copy or rename upstream propagated artifacts according to dependency context and record inherited files in `parameter_summary.json`.
- Do not change Hamiltonian-defining settings such as ENCUT, ISPIN, functional, or DFT+U settings between SCF and DOS unless the task explicitly requires a new electronic reference.
- Follow the task-type YAML for concrete `scf` and `dos_calc` INCAR/KPOINTS settings.
