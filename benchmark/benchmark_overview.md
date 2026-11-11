# CatPaw Benchmark Overview

An end-to-end evaluation suite: starting from natural-language descriptions, it assesses
the full capability of the CatPaw multi-agent pipeline
(modeling → parameter setup → submission → monitoring → analysis).

A total of **9 categories and 72 cases** (8 per category), covering diverse material systems
and difficulty combinations. Each case's difficulty is characterized along three dimensions
and tagged with a triple `(M, P, W)`.

---

### Modeling Difficulty (M)

Describes the complexity of the modeling operations the agent must perform.

| Level | Meaning | Operation | Typical scenario |
|-------|---------|-----------|------------------|
| M1 | Ready-to-use structure | No extra modeling after download/generation | Bulk optimization, molecular energy |
| M2 | Basic modeling | Surface cleaving / supercell / fixing bottom layers | Surface relaxation |
| M3 | Advanced modeling | Adsorption / doping / defect | Adsorption energy, defect systems |

### Parameter Correction Burden (P)

Describes the number of material-specific VASP electronic-structure corrections required.

| Level | Meaning | Corrections | Typical scenario |
|-------|---------|-------------|------------------|
| P1 | No material-specific correction | 0 | Standard setup or zero-magnetization spin check |
| P2 | One material-specific correction | 1 | Nonzero spin/magnetic order, DFT+U, vdW, or special PAW/POTCAR selection |
| P3 | Multiple material-specific corrections | 2 or more | Nonzero spin/magnetic order combined with DFT+U |

### Workflow Complexity (W)

Describes the dependency topology among computational tasks.

| Level | Meaning | Topology | Typical scenario |
|-------|---------|----------|------------------|
| W1 | Single calculation | `A` | Bulk optimization, surface relaxation |
| W2 | Linear sequence | `A → B → C` | Relax → SCF → DOS |
| W3 | DAG (directed acyclic graph) | Parallel branches and merges | Adsorption energy (slab_opt ∥ mol_opt → Eads), reaction profiles |

---

## Benchmark Cases

