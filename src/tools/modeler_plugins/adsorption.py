import urllib.parse

import numpy as np
import requests
from pymatgen.analysis.adsorption import AdsorbateSiteFinder
from pymatgen.core import Molecule, Structure
from pymatgen.core.surface import center_slab as pmg_center_slab
from rdkit import Chem
from rdkit.Chem import AllChem

MIN_ADSORBATE_SLAB_CLEARANCE_A = 1.0
MIN_INITIAL_HEIGHT_ABOVE_TOP_A = 1.0
REJECT_BELOW_TOP_BY_A = 0.6
REJECT_MAX_DISPLACEMENT_A = 2.5
REJECT_MAX_LATERAL_DISPLACEMENT_A = 1.0
SLAB_MATCH_TOLERANCE_A = 0.35
FRAC_Z_UPPER_LIMIT = 0.90
FRAC_Z_LOWER_LIMIT = 0.10
SINGLE_ATOM_ADSORBATES = {"H", "O"}


def safe_center_slab(struct: Structure) -> Structure:
    """Wrapper around pymatgen center_slab that handles large supercells.

    pymatgen's center_slab uses a hardcoded 10 Angstrom neighbor search radius.
    For large supercells (lattice vectors > 10 A) with big vacuum layers, the
    first atom may have no neighbors within that radius, causing an IndexError
    in ``bond_dists[0]``.  In that case we fall back to the uncentered structure,
    which is usually already acceptable for adsorption site finding.
    """
    try:
        return pmg_center_slab(struct)
    except (IndexError, ValueError):
        return struct


def get_smiles_from_name(chemical_name: str) -> str:
    """Convert a common chemical name into SMILES using the PubChem API."""
    encoded_name = urllib.parse.quote(chemical_name)
    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{encoded_name}/property/IsomericSMILES/JSON"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()["PropertyTable"]["Properties"][0]["IsomericSMILES"]
    except Exception as e:
        print(f"PubChem API request failed: {e}")
    return None


def robust_get_mol(adsorbate_input: str):
    """Robust molecule parser: first treat input as SMILES; on failure, query PubChem by name."""
    # Branch 1: assume the input is a SMILES string.
    try:
        mol = Chem.MolFromSmiles(adsorbate_input)
        if mol is not None:
            mol = Chem.AddHs(mol)
            return mol, adsorbate_input
    except Exception:
        pass  # Parsing failure is acceptable; proceed to branch 2.

    # Branch 2: assume the input is a common name and query PubChem for SMILES.
    fetched_smiles = get_smiles_from_name(adsorbate_input)
    if fetched_smiles:
        try:
            mol = Chem.MolFromSmiles(fetched_smiles)
            if mol is not None:
                mol = Chem.AddHs(mol)
                return mol, fetched_smiles
        except Exception:
            pass

    return None, None


def analyze_adsorbate_footprint(adsorbate_input: str) -> dict:
    """Estimate adsorbate geometric footprint to guide ModelingWorker-side vacuum and supercell planning."""
    simple_adsorbates_coords = {
        "H": np.array([[0.0, 0.0, 0.0]]),
        "O": np.array([[0.0, 0.0, 0.0]]),
        "OH": np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.98]]),
        "CO": np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.13]]),
        "OOH": np.array([[0.0, 0.0, 0.0], [1.1, 0.8, 0.5], [1.8, 1.3, -0.1]]),
        "N2": np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.10]]),
    }
    single_atom_diameters = {
        # Use compact covalent-diameter-like values for single-atom adsorbates.
        # The previous hardcoded 2.0 A pushed small species like H into overly large
        # lateral cells (for example Pt(100) -> 3x3), which is too conservative.
        "H": 0.62,
        "O": 1.32,
    }

    if adsorbate_input in simple_adsorbates_coords:
        coords = simple_adsorbates_coords[adsorbate_input]
    else:
        # Use robust_get_mol for both SMILES and name-based lookup.
        mol_rd, actual_smiles = robust_get_mol(adsorbate_input)
        if mol_rd is None:
            return {
                "error": f"Unable to parse '{adsorbate_input}' as a valid molecule. Please check spelling or provide a standard SMILES."
            }

        embed_status = AllChem.EmbedMolecule(mol_rd, AllChem.ETKDG())
        if embed_status == -1:
            return {"error": f"RDKit failed to generate a physically plausible 3D conformer for '{adsorbate_input}'."}

        try:
            AllChem.UFFOptimizeMolecule(mol_rd, maxIters=500)
        except Exception:
            pass

        conf = mol_rd.GetConformer()
        coords = np.array([conf.GetAtomPosition(i) for i in range(mol_rd.GetNumAtoms())])

    num_atoms = len(coords)
    if num_atoms > 1:
        diff = coords[:, np.newaxis, :] - coords[np.newaxis, :, :]
        distances = np.linalg.norm(diff, axis=-1)
        max_diameter = np.max(distances)
    else:
        max_diameter = single_atom_diameters.get(adsorbate_input, 1.5)

    safe_vacuum = max(15.0, max_diameter + 10.0)
    required_lateral_size = max(6.5, max_diameter + 6.0)

    return {
        "adsorbate_input": adsorbate_input,
        "max_diameter_A": round(float(max_diameter), 2),
        "recommended_vacuum_size_A": round(float(safe_vacuum), 2),
        "required_lateral_size_A": round(float(required_lateral_size), 2),
        "reasoning": (
            f"The estimated maximum molecular diameter is {max_diameter:.2f} Å.\n"
            f"1. To suppress spurious z-direction interactions, recommended vacuum_size is at least {safe_vacuum:.2f} Å.\n"
            f"2. To avoid lateral collisions in the xy plane, ensure supercell a and b lattice vectors are each >= {required_lateral_size:.2f} Å."
        ),
    }


