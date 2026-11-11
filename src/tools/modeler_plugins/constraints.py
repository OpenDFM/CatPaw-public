import numpy as np
from pymatgen.core import Structure


def fix_layers_by_element(
    input_poscar: str, fix_ratio: float = 0.5, tolerance: float = 0.1, output_poscar: str = "POSCAR_fixed"
) -> str:
    """
    Layer atoms independently by element type and fix the bottom layers of each element by a specified ratio.
    Suitable for multi-element slab systems (e.g., TiO2, perovskites).
    """
    try:
        struct = Structure.from_file(input_poscar)

        # 1) Initialize Selective Dynamics (default: all unconstrained T T T).
        sel_dyn = [[True, True, True] for _ in range(len(struct))]

        # 2) Group atom indices by element.
        element_indices = {}
        for i, site in enumerate(struct):
            el = site.species_string
            if el not in element_indices:
                element_indices[el] = []
            element_indices[el].append(i)

        report_lines = []

        # 3) Layer and fix atoms independently for each element.
        for el, indices in element_indices.items():
            z_coords = [struct[i].coords[2] for i in indices]
            sorted_local_idx = np.argsort(z_coords)
            sorted_global_idx = [indices[i] for i in sorted_local_idx]

            layers = []
            if not sorted_global_idx:
                continue

            current_layer = [sorted_global_idx[0]]
            current_z = struct[sorted_global_idx[0]].coords[2]

            for idx in sorted_global_idx[1:]:
                z = struct[idx].coords[2]
                if abs(z - current_z) <= tolerance:
                    current_layer.append(idx)
                else:
                    layers.append(current_layer)
                    current_layer = [idx]
                    current_z = z
            layers.append(current_layer)

            # 4) Compute number of layers to fix for this element.
            total_layers = len(layers)
            layers_to_fix = int(round(total_layers * fix_ratio))

            # 5) Set selected atoms to fixed dynamics (F F F).
            for i in range(layers_to_fix):
                for atom_idx in layers[i]:
                    sel_dyn[atom_idx] = [False, False, False]

            report_lines.append(
                f"  - Element {el}: detected {total_layers} layer(s); fixed bottom {layers_to_fix} layer(s)"
            )

        # 6) Write properties and sort by element to keep VASP-compatible POSCAR ordering.
        struct.add_site_property("selective_dynamics", sel_dyn)
        struct.sort()
        struct.to(fmt="poscar", filename=output_poscar)

        report_str = "\n".join(report_lines)
        return f"Success: bottom layers were fixed by element.\n{report_str}\nStructure saved to {output_poscar}"

    except Exception as e:
        return f"Error: element-wise layer fixing failed. Cause: {str(e)}"
