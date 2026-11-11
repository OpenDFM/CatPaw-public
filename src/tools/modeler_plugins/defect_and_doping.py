import random

import numpy as np
from pymatgen.core import Structure


def build_result(status: str, message: str, **extra) -> dict:
    payload = {"status": status, "message": message}
    payload.update(extra)
    return payload


def modify_surface_defects(
    slab_poscar: str,
    defect_type: str,
    target_element: str,
    dopant_element: str = None,
    surface_side: str = "top",
    output_poscar: str = "POSCAR_defect",
    coordination_number: int = None,
) -> dict:
    """
    Automatically create vacancies or substitutional dopants on slab surfaces.

    Args:
        coordination_number: If provided (e.g., 2 for bridging O), CrystalNN is
            first used to filter target_element atoms by this coordination number,
            then top-layer selection is applied via surface_side + z tolerance.
    Returns a structured dict (status / message / ...).
    """
    try:
        struct = Structure.from_file(slab_poscar)

        # Find all indices and z-coordinates for target_element.
        target_sites = []
        for i, site in enumerate(struct):
            if site.species_string == target_element:
                target_sites.append((i, site.coords[2]))

        if not target_sites:
            return build_result(
                "error",
                f"Error: target element '{target_element}' was not found in the structure.",
                output_written=False,
            )

        # 1b) If coordination number is specified, prefilter by local environment.
        cn_info = None
        if coordination_number is not None:
            import warnings

            from pymatgen.analysis.local_env import CrystalNN

            nn = CrystalNN()
            filtered = []
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                for idx, z in target_sites:
                    try:
                        cn = nn.get_cn(struct, idx)
                    except Exception:
                        continue
                    if cn == coordination_number:
                        filtered.append((idx, z))
            if not filtered:
                return build_result(
                    "error",
                    f"Error: no '{target_element}' atom with coordination_number = {coordination_number} was found. "
                    f"Please verify slab termination or adjust coordination_number.",
                    output_written=False,
                )
            cn_info = (
                f"coordination_number = {coordination_number} "
                f"({len(filtered)}/{len(target_sites)} candidate(s) after filtering)"
            )
            target_sites = filtered

        # Identify top/bottom layer candidates using 0.1 A tolerance.
        target_sites.sort(key=lambda x: x[1])

        candidates = []
        if surface_side == "top":
            max_z = target_sites[-1][1]
            candidates = [site for site in target_sites if abs(site[1] - max_z) <= 0.1]
        elif surface_side == "bottom":
            min_z = target_sites[0][1]
            candidates = [site for site in target_sites if abs(site[1] - min_z) <= 0.1]
        else:
            return build_result(
                "error",
                f"Error: unsupported surface_side '{surface_side}'. Use 'top' or 'bottom'.",
                output_written=False,
            )

        # Randomly choose one atom from same-layer candidates.
        target_index, target_z = random.choice(candidates)
        cn_suffix = f", {cn_info}" if cn_info else ""
        original_site_info = f"{target_element} (Z = {target_z:.2f} Å{cn_suffix})"

        # Apply operation.
        if defect_type == "vacancy":
            struct.remove_sites([target_index])
            action_desc = f"Removed surface atom {original_site_info}, creating one vacancy"

        elif defect_type == "doping":
            if not dopant_element:
                return build_result(
                    "error",
                    "Error: 'dopant_element' must be provided for 'doping' operation.",
                    output_written=False,
                )
            if dopant_element == target_element:
                return build_result(
                    "error",
                    f"Error: dopant_element ('{dopant_element}') equals target_element; "
                    f"self-substitution has no physical meaning. Please check the modeler-tool input.",
                    output_written=False,
                )
            struct.replace(target_index, dopant_element)
            action_desc = f"Replaced surface atom {original_site_info} with {dopant_element}"

        else:
            return build_result(
                "error",
                f"Error: unknown defect_type '{defect_type}'.",
                output_written=False,
            )

        # Resort and save structure.
        struct.sort()
        struct.to(fmt="poscar", filename=output_poscar)

        return build_result(
            "success",
            f"Success: surface defect engineering completed.\n"
            f"  - Operation detail: {action_desc} (randomly selected from {len(candidates)} same-layer candidate(s)).\n"
            f"  - Structure was resorted by element and saved to {output_poscar}.",
            output_written=True,
            defect_type=defect_type,
            candidates_count=len(candidates),
        )

    except Exception as e:
        return build_result("error", f"Error: defect generation failed. Cause: {str(e)}", output_written=False)