def build_result(status: str, message: str, **extra) -> dict:
    payload = {
        "status": status,
        "message": message,
    }
    payload.update(extra)
    return payload


def serialize_vector(vec) -> list[float]:
    return [round(float(x), 4) for x in np.asarray(vec)]


def serialize_sites_map(sites: dict) -> dict[str, list[list[float]]]:
    serialized: dict[str, list[list[float]]] = {}
    for key in ("ontop", "bridge", "hollow"):
        coords = sites.get(key, [])
        serialized[key] = [serialize_vector(coord) for coord in coords]
    return serialized


def wrap_structure_to_unit_cell(struct: Structure) -> Structure:
    wrapped = struct.copy()
    wrapped.translate_sites(list(range(len(wrapped))), [0.0, 0.0, 0.0], to_unit_cell=True)
    return wrapped


def write_adsorption_poscar(struct: Structure, output_poscar: str) -> None:
    """Group species without detaching coordinates or constraints from their atoms."""
    species_order = list(dict.fromkeys(site.species_string for site in struct))
    grouped = struct.copy()
    grouped.sort(key=lambda site: species_order.index(site.species_string))
    grouped.to(fmt="poscar", filename=output_poscar)


def adsorption_basis(input_poscar: str, substrate_poscar: str | None) -> tuple[Structure, Structure]:
    """Keep the input frame; find sites only on the explicitly identified substrate."""
    from pymatgen.io.vasp import Poscar

    from src.tools.modeling.validation import match_sites_by_species

    current = Poscar.from_file(input_poscar, check_for_potcar=False).structure
    if substrate_poscar is None:
        return current, current
    reference = Poscar.from_file(substrate_poscar, check_for_potcar=False).structure
    if not np.allclose(current.lattice.matrix, reference.lattice.matrix, atol=1e-6, rtol=0):
        raise ValueError("Substrate reference and input must have the same lattice.")
    pairs, missing, _ = match_sites_by_species(reference, current, SLAB_MATCH_TOLERANCE_A)
    if missing:
        raise ValueError("Substrate reference does not match input atoms; preserve the shared substrate frame.")
    return current, Structure.from_sites([current[j] for _, j in pairs])


def add_adsorbate_to_input(asf, current, molecule, target_pos):
    """Preserve existing coadsorbates while placing on substrate-only sites."""
    placed = asf.add_adsorbate(molecule, target_pos, translate=False)
    merged = current.copy()
    for site in placed[len(asf.slab) :]:
        properties = {key: value for key, value in site.properties.items() if key in current.site_properties}
        merged.append(site.specie, site.coords, coords_are_cartesian=True, properties=properties)
    return merged


def identify_adsorbate_indices(
    final_struct: Structure, slab_struct: Structure, expected_ads_count: int
) -> tuple[list[int], str]:
    if expected_ads_count <= 0:
        return [], "none"

    unmatched_final = set(range(len(final_struct)))
    species_to_final: dict[str, list[int]] = {}
    for idx, site in enumerate(final_struct):
        species_to_final.setdefault(site.species_string, []).append(idx)

    for slab_site in slab_struct:
        candidates = [idx for idx in species_to_final.get(slab_site.species_string, []) if idx in unmatched_final]
        if not candidates:
            continue

        best_idx = None
        best_dist = float("inf")
        for idx in candidates:
            dist = float(final_struct[idx].distance(slab_site))
            if dist < best_dist:
                best_dist = dist
                best_idx = idx

        if best_idx is not None and best_dist <= SLAB_MATCH_TOLERANCE_A:
            unmatched_final.remove(best_idx)

    adsorbate_indices = sorted(unmatched_final)
    if len(adsorbate_indices) == expected_ads_count:
        return adsorbate_indices, "geometry_match"
    return list(range(len(final_struct) - expected_ads_count, len(final_struct))), "tail_fallback"


def max_adsorbate_displacement(initial_ads_sites, final_ads_sites) -> float:
    if not initial_ads_sites or not final_ads_sites:
        return 0.0

    remaining = list(range(len(final_ads_sites)))
    max_disp = 0.0
    for init_site in initial_ads_sites:
        same_species = [idx for idx in remaining if final_ads_sites[idx].species_string == init_site.species_string]
        candidates = same_species or remaining
        best_idx = min(candidates, key=lambda idx: init_site.distance(final_ads_sites[idx]))
        max_disp = max(max_disp, float(init_site.distance(final_ads_sites[best_idx])))
        remaining.remove(best_idx)
        if not remaining:
            break
    return max_disp


def max_lateral_displacement(initial_ads_sites, final_ads_sites) -> float:
    """Compute maximum lateral (xy-plane) displacement of adsorbate atoms."""
    if not initial_ads_sites or not final_ads_sites:
        return 0.0

    remaining = list(range(len(final_ads_sites)))
    max_lat = 0.0
    for init_site in initial_ads_sites:
        same_species = [idx for idx in remaining if final_ads_sites[idx].species_string == init_site.species_string]
        candidates = same_species or remaining
        best_idx = min(candidates, key=lambda idx: init_site.distance(final_ads_sites[idx]))
        init_xy = init_site.coords[:2]
        final_xy = final_ads_sites[best_idx].coords[:2]
        lat_disp = float(np.linalg.norm(final_xy - init_xy))
        max_lat = max(max_lat, lat_disp)
        remaining.remove(best_idx)
        if not remaining:
            break
    return max_lat


