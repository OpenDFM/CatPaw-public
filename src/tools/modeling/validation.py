import math
from collections import Counter
from collections.abc import Sequence
from itertools import product
from pathlib import Path
from typing import Any, ClassVar

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import Tool, ToolExecutor, register_tool
from pydantic import Field

NEIGHBOR_CHECK_RADIUS_A = 3.5
OVERLAP_DISTANCE_A = 0.6
SHORT_BOND_WARNING_A = 0.9
ADSORBATE_BELOW_TOP_WARNING_A = 1.0
SLAB_MATCH_TOLERANCE_A = 0.35
DEFAULT_LAYER_TOLERANCE_A = 0.1
GEOMETRY_MATCH_TOLERANCE_A = 0.35
MOLECULE_MIN_IMAGE_DISTANCE_A = 10.0
MOLECULE_TIGHT_IMAGE_DISTANCE_A = 12.0
MOLECULE_LARGE_IMAGE_DISTANCE_A = 20.0
DEFAULT_MAX_SLAB_REPEAT_UNITS = 5
DEFAULT_FAIL_SLAB_REPEAT_UNITS = 8
DEFAULT_MAX_INPLANE_ATOMS_PER_LAYER = 25
LIGHT_ELEMENTS = {"H", "C", "N", "O"}


def composition_counts(struct) -> Counter[str]:
    return Counter(site.species_string for site in struct)


def vector_norm(vector) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in vector))


def cross_norm(first, second) -> float:
    cross = (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )
    return vector_norm(cross)


def selective_dynamics_tags(struct) -> list[tuple[bool, bool, bool]] | None:
    tags = struct.site_properties.get("selective_dynamics")
    if tags is None:
        return None
    return [tuple(bool(v) for v in tag) for tag in tags]


def identify_adsorbate_sites(final_struct, slab_struct, expected_ads_count: int):
    if expected_ads_count <= 0:
        return [], "none"

    unmatched_final = set(range(len(final_struct)))
    species_to_final = {}
    for idx, site in enumerate(final_struct):
        species_to_final.setdefault(site.species_string, []).append(idx)

    for slab_site in slab_struct:
        candidates = [idx for idx in species_to_final.get(slab_site.species_string, []) if idx in unmatched_final]
        if not candidates:
            continue
        best_idx = min(candidates, key=lambda idx: float(final_struct[idx].distance(slab_site)))
        if float(final_struct[best_idx].distance(slab_site)) <= SLAB_MATCH_TOLERANCE_A:
            unmatched_final.remove(best_idx)

    adsorbate_indices = sorted(unmatched_final)
    if len(adsorbate_indices) == expected_ads_count:
        return [final_struct[idx] for idx in adsorbate_indices], "geometry_match"
    raise ValueError("Cannot identify adsorbates by species/position against the substrate reference.")


