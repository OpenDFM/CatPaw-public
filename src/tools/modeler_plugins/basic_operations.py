import numpy as np
from pymatgen.core import Lattice, Structure
from pymatgen.core.surface import SlabGenerator
from pymatgen.core.surface import center_slab as pmg_center_slab


def get_bulk_coordination(bulk_struct):
    """Compute coordination number for each site in the bulk structure using CrystalNN."""
    from pymatgen.analysis.local_env import CrystalNN

    cnn = CrystalNN()
    coord_info = {}
    for i, site in enumerate(bulk_struct):
        try:
            nn_info = cnn.get_nn_info(bulk_struct, i)
            coord_info[i] = {
                "element": site.species_string,
                "coord_num": len(nn_info),
                "neighbors": sorted(set(n["site"].species_string for n in nn_info)),
            }
        except Exception:
            coord_info[i] = {
                "element": site.species_string,
                "coord_num": -1,
                "neighbors": [],
            }
    return coord_info


def get_surface_layer_sites(slab, z_threshold=0.5):
    """Return lists of (site_index, species_string) for top-layer and bottom-layer atoms."""
    z_coords = np.array([site.coords[2] for site in slab])
    z_max, z_min = z_coords.max(), z_coords.min()
    top_sites = [(i, slab[i].species_string) for i in range(len(slab)) if z_max - z_coords[i] <= z_threshold]
    bot_sites = [(i, slab[i].species_string) for i in range(len(slab)) if z_coords[i] - z_min <= z_threshold]
    return top_sites, bot_sites


def map_slab_site_to_bulk_coord(slab_site, bulk_struct, bulk_coord_info):
    """Find the closest matching bulk site for a slab atom and return its bulk coordination."""
    elem = slab_site.species_string
    best_cn = None
    best_neighbors = []
    for info in bulk_coord_info.values():
        if info["element"] == elem:
            best_cn = info["coord_num"]
            best_neighbors = info["neighbors"]
            break
    return best_cn, best_neighbors


def get_slab_coordination(slab, site_indices):
    """Compute actual coordination numbers for specific sites in a slab using CrystalNN."""
    from pymatgen.analysis.local_env import CrystalNN

    cnn = CrystalNN()
    result = {}
    for i in site_indices:
        try:
            nn_info = cnn.get_nn_info(slab, i)
            result[i] = {
                "coord_num": len(nn_info),
                "neighbors": sorted(set(n["site"].species_string for n in nn_info)),
            }
        except Exception:
            result[i] = {"coord_num": -1, "neighbors": []}
    return result


def analyze_slab_terminations(slabs, bulk_struct):
    """Analyze all candidate slabs: symmetry, polarity, and surface coordination environment."""
    bulk_coord_info = get_bulk_coordination(bulk_struct)

    bulk_coord_summary = {}
    for info in bulk_coord_info.values():
        elem = info["element"]
        if elem not in bulk_coord_summary:
            bulk_coord_summary[elem] = []
        bulk_coord_summary[elem].append(info)

    analyses = []
    for i, slab in enumerate(slabs):
        is_symmetric = slab.is_symmetric()
        is_polar = slab.is_polar()
        top_sites, bot_sites = get_surface_layer_sites(slab)

        all_surface_indices = list(set(idx for idx, _ in top_sites + bot_sites))
        slab_cn = get_slab_coordination(slab, all_surface_indices)

        def build_env(sites, current_slab=slab, current_slab_cn=slab_cn):
            env = {}
            for site_idx, elem in sites:
                if elem not in env:
                    bulk_cn, bulk_nn = map_slab_site_to_bulk_coord(
                        current_slab[site_idx],
                        bulk_struct,
                        bulk_coord_info,
                    )
                    env[elem] = {
                        "count": 0,
                        "bulk_coord_num": bulk_cn,
                        "bulk_neighbors": bulk_nn,
                        "slab_cns": [],
                    }
                env[elem]["count"] += 1
                env[elem]["slab_cns"].append(current_slab_cn[site_idx]["coord_num"])
            return env

        top_env = build_env(top_sites)
        bot_env = build_env(bot_sites)

        # Coordination saturation: average(slab_cn / bulk_cn) for all surface atoms.
        saturation_sum = 0.0
        saturation_count = 0
        for env in (top_env, bot_env):
            for info in env.values():
                bulk_cn = info["bulk_coord_num"]
                if bulk_cn and bulk_cn > 0:
                    for scn in info["slab_cns"]:
                        if scn >= 0:
                            saturation_sum += scn / bulk_cn
                            saturation_count += 1
        avg_saturation = saturation_sum / saturation_count if saturation_count > 0 else 0.0

        analyses.append(
            {
                "index": i,
                "slab": slab,
                "is_symmetric": is_symmetric,
                "is_polar": is_polar,
                "top_env": top_env,
                "bot_env": bot_env,
                "avg_saturation": avg_saturation,
            }
        )

    return analyses, bulk_coord_summary, bulk_coord_info