def collect_geometry_metrics(
    initial_struct: Structure,
    candidate_struct: Structure,
    slab_struct: Structure,
    expected_ads_count: int,
    surface_struct: Structure | None = None,
) -> dict:
    surface_struct = slab_struct if surface_struct is None else surface_struct
    slab_top_z = max(site.coords[2] for site in surface_struct)
    slab_bottom_z = min(site.coords[2] for site in surface_struct)
    slab_mid_z = (slab_top_z + slab_bottom_z) / 2.0

    candidate_ads_indices, detection_method = identify_adsorbate_indices(
        candidate_struct, slab_struct, expected_ads_count
    )
    candidate_ads_sites = [candidate_struct[idx] for idx in candidate_ads_indices]

    initial_ads_indices = list(range(len(initial_struct) - expected_ads_count, len(initial_struct)))
    initial_ads_sites = [initial_struct[idx] for idx in initial_ads_indices]

    ads_z = [site.coords[2] for site in candidate_ads_sites]
    ads_min_z = min(ads_z) if ads_z else slab_top_z
    ads_max_z = max(ads_z) if ads_z else slab_top_z

    min_ads_slab_distance = float("inf")
    for ads_site in candidate_ads_sites:
        for slab_site in slab_struct:
            min_ads_slab_distance = min(
                min_ads_slab_distance,
                float(ads_site.distance(slab_site)),
            )

    if min_ads_slab_distance == float("inf"):
        min_ads_slab_distance = 0.0

    frac_z = candidate_struct.frac_coords[candidate_ads_indices, 2] if candidate_ads_indices else np.array([])
    max_disp = max_adsorbate_displacement(initial_ads_sites, candidate_ads_sites)
    max_lat_disp = max_lateral_displacement(initial_ads_sites, candidate_ads_sites)

    return {
        "slab_top_z_A": round(float(slab_top_z), 4),
        "slab_mid_z_A": round(float(slab_mid_z), 4),
        "adsorbate_min_z_A": round(float(ads_min_z), 4),
        "adsorbate_max_z_A": round(float(ads_max_z), 4),
        "min_adsorbate_slab_distance_A": round(float(min_ads_slab_distance), 4),
        "max_adsorbate_displacement_A": round(float(max_disp), 4),
        "max_lateral_displacement_A": round(float(max_lat_disp), 4),
        "adsorbate_detection": detection_method,
        "raw_fractional_z_range": [
            round(float(np.min(frac_z)), 4) if frac_z.size else None,
            round(float(np.max(frac_z)), 4) if frac_z.size else None,
        ],
    }


def apply_initial_clearance_guard(
    merged_struct: Structure,
    slab_struct: Structure,
    expected_ads_count: int,
    surface_struct: Structure | None = None,
) -> tuple[Structure, float, dict]:
    surface_struct = slab_struct if surface_struct is None else surface_struct
    adjusted = merged_struct.copy()
    ads_indices = list(range(len(adjusted) - expected_ads_count, len(adjusted)))
    ads_sites = [adjusted[idx] for idx in ads_indices]

    slab_top_z = max(site.coords[2] for site in surface_struct)
    ads_min_z = min(site.coords[2] for site in ads_sites)

    min_dist = float("inf")
    for ads_site in ads_sites:
        for slab_site in slab_struct:
            min_dist = min(min_dist, float(ads_site.distance(slab_site)))
    if min_dist == float("inf"):
        min_dist = 0.0

    clearance_shift = max(
        0.0,
        MIN_ADSORBATE_SLAB_CLEARANCE_A - min_dist + 0.15,
        MIN_INITIAL_HEIGHT_ABOVE_TOP_A - (ads_min_z - slab_top_z),
    )
    if clearance_shift > 0:
        adjusted.translate_sites(ads_indices, [0.0, 0.0, clearance_shift], frac_coords=False)

    adjusted_metrics = collect_geometry_metrics(adjusted, adjusted, slab_struct, expected_ads_count, surface_struct)
    return adjusted, round(float(clearance_shift), 4), adjusted_metrics


def evaluate_relaxed_candidate(metrics: dict) -> list[str]:
    reasons: list[str] = []
    if metrics["adsorbate_min_z_A"] < metrics["slab_mid_z_A"]:
        reasons.append("Adsorbate moved below slab mid-plane")
    if metrics["adsorbate_min_z_A"] < metrics["slab_top_z_A"] - REJECT_BELOW_TOP_BY_A:
        reasons.append("Adsorbate is significantly below slab top, suggesting embedding")
    if metrics["min_adsorbate_slab_distance_A"] < 0.75:
        reasons.append("Minimum adsorbate-slab distance is too short, suggesting atomic overlap")
    if metrics["max_adsorbate_displacement_A"] > REJECT_MAX_DISPLACEMENT_A:
        reasons.append("Pre-relaxation caused excessive adsorbate displacement, suggesting runaway behavior")
    if metrics.get("max_lateral_displacement_A", 0.0) > REJECT_MAX_LATERAL_DISPLACEMENT_A:
        reasons.append(
            "Pre-relaxation caused excessive lateral displacement, suggesting migration to a neighboring site"
        )
    frac_range = metrics.get("raw_fractional_z_range", [None, None])
    if frac_range[1] is not None and frac_range[1] > FRAC_Z_UPPER_LIMIT:
        reasons.append(
            f"Adsorbate frac_z upper bound {frac_range[1]:.4f} > {FRAC_Z_UPPER_LIMIT}, approaching periodic boundary"
        )
    if frac_range[0] is not None and frac_range[0] < FRAC_Z_LOWER_LIMIT:
        reasons.append(f"Adsorbate frac_z lower bound {frac_range[0]:.4f} < {FRAC_Z_LOWER_LIMIT}, near cell bottom")
    return reasons