def cluster_z_layers(struct, layer_tolerance: float = DEFAULT_LAYER_TOLERANCE_A) -> dict[str, Any]:
    z_layer_map: list[list[Any]] = []
    for site in struct:
        z = float(site.coords[2])
        for entry in z_layer_map:
            if abs(z - entry[0]) <= layer_tolerance:
                entry[1].append(site.species_string)
                break
        else:
            z_layer_map.append([z, [site.species_string]])

    if not z_layer_map:
        return {
            "actual_atomic_layers": 0,
            "repeat_unit": None,
            "repeat_count": None,
            "slab_thickness_angstrom": 0.0,
            "vacuum_angstrom": None,
            "layer_signatures": [],
        }

    z_layer_map.sort(key=lambda item: item[0])
    layer_signatures = []
    for _, elems in z_layer_map:
        counts = Counter(elems)
        layer_signatures.append(
            "+".join(f"{counts[elem]}{elem}" if counts[elem] > 1 else elem for elem in sorted(counts))
        )

    repeat_unit = None
    repeat_count = None
    actual_layers = len(layer_signatures)
    for period in range(1, actual_layers // 2 + 1):
        if actual_layers % period != 0:
            continue
        candidate = layer_signatures[:period]
        if layer_signatures == candidate * (actual_layers // period):
            repeat_unit = candidate
            repeat_count = actual_layers // period
            break

    slab_thickness = float(z_layer_map[-1][0] - z_layer_map[0][0])
    c_length = float(struct.lattice.abc[2])
    return {
        "actual_atomic_layers": actual_layers,
        "repeat_unit": repeat_unit,
        "repeat_count": repeat_count,
        "slab_thickness_angstrom": round(slab_thickness, 3),
        "vacuum_angstrom": round(max(c_length - slab_thickness, 0.0), 3),
        "layer_signatures": layer_signatures,
    }


def primary_slab_species(struct) -> str | None:
    counts = composition_counts(struct)
    if not counts:
        return None
    heavy = {element: count for element, count in counts.items() if element not in LIGHT_ELEMENTS}
    source = heavy or counts
    return max(source.items(), key=lambda item: (item[1], item[0]))[0]


def cluster_species_layers(struct, species: str, layer_tolerance: float = 0.55) -> list[list[float]]:
    groups: list[list[float]] = []
    for site in sorted(
        (site for site in struct if site.species_string == species), key=lambda item: float(item.coords[2])
    ):
        z = float(site.coords[2])
        if not groups:
            groups.append([z])
            continue
        center = sum(groups[-1]) / len(groups[-1])
        if abs(z - center) <= layer_tolerance:
            groups[-1].append(z)
        else:
            groups.append([z])
    return groups


def slab_size_info(struct) -> dict[str, Any]:
    lattice = struct.lattice.matrix
    primary_species = primary_slab_species(struct)
    primary_layers = cluster_species_layers(struct, primary_species) if primary_species else []
    atoms_per_layer = [len(layer) for layer in primary_layers]
    return {
        "in_plane_a_A": round(vector_norm(lattice[0]), 3),
        "in_plane_b_A": round(vector_norm(lattice[1]), 3),
        "in_plane_area_A2": round(cross_norm(lattice[0], lattice[1]), 3),
        "primary_slab_species": primary_species,
        "primary_species_layers": len(primary_layers),
        "primary_species_atoms_per_layer": atoms_per_layer,
        "max_primary_species_atoms_per_layer": max(atoms_per_layer) if atoms_per_layer else 0,
    }


def lattice_mismatch(ref, other, length_tol: float, angle_tol: float) -> list[str]:
    mismatches = []
    for axis, ref_len, other_len in zip("abc", ref.lattice.abc, other.lattice.abc, strict=True):
        diff = abs(float(ref_len) - float(other_len))
        if diff > length_tol:
            mismatches.append(f"lattice {axis} differs by {diff:.3f} A")
    for axis, ref_ang, other_ang in zip(
        ("alpha", "beta", "gamma"), ref.lattice.angles, other.lattice.angles, strict=True
    ):
        diff = abs(float(ref_ang) - float(other_ang))
        if diff > angle_tol:
            mismatches.append(f"lattice {axis} differs by {diff:.3f} deg")
    return mismatches


def minimum_periodic_image_distance(struct) -> float | None:
    """Return the shortest atom-to-atom distance across neighboring periodic images."""
    if not struct:
        return None

    lattice_vectors = struct.lattice.matrix
    coords = [site.coords for site in struct]
    min_distance: float | None = None
    for shift in product((-1, 0, 1), repeat=3):
        if shift == (0, 0, 0):
            continue
        image_offset = sum((shift[i] * lattice_vectors[i] for i in range(3)), lattice_vectors[0] * 0)
        for coord_a in coords:
            for coord_b in coords:
                delta = coord_a - (coord_b + image_offset)
                distance = float((delta @ delta) ** 0.5)
                if min_distance is None or distance < min_distance:
                    min_distance = distance
    return min_distance


def match_sites_by_species(ref, other, coord_tol: float) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    unmatched_other = set(range(len(other)))
    pairs: list[tuple[int, int]] = []
    unmatched_ref: list[int] = []
    for ref_idx, ref_site in enumerate(ref):
        candidates = [
            other_idx for other_idx in unmatched_other if other[other_idx].species_string == ref_site.species_string
        ]
        if not candidates:
            unmatched_ref.append(ref_idx)
            continue
        best_idx = min(candidates, key=lambda idx: float(ref_site.distance(other[idx])))
        if float(ref_site.distance(other[best_idx])) <= coord_tol:
            pairs.append((ref_idx, best_idx))
            unmatched_other.remove(best_idx)
        else:
            unmatched_ref.append(ref_idx)
    return pairs, unmatched_ref, sorted(unmatched_other)


def match_sites_by_position(
    ref, other, ref_indices: list[int], other_indices: list[int], coord_tol: float
) -> list[tuple[int, int]]:
    unmatched_other = set(other_indices)
    pairs: list[tuple[int, int]] = []
    for ref_idx in ref_indices:
        if not unmatched_other:
            break
        best_idx = min(unmatched_other, key=lambda idx: float(ref[ref_idx].distance(other[idx])))
        if float(ref[ref_idx].distance(other[best_idx])) <= coord_tol:
            pairs.append((ref_idx, best_idx))
            unmatched_other.remove(best_idx)
    return pairs


def compare_selective_dynamics(ref, other, pairs: list[tuple[int, int]]) -> int:
    ref_tags = selective_dynamics_tags(ref)
    other_tags = selective_dynamics_tags(other)
    if ref_tags is None or other_tags is None:
        return 0
    return sum(1 for ref_idx, other_idx in pairs if ref_tags[ref_idx] != other_tags[other_idx])


def validate_structure(
    poscar_path: str,
    operation_type: str = "generic",
    pre_atom_count: int | None = None,
    slab_poscar: str | None = None,
    *,
    slab_structure=None,
) -> dict[str, Any]:
    """Run deterministic POSCAR format and basic physical checks."""
    from pymatgen.io.vasp import Poscar

    errors: list[str] = []
    warnings: list[str] = []
    info: dict[str, Any] = {}
    poscar = Path(poscar_path)

    if not poscar.exists():
        return {"passed": False, "errors": [f"Structure file does not exist: {poscar}"], "warnings": [], "info": {}}

    try:
        struct = Poscar.from_file(str(poscar)).structure
    except Exception as exc:
        return {"passed": False, "errors": [f"POSCAR parsing failed: {exc}"], "warnings": [], "info": {}}

    n_atoms = len(struct)
    info["formula"] = struct.composition.reduced_formula
    info["atom_count"] = n_atoms
    info["layer_info"] = cluster_z_layers(struct)

    sd = struct.site_properties.get("selective_dynamics")
    if sd is not None:
        if len(sd) != n_atoms:
            errors.append(f"Selective Dynamics tag count ({len(sd)}) does not match atom count ({n_atoms}).")
        else:
            malformed = [idx for idx, tag in enumerate(sd) if len(tag) != 3]
            if malformed:
                errors.append(f"Malformed Selective Dynamics tags at atom indices {malformed[:5]}.")

    if pre_atom_count is not None:
        delta = n_atoms - pre_atom_count
        info["atom_count_delta"] = delta
        if operation_type == "adsorption" and delta <= 0:
            errors.append(f"Adsorption should increase atom count (before: {pre_atom_count}, after: {n_atoms}).")
        elif operation_type == "vacancy" and delta >= 0:
            errors.append(f"Vacancy should decrease atom count (before: {pre_atom_count}, after: {n_atoms}).")
        elif operation_type == "doping" and delta != 0:
            warnings.append(
                f"Doping changed atom count (before: {pre_atom_count}, after: {n_atoms}); verify operation."
            )

    if n_atoms > 1:
        try:
            min_dist = min(
                nb.nn_distance for site in struct for nb in struct.get_neighbors(site, r=NEIGHBOR_CHECK_RADIUS_A)
            )
            info["min_bond_distance_A"] = round(float(min_dist), 3)
            if min_dist < OVERLAP_DISTANCE_A:
                errors.append(f"Atomic overlap detected: nearest-neighbor distance {min_dist:.3f} A.")
            elif min_dist < SHORT_BOND_WARNING_A:
                warnings.append(f"Nearest-neighbor distance is short ({min_dist:.3f} A); verify structure.")
        except ValueError:
            pass
        except Exception as exc:
            warnings.append(f"Nearest-neighbor calculation failed: {exc}")

    if (
        operation_type == "adsorption"
        and (slab_poscar is not None or slab_structure is not None)
        and pre_atom_count is not None
    ):
        try:
            slab_struct = slab_structure if slab_structure is not None else Poscar.from_file(slab_poscar).structure
            slab_z = [site.coords[2] for site in slab_struct]
            slab_top_z = max(slab_z)
            slab_mid_z = (max(slab_z) + min(slab_z)) / 2.0
            n_ads = n_atoms - pre_atom_count
            if n_ads > 0:
                ads_sites, method = identify_adsorbate_sites(struct, slab_struct, n_ads)
                ads_z = [site.coords[2] for site in ads_sites]
                info["slab_bottom_z_A"] = round(min(slab_z), 3)
                info["slab_top_z_A"] = round(slab_top_z, 3)
                ads_indices = {struct.index(site) for site in ads_sites}
                info["adsorbate_environments"] = [
                    {
                        "atom_index_1based": index + 1,
                        "element": struct[index].species_string,
                        "z_A": round(float(struct[index].coords[2]), 4),
                        "neighbors": [
                            {
                                "atom_index_1based": int(n.index) + 1,
                                "element": n.species_string,
                                "image": [int(v) for v in n.image],
                                "distance_A": round(float(n.nn_distance), 4),
                                "is_substrate": int(n.index) not in ads_indices,
                            }
                            for n in sorted(struct.get_neighbors(struct[index], 3.0), key=lambda n: n.nn_distance)[:8]
                        ],
                    }
                    for index in sorted(ads_indices)
                ]
                info["adsorbate_min_z_A"] = round(min(ads_z), 3)
                info["adsorbate_max_z_A"] = round(max(ads_z), 3)
                info["adsorbate_detection"] = method
                if min(ads_z) < slab_mid_z:
                    errors.append("Adsorbate appears embedded inside the slab.")
                elif min(ads_z) < slab_top_z - ADSORBATE_BELOW_TOP_WARNING_A:
                    warnings.append("Adsorbate minimum z is below slab top; verify adsorption geometry.")
        except Exception as exc:
            errors.append(f"Adsorbate geometry could not be verified: {exc}")

    if operation_type == "molecule":
        try:
            min_image_distance = minimum_periodic_image_distance(struct)
            if min_image_distance is not None:
                info["min_periodic_image_distance_A"] = round(min_image_distance, 3)
                if min_image_distance < MOLECULE_MIN_IMAGE_DISTANCE_A:
                    errors.append(
                        "Molecule periodic image distance is too small: "
                        f"{min_image_distance:.3f} A < {MOLECULE_MIN_IMAGE_DISTANCE_A:.1f} A."
                    )
                elif min_image_distance < MOLECULE_TIGHT_IMAGE_DISTANCE_A:
                    warnings.append(
                        "Molecule periodic image distance is tight: "
                        f"{min_image_distance:.3f} A; consider a slightly larger box if higher isolation is needed."
                    )
                elif min_image_distance >= MOLECULE_LARGE_IMAGE_DISTANCE_A:
                    warnings.append(
                        "Molecule periodic image distance is very large: "
                        f"{min_image_distance:.3f} A >= {MOLECULE_LARGE_IMAGE_DISTANCE_A:.1f} A; "
                        "the box may waste computational resources."
                    )
        except Exception as exc:
            warnings.append(f"Molecule periodic-image distance check failed: {exc}")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings, "info": info}


def validate_structure_relations(
    active_node_dir: str,
    structures: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    lattice_length_tolerance: float = 0.05,
    lattice_angle_tolerance: float = 0.2,
    coordinate_tolerance: float = GEOMETRY_MATCH_TOLERANCE_A,
    max_repeat_units: int = DEFAULT_MAX_SLAB_REPEAT_UNITS,
    fail_repeat_units: int = DEFAULT_FAIL_SLAB_REPEAT_UNITS,
    max_inplane_atoms_per_layer: int = DEFAULT_MAX_INPLANE_ATOMS_PER_LAYER,
) -> dict[str, Any]:
    """Validate structure relationships supplied by ModelingValidator."""
    from pymatgen.io.vasp import Poscar

    result = {"passed": True, "errors": [], "warnings": [], "findings": [], "structures": {}}
    active_dir = Path(active_node_dir).resolve()
    loaded: dict[str, dict[str, Any]] = {}

    for item in structures:
        label = str(item.get("label") or item.get("subdir") or "").strip()
        path_raw = item.get("poscar_path")
        if not label or not path_raw:
            result["errors"].append(f"Invalid structure entry missing label/poscar_path: {item}")
            continue
        path = Path(str(path_raw))
        if not path.is_absolute():
            path = active_dir / path
        try:
            struct = Poscar.from_file(str(path)).structure
        except Exception as exc:
            result["errors"].append(f"{label}: failed to parse POSCAR {path}: {exc}")
            continue
        layer_info = cluster_z_layers(struct)
        slab_size = slab_size_info(struct)
        repeat_count = layer_info["repeat_count"]
        if repeat_count is not None and repeat_count > fail_repeat_units:
            result["errors"].append(
                f"{label}: slab repeat count {repeat_count} exceeds fail threshold {fail_repeat_units}."
            )
        elif repeat_count is not None and repeat_count > max_repeat_units:
            result["warnings"].append(
                f"{label}: slab repeat count {repeat_count} exceeds normal maximum {max_repeat_units}."
            )
        primary_layers = slab_size["primary_species_layers"]
        if primary_layers > fail_repeat_units:
            result["errors"].append(
                f"{label}: primary slab species {slab_size['primary_slab_species']} has {primary_layers} z-layers, "
                f"exceeding fail threshold {fail_repeat_units}."
            )
        elif primary_layers > max_repeat_units:
            result["warnings"].append(
                f"{label}: primary slab species {slab_size['primary_slab_species']} has {primary_layers} z-layers; "
                f"default directly compared surface-family slabs should not exceed {max_repeat_units} without explicit justification."
            )
        max_atoms_per_layer = slab_size["max_primary_species_atoms_per_layer"]
        if max_atoms_per_layer > max_inplane_atoms_per_layer:
            result["warnings"].append(
                f"{label}: primary slab species {slab_size['primary_slab_species']} has up to "
                f"{max_atoms_per_layer} atoms per layer; this is a large in-plane slab for routine surface-family use."
            )
        loaded[label] = {"path": path, "structure": struct, "layer_info": layer_info}
        result["structures"][label] = {
            "poscar_path": str(path),
            "atom_count": len(struct),
            "composition": dict(composition_counts(struct)),
            "layer_info": layer_info,
            "slab_size": slab_size,
        }
        result["findings"].append(f"{label}: loaded {path}")

    for relation in relations:
        rel_type = str(relation.get("type") or "").strip()
        ref_label = str(relation.get("reference") or "").strip()
        target_label = str(relation.get("target") or "").strip()
        if not rel_type or not ref_label or not target_label:
            result["errors"].append(f"Invalid relation entry: {relation}")
            continue
        if ref_label not in loaded or target_label not in loaded:
            result["errors"].append(f"{rel_type}: missing referenced structure(s): {ref_label}, {target_label}")
            continue

        ref = loaded[ref_label]["structure"]
        target = loaded[target_label]["structure"]
        for issue in lattice_mismatch(ref, target, lattice_length_tolerance, lattice_angle_tolerance):
            result["errors"].append(f"{target_label}: lattice mismatch vs {ref_label}: {issue}.")

        pairs, unmatched_ref, unmatched_target = match_sites_by_species(ref, target, coordinate_tolerance)
        total_delta = len(target) - len(ref)
        delta = {
            elem: composition_counts(target).get(elem, 0) - composition_counts(ref).get(elem, 0)
            for elem in sorted(set(composition_counts(ref)) | set(composition_counts(target)))
        }

        if rel_type == "same_slab":
            if total_delta != 0 or unmatched_ref or unmatched_target:
                result["errors"].append(f"{target_label}: expected same slab as {ref_label}, but atom mapping differs.")
            sd_mismatch = compare_selective_dynamics(ref, target, pairs)
            if sd_mismatch:
                result["errors"].append(
                    f"{target_label}: {sd_mismatch} shared atom(s) have different Selective Dynamics tags than {ref_label}."
                )
        elif rel_type == "adsorption":
            if total_delta <= 0:
                result["errors"].append(
                    f"{target_label}: adsorption should increase atom count relative to {ref_label}."
                )
            if unmatched_ref:
                result["errors"].append(
                    f"{target_label}: adsorption changed or removed {len(unmatched_ref)} substrate atom(s)."
                )
        elif rel_type == "vacancy":
            if total_delta >= 0:
                result["errors"].append(f"{target_label}: vacancy should decrease atom count relative to {ref_label}.")
            if unmatched_target:
                result["errors"].append(
                    f"{target_label}: vacancy introduced {len(unmatched_target)} unmatched atom(s)."
                )
        elif rel_type == "doping":
            positive = sum(value for value in delta.values() if value > 0)
            negative = -sum(value for value in delta.values() if value < 0)
            if total_delta != 0 or positive == 0 or positive != negative:
                result["errors"].append(f"{target_label}: doping delta is not a balanced substitution: {delta}.")
            cross_pairs = match_sites_by_position(ref, target, unmatched_ref, unmatched_target, coordinate_tolerance)
            if len(cross_pairs) != positive:
                result["warnings"].append(f"{target_label}: could not position-match all substituted dopant site(s).")
        else:
            result["warnings"].append(
                f"{target_label}: unknown relation type {rel_type!r}; only lattice consistency was checked."
            )

        result["findings"].append(f"{target_label}: checked relation {rel_type} vs {ref_label}")

    result["passed"] = len(result["errors"]) == 0
    return result


def format_validation_report(result: dict[str, Any], poscar_name: str = "POSCAR") -> str:
    status = "PASSED" if result["passed"] else "FAILED"
    lines = [f"[Structure Validation] {poscar_name}: {status}"]
    info = result.get("info", {})
    if info:
        lines.append(f"  [INFO] formula={info.get('formula')} atom_count={info.get('atom_count')}")
        layer_info = info.get("layer_info")
        if layer_info:
            lines.append(
                "  [INFO] layers={layers} repeat_count={repeat} vacuum={vacuum} A".format(
                    layers=layer_info.get("actual_atomic_layers"),
                    repeat=layer_info.get("repeat_count"),
                    vacuum=layer_info.get("vacuum_angstrom"),
                )
            )
        if info.get("min_periodic_image_distance_A") is not None:
            lines.append(f"  [INFO] min_periodic_image_distance={info.get('min_periodic_image_distance_A')} A")
    if info.get("adsorbate_environments"):
        import json

        lines.append(f"  [INFO] substrate z range: {info['slab_bottom_z_A']}..{info['slab_top_z_A']} A")
        lines.append(
            "  [INFO] Adsorbate neighbors (periodic distances, not bond assignments): "
            + json.dumps(info["adsorbate_environments"])
        )
    for warning in result.get("warnings", []):
        lines.append(f"  [WARNING] {warning}")
    for error in result.get("errors", []):
        lines.append(f"  [ERROR] {error}")
    return "\n".join(lines)


def format_relation_validation_report(result: dict[str, Any]) -> str:
    status = "PASSED" if result["passed"] else "FAILED"
    lines = [f"[Structure Relations Validation] {status}"]
    for finding in result.get("findings", []):
        lines.append(f"  [FINDING] {finding}")
    for warning in result.get("warnings", []):
        lines.append(f"  [WARNING] {warning}")
    for error in result.get("errors", []):
        lines.append(f"  [ERROR] {error}")
    return "\n".join(lines)


class ValidateStructureAction(Action):
    poscar_path: str = Field(description="Path to the POSCAR/structure file to validate.")
    operation_type: str = Field(
        default="generic",
        description="Optional context: generic, adsorption, vacancy, doping, molecule, slab, or bulk.",
    )
    pre_atom_count: int | None = Field(
        default=None,
        description="Optional atom count before the modeling operation for simple delta checks.",
    )
    slab_poscar: str | None = Field(
        default=None,
        description="Optional clean slab POSCAR for adsorption height checks.",
    )


class ValidateStructureObservation(Observation):
    message: str = Field(default="")
    passed: bool = Field(default=False)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    info: dict[str, Any] = Field(default_factory=dict)

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


class ValidateStructureExecutor(ToolExecutor[ValidateStructureAction, ValidateStructureObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)

    def resolve_path_value(self, path_value: str | None) -> str | None:
        if not path_value:
            return None
        path = Path(path_value)
        if not path.is_absolute():
            path = self.workspace_dir / path
        return str(path)

    def __call__(self, action: ValidateStructureAction, conversation=None) -> ValidateStructureObservation:
        poscar_path = self.resolve_path_value(action.poscar_path)
        slab_poscar = self.resolve_path_value(action.slab_poscar)
        result = validate_structure(
            poscar_path=poscar_path or action.poscar_path,
            operation_type=action.operation_type,
            pre_atom_count=action.pre_atom_count,
            slab_poscar=slab_poscar,
        )
        return ValidateStructureObservation(
            message=format_validation_report(result, Path(action.poscar_path).name),
            passed=bool(result["passed"]),
            errors=list(result.get("errors", [])),
            warnings=list(result.get("warnings", [])),
            info=dict(result.get("info", {})),
        )


class ValidateStructureTool(ToolDefinition[ValidateStructureAction, ValidateStructureObservation]):
    """Validate one POSCAR for parsing, geometry sanity, and simple operation deltas."""

    name: ClassVar[str] = "ValidateStructure"

    DESCRIPTION: ClassVar[str] = (
        "Validate a single POSCAR for parseability, Selective Dynamics consistency, short contacts, "
        "basic slab layer info, molecule periodic-image spacing, and optional atom-count/adsorbate-height checks."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=ValidateStructureAction,
                observation_type=ValidateStructureObservation,
                executor=ValidateStructureExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


class ValidateStructureRelationsAction(Action):
    active_node_dir: str = Field(description="ACTIVE_NODE_DIR used to resolve relative POSCAR paths.")
    structures: list[dict[str, Any]] = Field(
        description="Structure entries prepared by ModelingValidator. Each item needs label or subdir, plus poscar_path."
    )
    relations: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Expected relationships prepared by ModelingValidator. Each item uses type/reference/target; "
            "supported types are same_slab, adsorption, vacancy, and doping."
        ),
    )
    lattice_length_tolerance: float = Field(default=0.05)
    lattice_angle_tolerance: float = Field(default=0.2)
    coordinate_tolerance: float = Field(default=GEOMETRY_MATCH_TOLERANCE_A)
    max_repeat_units: int = Field(default=DEFAULT_MAX_SLAB_REPEAT_UNITS)
    fail_repeat_units: int = Field(default=DEFAULT_FAIL_SLAB_REPEAT_UNITS)
    max_inplane_atoms_per_layer: int = Field(default=DEFAULT_MAX_INPLANE_ATOMS_PER_LAYER)


class ValidateStructureRelationsObservation(Observation):
    message: str = Field(default="")
    passed: bool = Field(default=False)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    structures: dict[str, Any] = Field(default_factory=dict)

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


class ValidateStructureRelationsExecutor(
    ToolExecutor[ValidateStructureRelationsAction, ValidateStructureRelationsObservation]
):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)

    def __call__(
        self,
        action: ValidateStructureRelationsAction,
        conversation=None,
    ) -> ValidateStructureRelationsObservation:
        active_dir = Path(action.active_node_dir)
        if not active_dir.is_absolute():
            active_dir = self.workspace_dir / active_dir
        result = validate_structure_relations(
            active_node_dir=str(active_dir),
            structures=action.structures,
            relations=action.relations,
            lattice_length_tolerance=action.lattice_length_tolerance,
            lattice_angle_tolerance=action.lattice_angle_tolerance,
            coordinate_tolerance=action.coordinate_tolerance,
            max_repeat_units=action.max_repeat_units,
            fail_repeat_units=action.fail_repeat_units,
            max_inplane_atoms_per_layer=action.max_inplane_atoms_per_layer,
        )
        return ValidateStructureRelationsObservation(
            message=format_relation_validation_report(result),
            passed=bool(result["passed"]),
            errors=list(result.get("errors", [])),
            warnings=list(result.get("warnings", [])),
            findings=list(result.get("findings", [])),
            structures=dict(result.get("structures", {})),
        )


class ValidateStructureRelationsTool(
    ToolDefinition[ValidateStructureRelationsAction, ValidateStructureRelationsObservation]
):
    """Validate relationships among POSCAR artifacts selected by ModelingValidator."""

    name: ClassVar[str] = "ValidateStructureRelations"

    DESCRIPTION: ClassVar[str] = (
        "Validate caller-supplied POSCAR relationships such as same-slab, adsorption, vacancy, and doping. "
        "The tool does not parse modeling_summary.json; the validator agent supplies the structured context."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=ValidateStructureRelationsAction,
                observation_type=ValidateStructureRelationsObservation,
                executor=ValidateStructureRelationsExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


register_tool("ValidateStructure", ValidateStructureTool)
register_tool("ValidateStructureRelations", ValidateStructureRelationsTool)


def get_modeling_validator_tools_config() -> list[Tool]:
    """Tool configuration for ModelingValidator."""
    return [Tool(name="ValidateStructure"), Tool(name="ValidateStructureRelations")]