def remove_atomic_layer(
    slab_poscar: str,
    target_element: str = None,
    surface_side: str = "top",
    tolerance: float = 0.1,
    remove_ratio: float = 1.0,
    distribution: str = "dispersed",
    output_poscar: str = "POSCAR_reconstructed",
) -> str:
    """
    Remove an entire or partial surface layer with physically motivated spatial distributions.

    Args:
        slab_poscar (str): Input slab POSCAR path.
        target_element (str, optional): Element type to remove (e.g., "O"). If omitted,
            target all atoms at outermost z on selected surface side.
        surface_side (str): Operation side, "top" or "bottom".
        tolerance (float): Z-height tolerance for same-layer atoms (A), default 0.1.
        remove_ratio (float): Removal ratio (0.0 < r <= 1.0), default 1.0.
        distribution (str): Vacancy distribution mode.
            - "random": purely random distribution.
            - "dispersed": farthest-point-sampling style isolated distribution.
            - "missing_row_x": missing-row reconstruction along X.
            - "missing_row_y": missing-row reconstruction along Y.
        output_poscar (str): Output reconstructed POSCAR path.
    """
    try:
        struct = Structure.from_file(slab_poscar)

        # Filter target atom set
        if target_element:
            candidate_indices = [i for i, site in enumerate(struct) if site.species_string == target_element]
            if not candidate_indices:
                return f"Error: element '{target_element}' is not present in the structure."
        else:
            candidate_indices = list(range(len(struct)))

        # Collect and sort candidate z coordinates
        z_coords = [(idx, struct[idx].coords[2]) for idx in candidate_indices]
        z_coords.sort(key=lambda x: x[1])

        # Determine reference z for target layer
        if surface_side == "top":
            reference_z = z_coords[-1][1]
        elif surface_side == "bottom":
            reference_z = z_coords[0][1]
        else:
            return f"Error: unsupported surface_side '{surface_side}'. Use 'top' or 'bottom'."

        # Cluster atoms in target layer
        layer_indices = []
        removed_elements = set()
        for idx, z in z_coords:
            if abs(z - reference_z) <= tolerance:
                layer_indices.append(idx)
                removed_elements.add(struct[idx].species_string)

        if not layer_indices:
            return "Error: no atoms meeting criteria were found at the target height."

        # Compute number of atoms to remove
        if remove_ratio <= 0.0 or remove_ratio > 1.0:
            return "Error: remove_ratio must be within (0.0, 1.0]."

        num_to_remove = int(round(len(layer_indices) * remove_ratio))

        if num_to_remove == 0:
            return (
                f"Note: target layer has {len(layer_indices)} atom(s); "
                f"remove_ratio={remove_ratio} yields 0 removals, so no action was taken."
            )

        # Core physically motivated distribution algorithms
        indices_to_remove = []

        if num_to_remove == len(layer_indices):
            indices_to_remove = layer_indices

        else:
            if distribution == "random":
                # Mode A: random distribution
                indices_to_remove = random.sample(layer_indices, num_to_remove)

            elif distribution == "dispersed":
                # Mode B: farthest-point sampling (electrostatic-repulsion inspired)
                remaining = layer_indices.copy()
                indices_to_remove = [remaining.pop(random.randrange(len(remaining)))]

                while len(indices_to_remove) < num_to_remove:
                    max_min_dist = -1
                    best_candidate = -1
                    for cand in remaining:
                        c_coord = struct[cand].coords
                        min_dist_to_selected = min(
                            [np.linalg.norm(c_coord - struct[s].coords) for s in indices_to_remove]
                        )

                        if min_dist_to_selected > max_min_dist:
                            max_min_dist = min_dist_to_selected
                            best_candidate = cand

                    indices_to_remove.append(best_candidate)
                    remaining.remove(best_candidate)

            elif distribution.startswith("missing_row"):
                # Mode C: missing-row reconstruction
                # Missing row along X => cluster rows by Y; vice versa.
                cluster_axis = 1 if distribution == "missing_row_x" else 0

                axis_coords = [(idx, struct[idx].coords[cluster_axis]) for idx in layer_indices]
                axis_coords.sort(key=lambda x: x[1])

                # 1D coordinate clustering (0.5 A row tolerance)
                rows = []
                current_row = [axis_coords[0][0]]
                current_val = axis_coords[0][1]

                for idx, val in axis_coords[1:]:
                    if abs(val - current_val) <= 0.5:
                        current_row.append(idx)
                    else:
                        rows.append(current_row)
                        current_row = [idx]
                        current_val = val
                rows.append(current_row)

                # Compute number of rows to remove
                rows_to_remove = int(round(len(rows) * remove_ratio))
                rows_to_remove = max(1, rows_to_remove)

                # Uniform row sampling (alternating missing-row-like pattern)
                step = len(rows) / rows_to_remove
                selected_row_indices = [int(i * step) for i in range(rows_to_remove)]

                for ri in selected_row_indices:
                    indices_to_remove.extend(rows[ri])
            else:
                return f"Error: unsupported distribution mode '{distribution}'."

        # Remove and save
        struct.remove_sites(indices_to_remove)

        # Resort for VASP POSCAR formatting compliance
        struct.sort()
        struct.to(fmt="poscar", filename=output_poscar)

        elem_info = f"elements {list(removed_elements)}" if not target_element else f"target element {target_element}"

        return (
            f"Success: surface reconstruction completed using '{distribution}' distribution mode.\n"
            f"  - Surface side: {surface_side} (reference Z = {reference_z:.2f} Å)\n"
            f"  - Target atoms: {elem_info}\n"
            f"  - Nominal removal ratio: {remove_ratio * 100:.1f}%\n"
            f"  - Target-layer atom count: {len(layer_indices)}; removed {len(indices_to_remove)} atom(s).\n"
            f"  - Structure saved to {output_poscar}."
        )

    except Exception as e:
        return f"Error: atomic-layer removal failed. Cause: {str(e)}"