def run_guarded_relaxation(
    initial_struct: Structure,
    slab_struct: Structure,
    *,
    expected_ads_count: int,
    num_slab_atoms: int,
    output_poscar: str,
    steps: int,
    surface_struct: Structure | None = None,
) -> dict:
    from ase.constraints import FixAtoms
    from chgnet.model.dynamics import StructOptimizer
    from pymatgen.io.ase import AseAtomsAdaptor

    adjusted_initial, clearance_shift, initial_metrics = apply_initial_clearance_guard(
        initial_struct,
        slab_struct,
        expected_ads_count,
        surface_struct,
    )

    fallback_struct = wrap_structure_to_unit_cell(adjusted_initial)
    fallback_struct = restore_selective_dynamics(slab_struct, fallback_struct, num_slab_atoms)

    # frac_z precheck: out-of-range at initial placement indicates fundamental placement issues; skip CHGNet.
    pre_frac_range = initial_metrics.get("raw_fractional_z_range", [None, None])
    if (pre_frac_range[1] is not None and pre_frac_range[1] > FRAC_Z_UPPER_LIMIT) or (
        pre_frac_range[0] is not None and pre_frac_range[0] < FRAC_Z_LOWER_LIMIT
    ):
        write_adsorption_poscar(fallback_struct, output_poscar)
        return build_result(
            "guarded_fallback",
            (
                "Success (guarded fallback): initial adsorption geometry was saved and CHGNet pre-relaxation was skipped.\n"
                f"  - Reason: adsorbate frac_z range {pre_frac_range} is out of safe bounds "
                f"({FRAC_Z_LOWER_LIMIT}~{FRAC_Z_UPPER_LIMIT}).\n"
                f"  - Structure saved to {output_poscar}."
            ),
            relaxation_attempted=False,
            relaxation_accepted=False,
            output_poscar=output_poscar,
            output_written=True,
            initial_clearance_shift_A=clearance_shift,
            geometry_metrics=initial_metrics,
            fallback_reason=f"Initial frac_z out-of-range: {pre_frac_range}",
            periodic_wrap_applied=True,
        )

    atoms = AseAtomsAdaptor.get_atoms(adjusted_initial)
    atoms.set_constraint(FixAtoms(indices=list(range(num_slab_atoms))))

    optimizer = StructOptimizer()
    print(f"[CHGNet] Starting pre-relaxation (fmax=0.5, up to {steps} steps)...", flush=True)

    try:
        relax_result = optimizer.relax(
            atoms,
            fmax=0.5,
            steps=steps,
            relax_cell=False,
            verbose=True,
        )
        print("[CHGNet] Pre-relaxation completed.", flush=True)
        relaxed = relax_result["final_structure"]
        if isinstance(relaxed, Structure):
            candidate_struct = relaxed
        else:
            candidate_struct = AseAtomsAdaptor.get_structure(relaxed)
    except Exception as exc:
        write_adsorption_poscar(fallback_struct, output_poscar)
        return build_result(
            "guarded_fallback",
            (
                "Success (guarded fallback): initial adsorption geometry was saved; CHGNet pre-relaxation result was not adopted.\n"
                f"  - Reason: CHGNet pre-relaxation execution failed: {exc}\n"
                f"  - Structure saved to {output_poscar}."
            ),
            relaxation_attempted=True,
            relaxation_accepted=False,
            output_poscar=output_poscar,
            output_written=True,
            initial_clearance_shift_A=clearance_shift,
            geometry_metrics=initial_metrics,
            fallback_reason=f"CHGNet pre-relaxation execution failed: {exc}",
            periodic_wrap_applied=True,
        )

    max_lattice_drift = float(np.max(np.abs(candidate_struct.lattice.matrix - adjusted_initial.lattice.matrix)))
    if max_lattice_drift > 1e-8:
        write_adsorption_poscar(fallback_struct, output_poscar)
        return build_result(
            "guarded_fallback",
            (
                "Success (guarded fallback): initial adsorption geometry was retained; "
                "CHGNet result was not adopted.\n"
                "  - Reason: fixed-cell pre-relaxation changed the lattice vectors "
                f"(max drift {max_lattice_drift:.3e} A).\n"
                f"  - Structure saved to {output_poscar}."
            ),
            relaxation_attempted=True,
            relaxation_accepted=False,
            output_poscar=output_poscar,
            output_written=True,
            initial_clearance_shift_A=clearance_shift,
            geometry_metrics=initial_metrics,
            fallback_reason=(f"Fixed-cell pre-relaxation changed lattice vectors: max drift {max_lattice_drift:.3e} A"),
            periodic_wrap_applied=True,
        )

    candidate_metrics = collect_geometry_metrics(
        adjusted_initial,
        candidate_struct,
        slab_struct,
        expected_ads_count,
        surface_struct,
    )
    rejection_reasons = evaluate_relaxed_candidate(candidate_metrics)

    raw_frac_range = candidate_metrics.get("raw_fractional_z_range", [None, None])
    wrap_notice = raw_frac_range[0] is not None and (raw_frac_range[0] < 0.0 or raw_frac_range[1] > 1.0)

    if rejection_reasons:
        write_adsorption_poscar(fallback_struct, output_poscar)
        return build_result(
            "guarded_fallback",
            (
                "Success (guarded fallback): initial adsorption geometry was retained; CHGNet result was not adopted.\n"
                f"  - Reason: {'; '.join(rejection_reasons)}\n"
                f"  - Structure saved to {output_poscar}."
            ),
            relaxation_attempted=True,
            relaxation_accepted=False,
            output_poscar=output_poscar,
            output_written=True,
            initial_clearance_shift_A=clearance_shift,
            geometry_metrics=candidate_metrics,
            fallback_reason="; ".join(rejection_reasons),
            periodic_wrap_applied=True,
        )

    accepted_struct = wrap_structure_to_unit_cell(candidate_struct)
    accepted_struct = restore_selective_dynamics(slab_struct, accepted_struct, num_slab_atoms)
    write_adsorption_poscar(accepted_struct, output_poscar)

    return build_result(
        "success",
        (
            "Success: CHGNet pre-relaxation result passed adsorption safety guards and was written.\n"
            f"  - Structure saved to {output_poscar}."
        ),
        relaxation_attempted=True,
        relaxation_accepted=True,
        output_poscar=output_poscar,
        output_written=True,
        initial_clearance_shift_A=clearance_shift,
        geometry_metrics=candidate_metrics,
        periodic_wrap_applied=True,
        periodic_boundary_crossing_detected=wrap_notice,
    )