Nine categories, 8 cases each. Every case lists its three-dimensional difficulty `(M, P, W)`
and the reference answer (taken from each answer file's `acceptance_criteria` value, with units).

### 1. Molecular Optimization (`mol_opt`)

| case_id | M | P | W | Reference answer |
|---------|---|---|---|------------------|
| H2_mol_opt | M1 | P1 | W1 | d(H–H) = 0.750 Å |
| H2O_mol_opt | M1 | P1 | W1 | d(O–H) = 0.972 Å |
| CO_mol_opt | M1 | P1 | W1 | d(C–O) = 1.143 Å |
| CO2_mol_opt | M1 | P1 | W1 | d(C–O) = 1.176 Å |
| NH3_mol_opt | M1 | P1 | W1 | d(N–H) = 1.022 Å |
| O2_mol_opt | M1 | P2 | W1 | d(O–O) = 1.233 Å |
| NO_mol_opt | M1 | P2 | W1 | d(N–O) = 1.169 Å |
| NO2_mol_opt | M1 | P2 | W1 | d(N–O) = 1.212 Å |

### 2. Bulk Optimization (`bulk_opt`)

| case_id | M | P | W | Reference answer |
|---------|---|---|---|------------------|
| Al_bulk_opt | M1 | P1 | W1 | E = -3.745 eV/f.u. |
| Si_bulk_opt | M1 | P1 | W1 | E = -5.423 eV/f.u. |
| GaN_bulk_opt | M1 | P1 | W1 | E = -12.317 eV/f.u. |
| graphite_bulk_opt | M1 | P2 | W1 | E = -9.22 eV/f.u. |
| TiN_bulk_opt | M1 | P2 | W1 | E = -19.663 eV/f.u. |
| Fe_bulk_opt | M1 | P2 | W1 | E = -8.47 eV/f.u. |
| Ni_bulk_opt | M1 | P2 | W1 | E = -5.78 eV/f.u. |
| FeS2_bulk_opt | M1 | P2 | W1 | E = -18.258 eV/f.u. |

### 3. Surface (Slab) Relaxation (`slab_opt`)

| case_id | M | P | W | Reference answer |
|---------|---|---|---|------------------|
| Cu_111_slab_opt | M2 | P1 | W2 | γ = 1.33 J/m² |
| Al_100_slab_opt | M2 | P1 | W2 | γ = 0.86 J/m² |
| MgO_100_slab_opt | M2 | P1 | W2 | γ = 1.02 J/m² |
| Mo2C_100_slab_opt | M2 | P1 | W2 | γ = 3.03 J/m² |
| Co_001_slab_opt | M2 | P2 | W2 | γ = 2.1 J/m² |
| MnO2_110_slab_opt | M2 | P3 | W2 | γ = 0.62 J/m² |
| TiO2_110_Ovac_slab_opt | M3 | P3 | W3 | E_vac = 3.7 eV |
| CeO2_111_Ovac_slab_opt | M3 | P3 | W3 | E_vac = 2.3 eV |

### 4. Bader Charge (`bader_charge`)

| case_id | M | P | W | Reference answer |
|---------|---|---|---|------------------|
| H2O_bader | M1 | P1 | W2 | O = -1.15 e; H = +0.58 e |
| MgO_bader | M1 | P1 | W2 | Mg = +1.70 e; O = -1.70 e |
| Fe2O3_bader | M1 | P3 | W2 | Fe = +1.72 e; O = -1.15 e |
| TiO2_bader | M1 | P1 | W2 | Ti = +2.52 e; O = -1.3 e |
| CaF2_bader | M1 | P1 | W2 | Ca = +1.4 e; F = -0.7 e |
| NiO_100_bader | M2 | P3 | W2 | bulk-like Ni = +1.27 e |
| CuO_111_bader | M2 | P3 | W2 | surface Cu = +0.5 e |
| Fe2O3_Sndoped_bader | M3 | P3 | W2 | Sn = +2.28 e |

### 5. Spin Density Localization (`spin_localization`)

| case_id | M | P | W | Reference answer |
|---------|---|---|---|------------------|
| NO_spin | M1 | P2 | W2 | total magmom = 1.0 μB |
| O3_spin | M1 | P1 | W2 | total magmom = 0.0 μB |
| BaTiO3_spin | M1 | P1 | W2 | total magmom = 0.0 μB |
| ZnO_100_spin | M2 | P1 | W2 | total magmom = 0.0 μB |
| V2O3_spin | M1 | P3 | W2 | per-V magmom = 1.7 μB |
| BaTiO3_Fedoped_spin | M3 | P3 | W2 | Fe magmom = 3.05 μB |
| ZnO_bulk_Cudoped_spin | M3 | P2 | W2 | Bulk ZnO: Cu magmom = 0.57 μB |
| MnO2_110_spin | M2 | P3 | W2 | surface Mn magmom = 3.75 μB |

### 6. Band Gap (`band_gap`)

| case_id | M | P | W | Reference answer |
|---------|---|---|---|------------------|
| Si_band_gap | M1 | P1 | W2 | E_gap = 0.61 eV |
| GaN_band_gap | M1 | P2 | W2 | E_gap = 1.71 eV |
| MoS2_band_gap | M1 | P2 | W2 | E_gap = 0.92 eV |
| Diamond_band_gap | M1 | P1 | W2 | E_gap = 4.1 eV |
| CdS_band_gap | M1 | P2 | W2 | E_gap = 2.4 eV |
| CdS_100_band_gap | M2 | P2 | W2 | E_gap = 2.23 eV |
| Cr2O3_band_gap | M1 | P3 | W2 | E_gap = 2.6 eV |
| CeO2_111_Mndoped_band_gap | M3 | P3 | W2 | E_gap = 0.31 eV |

### 7. d-Band Center (`d_band_center`)

| case_id | M | P | W | Reference answer |
|---------|---|---|---|------------------|
| Pd_bulk_dband | M1 | P1 | W2 | ε_d = -2.3 eV |
| Pt_111_dband | M2 | P1 | W2 | ε_d = -2.25 eV |
| Ag_111_dband | M2 | P1 | W2 | ε_d = -4.3 eV |
| Fe_110_dband | M2 | P2 | W2 | ε_d = -0.69 eV |
| RuO2_110_dband | M2 | P1 | W2 | ε_d = -1.76 eV |
| WC_001_dband | M2 | P1 | W2 | ε_d = -1.08 eV |
| Pt_at_Ni_111_dband | M3 | P2 | W2 | ε_d = -3.05 eV |
| Pt_overlayer_Cu_111_dband | M3 | P1 | W2 | ε_d = -3.4 eV |

### 8. Adsorption Energy (`adsorption_energy`)

| case_id | M | P | W | Reference answer |
|---------|---|---|---|------------------|
| CO_at_Pt_111_adsorption | M3 | P1 | W3 | Eads = -1.45 eV |
| NH3_at_Cu_111_adsorption | M3 | P1 | W3 | Eads = -0.46 eV |
| O_at_Rh_111_adsorption | M3 | P2 | W3 | Eads = -5.22 eV |
| H_at_Ni_111_adsorption | M3 | P2 | W3 | Eads = -2.89 eV |
| N2_at_Co_001_adsorption | M3 | P2 | W3 | Eads = -0.46 eV |
| OH_at_IrO2_110_adsorption | M3 | P3 | W3 | Eads = -0.23 eV |
| H2O_at_TiO2_110_adsorption | M3 | P2 | W3 | Eads = -0.87 eV |
| CH3OH_at_CeO2_111_adsorption | M3 | P2 | W3 | Eads = -0.69 eV |

### 9. Reaction Profile (`reaction_profile`)

| case_id | M | P | W | Reference answer |
|---------|---|---|---|------------------|
| IrO2_110_OER | M3 | P3 | W3 | ΔG_rds = 1.79 eV |
| Pt_111_ORR | M3 | P2 | W3 | ΔG_rds = 0.45 eV |
| Ag_111_CO2RR | M3 | P1 | W3 | ΔG_rds = 1.25 eV |
| Pt_111_CO_oxidation | M3 | P2 | W3 | ΔE_total = -3.41 eV |
| CeO2_111_MVK_CO | M3 | P3 | W3 | ΔE_total = -3.41 eV |
| Ni_111_CH4_dissociation | M3 | P2 | W3 | ΔE_total = 0.62 eV |
| Ru_001_NRR_distal | M3 | P1 | W3 | ΔG_rds = 1.08 eV |
| Cu_111_WGS_carboxyl | M3 | P1 | W3 | ΔE_total = -0.85 eV |