def format_env(env):
    """Format a surface environment dict into a readable string."""
    parts = []
    for elem, info in sorted(env.items()):
        bulk_cn = info["bulk_coord_num"]
        nn = info["bulk_neighbors"]
        n = info["count"]
        nn_str = ",".join(nn) if nn else "?"
        slab_cns = info.get("slab_cns", [])
        unique_scns = sorted(set(slab_cns))
        if unique_scns:
            scn_str = "/".join(str(c) for c in unique_scns)
            parts.append(f"{n}×{elem}(slab CN={scn_str}, bulk CN={bulk_cn}, neighbors=[{nn_str}])")
        else:
            parts.append(f"{n}×{elem}(bulk CN={bulk_cn}, neighbors=[{nn_str}])")
    return ", ".join(parts) if parts else "N/A"


def analyze_final_surface(slab, z_layer_map, bulk_coord_info, n_layers=3):
    """Analyze top and bottom surface layers of the final orthogonalized slab.

    Reports per-layer element, count, slab CN, and bulk CN for the outermost n_layers.
    """
    from pymatgen.analysis.local_env import CrystalNN

    cnn = CrystalNN()

    # Build site-to-layer mapping.
    site_layer = {}
    for layer_idx, (z_ref, _) in enumerate(z_layer_map):
        for i, site in enumerate(slab):
            if abs(site.coords[2] - z_ref) <= 0.1:
                site_layer[i] = layer_idx

    n_total = len(z_layer_map)
    top_layer_indices = list(range(n_total - 1, max(n_total - 1 - n_layers, -1), -1))
    bot_layer_indices = list(range(0, min(n_layers, n_total)))

    def describe_layers(layer_indices, label_order="top-down"):
        lines = []
        ordered = layer_indices if label_order == "top-down" else list(reversed(layer_indices))
        for rank, layer_idx in enumerate(ordered):
            z_ref, elems = z_layer_map[layer_idx]
            sites_in_layer = [i for i, current_layer in site_layer.items() if current_layer == layer_idx]
            elem_info = {}
            for site_idx in sites_in_layer:
                elem = slab[site_idx].species_string
                try:
                    nn_info = cnn.get_nn_info(slab, site_idx)
                    scn = len(nn_info)
                except Exception:
                    scn = -1
                if elem not in elem_info:
                    bulk_cn = None
                    for info in bulk_coord_info.values():
                        if info["element"] == elem:
                            bulk_cn = info["coord_num"]
                            break
                    elem_info[elem] = {"count": 0, "slab_cns": [], "bulk_cn": bulk_cn}
                elem_info[elem]["count"] += 1
                elem_info[elem]["slab_cns"].append(scn)

            parts = []
            for elem in sorted(elem_info):
                info = elem_info[elem]
                unique_cns = sorted(set(info["slab_cns"]))
                scn_str = "/".join(str(c) for c in unique_cns)
                parts.append(f"{info['count']}×{elem}(slab CN={scn_str}, bulk CN={info['bulk_cn']})")
            layer_label = f"layer {rank + 1}" if label_order == "top-down" else f"layer {rank + 1}"
            lines.append(f"        {layer_label}: {', '.join(parts)}  (z={z_ref:.2f} Å)")
        return lines

    top_lines = describe_layers(top_layer_indices, "top-down")
    bot_lines = describe_layers(bot_layer_indices, "bottom-up")
    return top_lines, bot_lines