def restore_selective_dynamics(original_slab: Structure, final_struct: Structure, num_slab_atoms: int) -> Structure:
    """
    Restore selective dynamics after CHGNet relaxation:
    - Original slab atoms inherit selective_dynamics from original POSCAR.
    - Newly added adsorbate atoms default to [True, True, True].
    """
    original_sd = original_slab.site_properties.get("selective_dynamics")
    if original_sd is None:
        return final_struct

    new_sd = []
    for i in range(len(final_struct)):
        if i < num_slab_atoms:
            new_sd.append(original_sd[i])
        else:
            new_sd.append([True, True, True])
    final_struct.add_site_property("selective_dynamics", new_sd)
    return final_struct


def label_ontop_sites(asf: AdsorbateSiteFinder, ontop_coords: list) -> list[dict]:
    """Report substrate atom identity and periodic CrystalNN neighbors for ontop sites."""
    import warnings

    from pymatgen.analysis.local_env import CrystalNN

    cnn = CrystalNN()
    labels = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for coord in ontop_coords:
            frac = asf.slab.lattice.get_fractional_coords(coord)
            # Sites were requested at distance=0. Match in 3D with the lattice metric,
            # including periodic images, rather than guessing from fractional xy.
            distances = asf.slab.lattice.get_all_distances([frac], asf.slab.frac_coords)[0]
            index = int(np.argmin(distances))
            if distances[index] > 1e-5:
                raise ValueError("Ontop site does not match a substrate atom.")
            neighbors = cnn.get_nn_info(asf.slab, index)
            site = asf.slab[index]
            labels.append(
                {
                    "element": site.species_string,
                    "cn": len(neighbors),
                    "substrate_atom_index_1based": index + 1,
                    "fractional_coords": serialize_vector(site.frac_coords),
                    "neighbors": [
                        {
                            "atom_index_1based": int(n["site_index"]) + 1,
                            "element": n["site"].species_string,
                            "image": [int(v) for v in n["image"]],
                            "distance_A": round(float(np.linalg.norm(n["site"].coords - site.coords)), 4),
                        }
                        for n in neighbors
                    ],
                }
            )
    return labels


def filter_labeled_ontop(
    ontop_coords: list,
    ontop_labels: list[dict],
    target_element: str | None,
    target_cn: int | None,
) -> tuple[list, list[dict]]:
    """Return (filtered_coords, filtered_labels) for ontop sites matching criteria."""
    filtered_coords = []
    filtered_labels = []
    for coord, label in zip(ontop_coords, ontop_labels, strict=True):
        if target_element is not None and label["element"] != target_element:
            continue
        if target_cn is not None and label["cn"] != target_cn:
            continue
        filtered_coords.append(coord)
        filtered_labels.append(label)
    return filtered_coords, filtered_labels


def format_sites_summary(
    sites: dict, used_type: str = None, used_index: int = None, ontop_labels: list[dict] = None
) -> str:
    """Format adsorption-site summary and mark the currently used site."""
    lines = []
    for stype in ("ontop", "bridge", "hollow"):
        coords_list = sites.get(stype, [])
        if not coords_list:
            continue
        header = f"  {stype} ({len(coords_list)} total):"
        lines.append(header)
        for i, coord in enumerate(coords_list):
            marker = " <-- selected" if (stype == used_type and i == used_index) else ""
            label_str = ""
            if stype == "ontop" and ontop_labels and i < len(ontop_labels):
                lbl = ontop_labels[i]
                label_str = f" [{lbl['element']}, CN={lbl['cn']}, substrate atom={lbl['substrate_atom_index_1based']}]"
            lines.append(f"    [{i}] ({coord[0]:.2f}, {coord[1]:.2f}, {coord[2]:.2f}){label_str}{marker}")
    return "\n".join(lines)


def auto_place_adsorbate(
    slab_poscar: str,
    adsorbate_input: str,
    site_type: str = "ontop",
    site_index: int = 0,
    height: float = 1.5,
    anchor_element: str = None,
    output_poscar: str = "POSCAR_adsorbed",
    target_element: str = None,
    target_cn: int = None,
    substrate_poscar: str | None = None,
) -> dict:
    """Unified smart-adsorption entry: auto-detect simple molecules vs. complex SMILES and dispatch to corresponding logic."""
    simple_adsorbates = {
        "H": Molecule(["H"], [[0.0, 0.0, 0.0]]),
        "O": Molecule(["O"], [[0.0, 0.0, 0.0]]),
        "OH": Molecule(["O", "H"], [[0.0, 0.0, 0.0], [0.0, 0.0, 0.98]]),
        "CO": Molecule(["C", "O"], [[0.0, 0.0, 0.0], [0.0, 0.0, 1.13]]),
        "OOH": Molecule(["O", "O", "H"], [[0.0, 0.0, 0.0], [1.1, 0.8, 0.5], [1.8, 1.3, -0.1]]),
        "N2": Molecule(["N", "N"], [[0.0, 0.0, 0.0], [0.0, 0.0, 1.10]]),
    }

    # Intercept homonuclear diatomic adsorption requests and guide to physically appropriate modeling strategy.
    diatomic_dissociative = {
        "H2": ("H", 2, "H2 tends to adsorb dissociatively on most transition-metal surfaces"),
        "N2": None,  # N2 molecular adsorption can be valid (e.g., first NRR step), so do not block.
        "O2": ("O", 2, "O2 tends to adsorb dissociatively on most transition-metal surfaces"),
    }
    if adsorbate_input in diatomic_dissociative and diatomic_dissociative[adsorbate_input] is not None:
        atom, count, reason = diatomic_dissociative[adsorbate_input]
        return build_result(
            "needs_user_action",
            f"Note: {reason}; it should not be placed as a single intact molecule.\n"
            f"Recommended workflow: call this tool {count} times, placing one '{atom}' atom per call, selecting different adsorption sites via site_type and site_index.\n"
            f"Example: first call with site_type='ontop', site_index=0; second call with site_type='ontop', site_index=1 (or a different site_type).\n"
            f"The first call returns all available sites and indices; use that list to choose the second-site index.",
            output_written=False,
        )

    try:
        if adsorbate_input in simple_adsorbates:
            slab_struct, surface_struct = adsorption_basis(slab_poscar, substrate_poscar)
            base_molecule = simple_adsorbates[adsorbate_input]
            finder_height = 1.5 if (target_element is not None or target_cn is not None) else 0.9
            asf = AdsorbateSiteFinder(surface_struct, height=finder_height)
            sites = asf.find_adsorption_sites(distance=0)

            # Compute element+CN labels for ontop sites (always, for informative output).
            ontop_labels = label_ontop_sites(asf, sites.get("ontop", []))

            # Apply target_element / target_cn filtering.
            filter_note = ""
            unfiltered_sites = None
            all_ontop_labels = list(ontop_labels)
            if target_element is not None or target_cn is not None:
                if site_type != "ontop":
                    parts = []
                    if target_element:
                        parts.append(f"target_element='{target_element}'")
                    if target_cn is not None:
                        parts.append(f"target_cn={target_cn}")
                    filter_note = f"Note: {', '.join(parts)} filtering only applies to site_type='ontop'; ignored for '{site_type}'.\n"
                else:
                    unfiltered_sites = dict(sites)
                    filtered_coords, filtered_labels = filter_labeled_ontop(
                        sites["ontop"],
                        ontop_labels,
                        target_element,
                        target_cn,
                    )
                    sites = dict(sites)
                    sites["ontop"] = filtered_coords
                    ontop_labels = filtered_labels

                    parts = []
                    if target_element:
                        parts.append(f"element={target_element}")
                    if target_cn is not None:
                        parts.append(f"CN={target_cn}")
                    filter_note = f"Ontop sites filtered by {', '.join(parts)}: {len(filtered_coords)}/{len(unfiltered_sites['ontop'])} retained.\n"

            if site_type not in sites or len(sites[site_type]) == 0:
                available = [k for k, v in sites.items() if len(v) > 0]
                show_sites = unfiltered_sites if unfiltered_sites else sites
                show_labels = all_ontop_labels if unfiltered_sites else ontop_labels
                return build_result(
                    "error",
                    f"Error: no '{site_type}' adsorption sites {'matched the filter' if unfiltered_sites else 'were found'}. "
                    f"Available types with sites: {available}.\n"
                    f"{filter_note}"
                    f"All adsorption sites on the current surface:\n{format_sites_summary(show_sites, ontop_labels=show_labels)}",
                    output_written=False,
                    available_sites=serialize_sites_map(show_sites),
                )

            if site_index >= len(sites[site_type]):
                show_sites = unfiltered_sites if unfiltered_sites else sites
                show_labels = all_ontop_labels if unfiltered_sites else ontop_labels
                return build_result(
                    "error",
                    f"Error: '{site_type}' has {len(sites[site_type])} site(s){' after filtering' if unfiltered_sites else ''}, "
                    f"but site_index={site_index} is out of range (valid: 0~{len(sites[site_type]) - 1}).\n"
                    f"{filter_note}"
                    f"Available adsorption sites on the current surface:\n{format_sites_summary(show_sites, ontop_labels=show_labels)}",
                    output_written=False,
                    available_sites=serialize_sites_map(show_sites),
                )

            target_site_coords = sites[site_type][site_index]
            target_pos = target_site_coords + np.array([0, 0, height])
            molecule = Molecule(base_molecule.species, base_molecule.cart_coords)
            merged_struct = add_adsorbate_to_input(asf, slab_struct, molecule, target_pos)
            chgnet_steps = 5 if adsorbate_input in SINGLE_ATOM_ADSORBATES else 15
            relax_result = run_guarded_relaxation(
                merged_struct,
                slab_struct,
                expected_ads_count=len(molecule),
                num_slab_atoms=len(slab_struct),
                output_poscar=output_poscar,
                steps=chgnet_steps,
                surface_struct=surface_struct,
            )
            sites_summary = format_sites_summary(
                sites, used_type=site_type, used_index=site_index, ontop_labels=ontop_labels
            )
            shift_note = ""
            if relax_result.get("initial_clearance_shift_A", 0.0) > 0:
                shift_note = f"\n  - Initial clearance was insufficient; adsorbate was lifted by {relax_result['initial_clearance_shift_A']:.2f} Å before pre-relaxation."
            mode_label = (
                "lightweight mode + pre-relaxation" if relax_result["status"] == "success" else "guarded fallback"
            )
            message = f"Success ({mode_label}): base molecule {adsorbate_input} was placed at site {site_type}[{site_index}].\n"
            if filter_note:
                message += f"  - {filter_note}"
            message += (
                f"  - Selective dynamics restored (slab atoms inherit original constraints; adsorbate atoms are free to relax).{shift_note}\n"
                f"  - Pre-relaxation acceptance: {'accepted' if relax_result.get('relaxation_accepted') else 'rejected'}."
            )
            if relax_result.get("fallback_reason"):
                message += f"\n  - Fallback reason: {relax_result['fallback_reason']}"
            message += (
                f"\n  - Structure saved to {output_poscar}.\n"
                f"  - All adsorption sites on current surface:\n{sites_summary}"
            )
            relax_result["message"] = message
            relax_result.update(
                {
                    "mode": "simple",
                    "adsorbate_input": adsorbate_input,
                    "site_type": site_type,
                    "site_index": site_index,
                    "selected_site_coords": serialize_vector(target_site_coords),
                    "available_sites": serialize_sites_map(sites),
                    "selected_site": ontop_labels[site_index] if site_type == "ontop" else None,
                    "substrate_poscar": substrate_poscar or slab_poscar,
                }
            )
            return relax_result

        else:
            if not anchor_element:
                return build_result(
                    "error",
                    f"Error: '{adsorbate_input}' is classified as a complex SMILES molecule. Please provide 'anchor_element' (e.g., 'O' or 'N') for anchoring.",
                    output_written=False,
                )

            mol_rd, actual_smiles = robust_get_mol(adsorbate_input)
            if mol_rd is None:
                return build_result(
                    "error",
                    f"Error: unable to parse '{adsorbate_input}'. It is neither valid SMILES nor resolvable in PubChem; please check spelling or provide a standard SMILES.",
                    output_written=False,
                )

            embed_status = AllChem.EmbedMolecule(mol_rd, AllChem.ETKDG())
            if embed_status == -1:
                return build_result(
                    "error",
                    f"Error: RDKit failed to generate a physically plausible 3D conformer for '{adsorbate_input}', so adsorption cannot proceed.",
                    output_written=False,
                )
            AllChem.UFFOptimizeMolecule(mol_rd)

            conf = mol_rd.GetConformer()
            species = [atom.GetSymbol() for atom in mol_rd.GetAtoms()]
            coords = np.array([conf.GetAtomPosition(i) for i in range(mol_rd.GetNumAtoms())])

            anchor_idx = next((i for i, sym in enumerate(species) if sym == anchor_element), None)
            if anchor_idx is None:
                return build_result(
                    "error",
                    f"Error: anchor element '{anchor_element}' was not found in molecule '{adsorbate_input}'.",
                    output_written=False,
                )

            if len(species) > 1:
                anchor_pos = coords[anchor_idx]
                coords = coords - anchor_pos
                other_indices = [i for i in range(len(species)) if i != anchor_idx]
                cog = np.mean(coords[other_indices], axis=0)
                vec_mol = cog
                norm_vec = np.linalg.norm(vec_mol)

                if norm_vec > 1e-4:
                    vec_mol = vec_mol / norm_vec
                    target_vec = np.array([0.0, 0.0, 1.0])
                    dot_prod = np.clip(np.dot(vec_mol, target_vec), -1.0, 1.0)
                    angle = np.arccos(dot_prod)

                    if abs(angle) > 1e-4 and abs(angle - np.pi) > 1e-4:
                        axis = np.cross(vec_mol, target_vec)
                        axis = axis / np.linalg.norm(axis)
                        K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
                        R = np.eye(3) + np.sin(angle) * K + (1 - np.cos(angle)) * np.dot(K, K)
                        coords = np.dot(coords, R.T)
                    elif abs(angle - np.pi) <= 1e-4:
                        R = np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1]])
                        coords = np.dot(coords, R.T)

            pmg_mol = Molecule(species, coords)
            slab_struct, surface_struct = adsorption_basis(slab_poscar, substrate_poscar)
            finder_height = 1.5 if (target_element is not None or target_cn is not None) else 0.9
            asf = AdsorbateSiteFinder(surface_struct, height=finder_height)
            sites = asf.find_adsorption_sites(distance=0)

            ontop_labels = label_ontop_sites(asf, sites.get("ontop", []))

            filter_note = ""
            unfiltered_sites = None
            all_ontop_labels = list(ontop_labels)
            if target_element is not None or target_cn is not None:
                if site_type != "ontop":
                    parts = []
                    if target_element:
                        parts.append(f"target_element='{target_element}'")
                    if target_cn is not None:
                        parts.append(f"target_cn={target_cn}")
                    filter_note = f"Note: {', '.join(parts)} filtering only applies to site_type='ontop'; ignored for '{site_type}'.\n"
                else:
                    unfiltered_sites = dict(sites)
                    filtered_coords, filtered_labels = filter_labeled_ontop(
                        sites["ontop"],
                        ontop_labels,
                        target_element,
                        target_cn,
                    )
                    sites = dict(sites)
                    sites["ontop"] = filtered_coords
                    ontop_labels = filtered_labels

                    parts = []
                    if target_element:
                        parts.append(f"element={target_element}")
                    if target_cn is not None:
                        parts.append(f"CN={target_cn}")
                    filter_note = f"Ontop sites filtered by {', '.join(parts)}: {len(filtered_coords)}/{len(unfiltered_sites['ontop'])} retained.\n"

            if site_type not in sites or len(sites[site_type]) == 0:
                available = [k for k, v in sites.items() if len(v) > 0]
                show_sites = unfiltered_sites if unfiltered_sites else sites
                show_labels = all_ontop_labels if unfiltered_sites else ontop_labels
                return build_result(
                    "error",
                    f"Error: no '{site_type}' adsorption sites {'matched the filter' if unfiltered_sites else 'were found'}. "
                    f"Available types with sites: {available}.\n"
                    f"{filter_note}"
                    f"All adsorption sites on the current surface:\n{format_sites_summary(show_sites, ontop_labels=show_labels)}",
                    output_written=False,
                    available_sites=serialize_sites_map(show_sites),
                )

            if site_index >= len(sites[site_type]):
                show_sites = unfiltered_sites if unfiltered_sites else sites
                show_labels = all_ontop_labels if unfiltered_sites else ontop_labels
                return build_result(
                    "error",
                    f"Error: '{site_type}' has {len(sites[site_type])} site(s){' after filtering' if unfiltered_sites else ''}, "
                    f"but site_index={site_index} is out of range (valid: 0~{len(sites[site_type]) - 1}).\n"
                    f"{filter_note}"
                    f"Available adsorption sites on the current surface:\n{format_sites_summary(show_sites, ontop_labels=show_labels)}",
                    output_written=False,
                    available_sites=serialize_sites_map(show_sites),
                )

            target_site = sites[site_type][site_index]

            target_pos = target_site + np.array([0, 0, height])

            initial_merged_struct = add_adsorbate_to_input(asf, slab_struct, pmg_mol, target_pos)
            relax_result = run_guarded_relaxation(
                initial_merged_struct,
                slab_struct,
                expected_ads_count=len(pmg_mol),
                num_slab_atoms=len(slab_struct),
                output_poscar=output_poscar,
                steps=10,
                surface_struct=surface_struct,
            )

            sites_summary = format_sites_summary(
                sites, used_type=site_type, used_index=site_index, ontop_labels=ontop_labels
            )
            shift_note = ""
            if relax_result.get("initial_clearance_shift_A", 0.0) > 0:
                shift_note = f"\n  - Initial clearance was insufficient; adsorbate was lifted by {relax_result['initial_clearance_shift_A']:.2f} Å before pre-relaxation."
            mode_label = "ML mode" if relax_result["status"] == "success" else "ML mode (guarded fallback)"
            message = (
                f"Success ({mode_label}): complex molecule '{adsorbate_input}' (SMILES: {actual_smiles}) completed collision-avoiding orientation and attempted adsorption at site {site_type}[{site_index}].\n"
                f"  - Anchor element: {anchor_element}\n"
            )
            if filter_note:
                message += f"  - {filter_note}"
            message += (
                f"  - Selective dynamics restored (slab atoms inherit original constraints; adsorbate atoms are free to relax).{shift_note}\n"
                f"  - Pre-relaxation acceptance: {'accepted' if relax_result.get('relaxation_accepted') else 'rejected'}."
            )
            if relax_result.get("fallback_reason"):
                message += f"\n  - Fallback reason: {relax_result['fallback_reason']}"
            message += (
                f"\n  - Structure saved to {output_poscar}.\n"
                f"  - All adsorption sites on current surface:\n{sites_summary}"
            )
            relax_result["message"] = message
            relax_result.update(
                {
                    "mode": "complex",
                    "adsorbate_input": adsorbate_input,
                    "actual_smiles": actual_smiles,
                    "anchor_element": anchor_element,
                    "site_type": site_type,
                    "site_index": site_index,
                    "selected_site_coords": serialize_vector(target_site),
                    "available_sites": serialize_sites_map(sites),
                    "selected_site": ontop_labels[site_index] if site_type == "ontop" else None,
                    "substrate_poscar": substrate_poscar or slab_poscar,
                }
            )
            return relax_result

    except Exception as e:
        return build_result(
            "error",
            f"Error: auto_place_adsorbate execution failed. Cause: {str(e)}",
            output_written=False,
        )