def orthogonalize_and_center_slab(slab):
    """Return the final slab geometry used for layer analysis and output."""
    slab = slab.get_orthogonal_c_slab()
    try:
        slab = pmg_center_slab(slab)
    except (IndexError, ValueError):
        pass
    return slab


def analyze_slab_layers(slab):
    """Count z-layers and infer the shortest clean repeating unit."""
    z_layer_map = []
    for site in slab:
        z = site.coords[2]
        elem = site.species_string
        placed = False
        for entry in z_layer_map:
            if abs(z - entry[0]) <= 0.1:
                entry[1].append(elem)
                placed = True
                break
        if not placed:
            z_layer_map.append([z, [elem]])
    z_layer_map.sort(key=lambda x: x[0])
    actual_atomic_layers = len(z_layer_map)
    actual_thickness = z_layer_map[-1][0] - z_layer_map[0][0]

    layer_signatures = []
    for _, elems in z_layer_map:
        elem_counts = {}
        for elem in elems:
            elem_counts[elem] = elem_counts.get(elem, 0) + 1
        parts = []
        for elem in sorted(elem_counts):
            count = elem_counts[elem]
            parts.append(f"{count}{elem}" if count > 1 else elem)
        layer_signatures.append("+".join(parts))

    repeat_unit = None
    repeat_count = None
    for period in range(1, actual_atomic_layers // 2 + 1):
        if actual_atomic_layers % period != 0:
            continue
        candidate = layer_signatures[:period]
        if layer_signatures == candidate * (actual_atomic_layers // period):
            repeat_unit = candidate
            repeat_count = actual_atomic_layers // period
            break

    return {
        "z_layer_map": z_layer_map,
        "actual_atomic_layers": actual_atomic_layers,
        "actual_thickness": actual_thickness,
        "layer_signatures": layer_signatures,
        "repeat_unit": repeat_unit,
        "repeat_count": repeat_count,
    }


def cleave_surface(
    input_bulk_poscar: str,
    miller_index: list,
    num_layers: int = 4,
    vacuum_size: float = 15.0,
    termination_element: str = None,
    output_poscar: str = "POSCAR_slab",
    max_repeat_units: int = 6,
    fail_repeat_units: int = 8,
) -> str:
    """
    Cleave a slab from bulk using the specified Miller index, with intelligent termination selection.

    Enumerates all candidate terminations and selects the best one based on:
    symmetry, polarity, and optionally a preferred termination element.
    Reports coordination environment of surface atoms from the bulk reference.
    vacuum_size is the minimum total gap between neighboring periodic slabs,
    not the vacuum padding on each side of the centered slab.
    """
    try:
        bulk_struct = Structure.from_file(input_bulk_poscar)

        d_hkl = bulk_struct.lattice.d_hkl(miller_index)
        target_slab_size = (num_layers * d_hkl) + 0.1

        generator = SlabGenerator(
            initial_structure=bulk_struct,
            miller_index=miller_index,
            min_slab_size=target_slab_size,
            min_vacuum_size=vacuum_size,
            center_slab=False,
        )
        slabs = generator.get_slabs()

        if not slabs:
            return f"Error: unable to generate a slab along direction {miller_index}."

        analyses, bulk_coord_summary, bulk_coord_info = analyze_slab_terminations(slabs, bulk_struct)

        # Build bulk coordination reference string.
        bulk_ref_parts = []
        for elem, infos in sorted(bulk_coord_summary.items()):
            seen = set()
            for info in infos:
                key = (info["coord_num"], tuple(info["neighbors"]))
                if key not in seen:
                    seen.add(key)
                    nn_str = ",".join(info["neighbors"]) if info["neighbors"] else "?"
                    bulk_ref_parts.append(f"{elem}: CN={info['coord_num']}, neighbors=[{nn_str}]")
        bulk_ref_str = "; ".join(bulk_ref_parts)

        for a in analyses:
            final_slab = orthogonalize_and_center_slab(a["slab"])
            layer_info = analyze_slab_layers(final_slab)
            a["final_slab"] = final_slab
            a["layer_info"] = layer_info

        # Build termination summary for all candidates.
        term_lines = []
        for a in analyses:
            sym_tag = "symmetric" if a["is_symmetric"] else "asymmetric"
            pol_tag = "polar" if a["is_polar"] else "non-polar"
            top_str = format_env(a["top_env"])
            bot_str = format_env(a["bot_env"])
            repeat_count = a["layer_info"]["repeat_count"]
            repeat_str = (
                f"{repeat_count} repeat units"
                if repeat_count is not None
                else f"{a['layer_info']['actual_atomic_layers']} atomic layers"
            )
            quality = "✓" if (a["is_symmetric"] and not a["is_polar"]) else ("~" if not a["is_polar"] else "✗")
            term_lines.append(
                f"    [{quality}] Candidate {a['index']}: {sym_tag}, {pol_tag}, {repeat_str}\n"
                f"        top: {top_str}\n"
                f"        bot: {bot_str}"
            )

        # Rank candidates: avoid over-thick slabs first, then prefer chemically good terminations.
        def rank(a):
            score = 0
            if a["is_symmetric"] and not a["is_polar"]:
                score = 2
            elif not a["is_polar"]:
                score = 1
            has_pref = 0
            if termination_element and termination_element in a["top_env"]:
                has_pref = 1
            repeat_count = a["layer_info"]["repeat_count"]
            is_fail_thick = repeat_count is not None and repeat_count > fail_repeat_units
            is_over_target = repeat_count is not None and repeat_count > max_repeat_units
            repeat_distance = abs(repeat_count - num_layers) if repeat_count is not None else 999
            # Higher coordination saturation = more stable surface.
            return (
                int(is_fail_thick),
                int(is_over_target),
                -score,
                -has_pref,
                repeat_distance,
                -a["avg_saturation"],
                a["index"],
            )

        ranked = sorted(analyses, key=rank)
        best = ranked[0]
        best_slab = best["final_slab"]
        layer_info = best["layer_info"]
        z_layer_map = layer_info["z_layer_map"]
        actual_atomic_layers = layer_info["actual_atomic_layers"]
        normal = np.cross(best_slab.lattice.matrix[0], best_slab.lattice.matrix[1])
        normal /= np.linalg.norm(normal)
        heights = best_slab.cart_coords @ normal
        actual_thickness = float(np.ptp(heights))
        cell_height = abs(float(best_slab.lattice.matrix[2] @ normal))
        actual_vacuum = cell_height - actual_thickness
        repeat_unit = layer_info["repeat_unit"]
        repeat_count = layer_info["repeat_count"]

        if repeat_count is not None and repeat_count > fail_repeat_units:
            return (
                f"Error: all candidate slabs exceed fail_repeat_units={fail_repeat_units}; "
                f"selected candidate has {repeat_count} repeat units ({actual_atomic_layers} atomic layers). "
                "No structure was written. Reduce num_layers (Miller-plane repeats, not atomic layers). "
                "Raise the limit only for an explicitly justified thickness-convergence task."
            )

        if repeat_unit:
            unit_str = " | ".join(repeat_unit)
            repeat_info = f"{repeat_count} repeating units of [{unit_str}] (recommended: 4–6 units)"
        else:
            repeat_info = f"{actual_atomic_layers} atomic layers (no clean repeating unit detected)"

        best_slab.to(fmt="poscar", filename=output_poscar)

        # Detailed surface analysis on the final orthogonalized slab.
        top_detail, bot_detail = analyze_final_surface(best_slab, z_layer_map, bulk_coord_info)

        final_sorted = sorted(best_slab, key=lambda site: site.coords[2], reverse=True)
        final_top_z = final_sorted[0].coords[2]
        final_top_elements = list(
            set(site.species_string for site in final_sorted if abs(final_top_z - site.coords[2]) <= 0.1)
        )
        total_atoms = len(best_slab)

        if repeat_count is not None:
            if repeat_count <= 3:
                layer_hint = "thin; top/bottom surface interactions may remain. Consider increasing num_layers."
            elif repeat_count <= 6:
                layer_hint = "moderate; suitable for most adsorption/reaction calculations."
            elif repeat_count <= 9:
                layer_hint = "thick; suitable for precise surface-energy/convergence studies."
            else:
                layer_hint = "very thick; reduce num_layers unless special accuracy is required."
        else:
            if actual_atomic_layers <= 6:
                layer_hint = "thin to moderate."
            else:
                layer_hint = "thick."

        selection_note = f"Candidate {best['index']}"
        if best["is_symmetric"] and not best["is_polar"]:
            selection_note += " (symmetric, non-polar — ideal)"
        elif not best["is_polar"]:
            selection_note += " (non-polar, asymmetric)"
        else:
            selection_note += " (WARNING: polar — no non-polar candidate available)"

        return (
            f"Success: slab model cleaved from bulk along {miller_index}.\n"
            f"  - Crystallographic multiplier: {num_layers} (d_hkl = {d_hkl:.2f} Å)\n"
            f"  - Slab structure: {repeat_info} -> {layer_hint}\n"
            f"  - Actual atomic-layer count: {actual_atomic_layers}\n"
            f"  - Total atom count: {total_atoms} (after supercell [M×N×1]: {total_atoms}×M×N)\n"
            f"  - Absolute slab thickness: {actual_thickness:.2f} Å\n"
            f"  - Cell height along surface normal: {cell_height:.2f} Å\n"
            f"  - Requested minimum periodic vacuum gap: {vacuum_size:.2f} Å\n"
            f"  - Actual periodic vacuum gap: {actual_vacuum:.2f} Å (cell height minus slab thickness)\n"
            "  - Vacuum convention: total gap between neighboring periodic slabs, not per-side padding; "
            "the bottom and top cell-boundary gaps of a centered slab add up to this gap.\n"
            f"  - Selected termination top-layer elements: {final_top_elements}\n"
            f"  - Selected: {selection_note}\n"
            f"  - Thickness guardrails: target={num_layers}, max={max_repeat_units}, fail={fail_repeat_units} repeating units\n"
            f"  - Structure centered and saved to {output_poscar}\n"
            f"  - Note: num_layers is a crystallographic repeat count (multiples of d_hkl), not direct atomic-layer count.\n"
            f"\n"
            f"  Bulk coordination reference: {bulk_ref_str}\n"
            f"\n"
            f"  Surface detail (top, outermost first):\n" + "\n".join(top_detail) + "\n"
            "  Surface detail (bottom, outermost first):\n" + "\n".join(bot_detail) + "\n"
            f"\n"
            f"  All candidate terminations ({len(analyses)} found):\n" + "\n".join(term_lines)
        )

    except Exception as e:
        return f"Error: surface cleavage failed. Cause: {str(e)}"


def create_supercell(input_poscar: str, scaling_matrix: list, output_poscar: str = "POSCAR_supercell") -> str:
    """Expand a POSCAR structure using the provided scaling matrix."""
    try:
        from pymatgen.core import Structure

        struct = Structure.from_file(input_poscar)
        struct.make_supercell(scaling_matrix)

        # Sort by element for robust VASP POSCAR formatting.
        struct.sort()

        struct.to(fmt="poscar", filename=output_poscar)

        return f"Success: structure expanded with matrix {scaling_matrix}, sorted by element, and saved to {output_poscar}."
    except Exception as e:
        return f"Error: supercell expansion failed. Cause: {str(e)}"


def add_vacuum_layer(input_poscar: str, vacuum_size: float, axis: int = 2, output_poscar: str = "POSCAR_vacuum") -> str:
    """
    Add a vacuum layer to a structure (typically for 2D materials or slab models).

    Args:
        input_poscar (str): Input POSCAR path.
        vacuum_size (float): Vacuum thickness to add (A).
        axis (int): Axis for vacuum insertion: 0=a, 1=b, 2=c (default).
        output_poscar (str): Output POSCAR path.

    Returns:
        str: Operation summary.
    """
    try:
        struct = Structure.from_file(input_poscar)

        # Keep cartesian positions fixed while changing lattice.
        cart_coords = struct.cart_coords
        species = struct.species

        lattice_matrix = struct.lattice.matrix.copy()
        vector_length = np.linalg.norm(lattice_matrix[axis])

        new_length = vector_length + vacuum_size
        lattice_matrix[axis] = lattice_matrix[axis] * (new_length / vector_length)

        new_struct = Structure(lattice_matrix, species, cart_coords, coords_are_cartesian=True)
        new_struct.to(fmt="poscar", filename=output_poscar)

        return f"Success: added {vacuum_size} Å vacuum along axis {axis}; saved to {output_poscar}."
    except Exception as e:
        return f"Error: failed to add vacuum layer. Cause: {str(e)}"


def center_slab(input_poscar: str, axis: int = 2, output_poscar: str = "POSCAR_centered") -> str:
    """
    Center a slab along the specified axis (default z) by locating the largest vacuum gap.

    This avoids layer-identification artifacts caused by atoms crossing periodic boundaries.

    Args:
        input_poscar (str): Input POSCAR path.
        axis (int): Axis to center along: 0=a, 1=b, 2=c (default).
        output_poscar (str): Output POSCAR path.

    Returns:
        str: Operation summary.
    """
    try:
        struct = Structure.from_file(input_poscar)

        # 1) Fractional coordinates along selected axis in periodic range [0, 1).
        frac_coords = struct.frac_coords[:, axis]
        frac_coords = frac_coords % 1.0

        # 2) Sort and compute neighboring gaps.
        sorted_coords = np.sort(frac_coords)
        gaps = np.diff(sorted_coords)

        # Include boundary-crossing gap.
        boundary_gap = 1.0 - sorted_coords[-1] + sorted_coords[0]
        gaps = np.append(gaps, boundary_gap)

        # 3) Locate largest gap and its center.
        max_gap_idx = np.argmax(gaps)

        if max_gap_idx == len(gaps) - 1:
            vacuum_center = (sorted_coords[-1] + boundary_gap / 2.0) % 1.0
        else:
            vacuum_center = sorted_coords[max_gap_idx] + gaps[max_gap_idx] / 2.0

        # 4) Shift so vacuum center moves to origin and slab body to ~0.5.
        shift_vector = [0.0, 0.0, 0.0]
        shift_vector[axis] = -vacuum_center
        struct.translate_sites(list(range(len(struct))), shift_vector, to_unit_cell=True)

        struct.to(fmt="poscar", filename=output_poscar)

        return f"Success: largest-gap centering applied along axis {axis}; saved to {output_poscar}."

    except Exception as e:
        return f"Error: slab centering failed. Cause: {str(e)}"


def apply_strain(input_poscar: str, strain_vector: list, output_poscar: str = "POSCAR_strained") -> str:
    """
    Apply lattice strain to a structure.

    Args:
        input_poscar (str): Input POSCAR path.
        strain_vector (list): Strain values for a, b, c.
            Example: [0.02, 0.02, 0.0] for 2% biaxial tensile strain in ab-plane;
                     [-0.01, 0.0, 0.0] for 1% compressive strain along a-axis.
        output_poscar (str): Output strained POSCAR path.
    """
    try:
        struct = Structure.from_file(input_poscar)

        if len(strain_vector) != 3:
            return "Error: strain_vector must contain exactly 3 values, e.g. [0.02, 0.02, 0.0]."

        lattice_matrix = struct.lattice.matrix.copy()

        # L_new = L_old * (1 + strain)
        for i in range(3):
            lattice_matrix[i] = lattice_matrix[i] * (1.0 + strain_vector[i])

        # Keep fractional coordinates unchanged.
        new_struct = Structure(lattice_matrix, struct.species, struct.frac_coords)

        if strain_vector[2] != 0.0:
            action_z = f"{strain_vector[2] * 100:.1f}%"
        else:
            action_z = "0% (unchanged)"

        new_struct.to(fmt="poscar", filename=output_poscar)

        return (
            f"Success: strain applied to structure.\n"
            f"  - a-axis strain: {strain_vector[0] * 100:.1f}%\n"
            f"  - b-axis strain: {strain_vector[1] * 100:.1f}%\n"
            f"  - c-axis strain: {action_z}\n"
            f"  - Structure saved to {output_poscar}. Consider setting ISIF=2 in INCAR for in-plane strain workflows."
        )

    except Exception as e:
        return f"Error: strain application failed. Cause: {str(e)}"


def build_result(status: str, message: str, **extra) -> dict:
    payload = {"status": status, "message": message}
    payload.update(extra)
    return payload


def build_molecule_in_box(molecule_input: str, box_size: float = 15.0, output_poscar: str = "POSCAR_molecule") -> dict:
    """
    Build an isolated molecular POSCAR model in a periodic cubic box for gas-phase reference calculations.

    Returns a structured dict (status / message / ...).
    """
    known_molecules = {
        "H2": {"species": ["H", "H"], "coords": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.74]]},
        "O2": {"species": ["O", "O"], "coords": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.21]]},
        "N2": {"species": ["N", "N"], "coords": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.10]]},
        "CO": {"species": ["C", "O"], "coords": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.13]]},
        "NO": {"species": ["N", "O"], "coords": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.15]]},
        "OH": {"species": ["O", "H"], "coords": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.98]]},
        "H2O": {"species": ["O", "H", "H"], "coords": [[0.0, 0.0, 0.0], [0.76, 0.59, 0.0], [-0.76, 0.59, 0.0]]},
        "CO2": {"species": ["C", "O", "O"], "coords": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.16], [0.0, 0.0, -1.16]]},
        "NH3": {
            "species": ["N", "H", "H", "H"],
            "coords": [[0.0, 0.0, 0.0], [0.94, 0.27, 0.0], [-0.47, 0.27, 0.82], [-0.47, 0.27, -0.82]],
        },
    }

    try:
        species = None
        coords = None

        if molecule_input in known_molecules:
            data = known_molecules[molecule_input]
            species = data["species"]
            coords = np.array(data["coords"])
        else:
            try:
                from rdkit.Chem import AllChem

                from src.tools.modeler_plugins.adsorption import robust_get_mol

                mol_rd, actual_smiles = robust_get_mol(molecule_input)
                if mol_rd is None:
                    return build_result(
                        "error",
                        f"Error: '{molecule_input}' could not be parsed as a valid molecule. Please check spelling or provide a standard SMILES.",
                        output_written=False,
                    )

                embed_status = AllChem.EmbedMolecule(mol_rd, AllChem.ETKDG())
                if embed_status == -1:
                    return build_result(
                        "error",
                        f"Error: RDKit could not generate a physically plausible 3D conformer for '{molecule_input}'.",
                        output_written=False,
                    )

                try:
                    AllChem.UFFOptimizeMolecule(mol_rd, maxIters=500)
                except Exception:
                    pass

                conf = mol_rd.GetConformer()
                species = [atom.GetSymbol() for atom in mol_rd.GetAtoms()]
                coords = np.array([conf.GetAtomPosition(i) for i in range(mol_rd.GetNumAtoms())])
            except ImportError:
                return build_result(
                    "error",
                    f"Error: '{molecule_input}' is not in the built-in molecule library, and RDKit is not installed for custom parsing.",
                    output_written=False,
                )

        center = np.array([box_size / 2.0] * 3)
        centroid = np.mean(coords, axis=0)
        coords = coords - centroid + center

        lattice = Lattice.from_parameters(box_size, box_size, box_size, 90, 90, 90)
        struct = Structure(lattice, species, coords, coords_are_cartesian=True)
        struct.sort()
        struct.to(fmt="poscar", filename=output_poscar)

        return build_result(
            "success",
            f"Success: molecule '{molecule_input}' was placed into a {box_size}×{box_size}×{box_size} Å cubic box.\n"
            f"  - Atom count: {len(species)}\n"
            f"  - Molecule centered; structure saved to {output_poscar}.\n"
            f"  - Tip: for calculation, consider ISMEAR=0 and ISYM=0 in INCAR.",
            output_written=True,
            atom_count=len(species),
        )
    except Exception as e:
        return build_result("error", f"Error: molecular modeling failed. Cause: {str(e)}", output_written=False)
