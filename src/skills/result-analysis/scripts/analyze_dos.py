#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib.pyplot as plt
import numpy as np
from pymatgen.electronic_structure.core import OrbitalType, Spin
from pymatgen.io.vasp import Vasprun


@dataclass
class ParsedDosRun:
    work_dir: Path
    xml_path: Path
    vasprun: Vasprun
    complete_dos: object


TRAPEZOID = getattr(np, "trapezoid", None) or np.trapz
LAYER_TOLERANCE_FRACTIONAL = 0.03


def load_parsed_dos_run(work_dir: Path) -> ParsedDosRun:
    xml_path = work_dir / "vasprun.xml"
    if not xml_path.exists():
        raise FileNotFoundError(f"vasprun.xml not found: {xml_path}")
    vasprun = Vasprun(str(xml_path), parse_dos=True)
    return ParsedDosRun(
        work_dir=work_dir,
        xml_path=xml_path,
        vasprun=vasprun,
        complete_dos=vasprun.complete_dos,
    )


def d_band_center(
    d_dos: np.ndarray,
    energies: np.ndarray,
    efermi: float,
    *,
    integration: str = "occupied",
) -> float | None:
    rel_energies = energies - efermi
    if integration == "occupied":
        mask = energies <= efermi
        integration_energies = rel_energies[mask]
        integration_dos = d_dos[mask]
    elif integration == "full":
        integration_energies = rel_energies
        integration_dos = d_dos
    else:
        raise ValueError(f"Unsupported d-band integration mode: {integration}")

    if np.sum(integration_dos) <= 1e-5:
        return None
    denominator = TRAPEZOID(integration_dos, integration_energies)
    if abs(denominator) <= 1e-12:
        return None
    numerator = TRAPEZOID(integration_energies * integration_dos, integration_energies)
    return round(float(numerator / denominator), 4)


def sum_spin_densities(dos_obj) -> np.ndarray:
    d = dos_obj.densities[Spin.up].copy()
    if Spin.down in dos_obj.densities:
        d += dos_obj.densities[Spin.down]
    return d


def site_d_dos(cdos, site_indices: list[int], target_element: str | None = None) -> np.ndarray | None:
    structure = cdos.structure
    d_dos: np.ndarray | None = None
    for i in site_indices:
        if i < 0 or i >= len(structure):
            continue
        site = structure[i]
        if target_element and site.specie.symbol != target_element:
            continue
        spd_dos = cdos.get_site_spd_dos(site)
        if OrbitalType.d not in spd_dos:
            continue
        site_d = sum_spin_densities(spd_dos[OrbitalType.d])
        d_dos = site_d if d_dos is None else d_dos + site_d
    return d_dos


def select_layer_site_indices(
    structure,
    selection: str | None,
    target_element: str | None = None,
) -> tuple[list[int], str | None]:
    if not selection:
        return [], None
    axis = max(range(3), key=lambda idx: structure.lattice.abc[idx])
    coords = [(float(site.frac_coords[axis]) % 1.0, i) for i, site in enumerate(structure)]
    min_coord = min(coord for coord, i in coords)
    max_coord = max(coord for coord, i in coords)

    selected: list[int] = []
    if selection in {"top-layer", "both-surfaces"}:
        selected.extend(i for coord, i in coords if abs(coord - max_coord) <= LAYER_TOLERANCE_FRACTIONAL)
    if selection in {"bottom-layer", "both-surfaces"}:
        selected.extend(i for coord, i in coords if abs(coord - min_coord) <= LAYER_TOLERANCE_FRACTIONAL)

    selected = sorted(set(selected))
    if target_element:
        selected = [i for i in selected if structure[i].specie.symbol == target_element]
    label = f"{selection} along lattice axis {axis + 1}"
    if target_element:
        label += f", element={target_element}"
    return selected, label


def extract_dos_physics(
    parsed: ParsedDosRun,
    site_indices: list[int] | None = None,
    site_selection: str | None = None,
    target_element: str | None = None,
    integration: str = "occupied",
) -> dict[str, Any]:
    v = parsed.vasprun
    cdos = parsed.complete_dos
    bandgap, cbm, vbm, is_direct = v.eigenvalue_band_properties

    energies = cdos.energies
    tdos = cdos.densities[Spin.up].copy()
    if Spin.down in cdos.densities:
        tdos += cdos.densities[Spin.down]
    idx_fermi = np.abs(energies - v.efermi).argmin()
    dos_at_fermi = float(tdos[idx_fermi])
    is_metal = bandgap < 0.05 or dos_at_fermi > 1e-3

    d_band_centers: dict[str, float] = {}
    full_d_band_centers: dict[str, float] = {}
    for element, pdos in cdos.get_element_dos().items():
        spd_dos = cdos.get_element_spd_dos(element)
        if OrbitalType.d not in spd_dos:
            continue
        d_dos = sum_spin_densities(spd_dos[OrbitalType.d])
        val = d_band_center(d_dos, energies, v.efermi, integration=integration)
        full_val = d_band_center(d_dos, energies, v.efermi, integration="full")
        if val is not None:
            d_band_centers[str(element)] = val
        if full_val is not None:
            full_d_band_centers[str(element)] = full_val

    result = {
        "fermi_level_eV": round(float(v.efermi), 4),
        "is_metal": bool(is_metal),
        "bandgap_eV": round(float(bandgap), 4) if not is_metal else 0.0,
        "cbm_abs_eV": round(float(cbm), 4) if not is_metal else None,
        "vbm_abs_eV": round(float(vbm), 4) if not is_metal else None,
        "is_direct_gap": bool(is_direct) if not is_metal else None,
        "dos_at_fermi_level": round(dos_at_fermi, 4),
        "d_band_integration": integration,
        "d_band_centers_rel_fermi_eV": d_band_centers or None,
        "full_d_band_centers_rel_fermi_eV": full_d_band_centers or None,
    }

    selected_indices = site_indices
    selected_label = "explicit site indices" if site_indices is not None else None
    if selected_indices is None and site_selection:
        selected_indices, selected_label = select_layer_site_indices(cdos.structure, site_selection, target_element)

    if selected_indices is not None:
        site_d: dict[str, float] = {}
        structure = cdos.structure
        for i in selected_indices:
            if i < 0 or i >= len(structure):
                continue
            site = structure[i]
            if target_element and site.specie.symbol != target_element:
                continue
            spd_dos = cdos.get_site_spd_dos(site)
            if OrbitalType.d not in spd_dos:
                continue
            d_dos = sum_spin_densities(spd_dos[OrbitalType.d])
            val = d_band_center(d_dos, energies, v.efermi, integration=integration)
            if val is not None:
                site_d[f"{i}_{site.specie.symbol}"] = val
        aggregate_d_dos = site_d_dos(cdos, selected_indices, target_element=target_element)
        if aggregate_d_dos is not None:
            aggregate_val = d_band_center(aggregate_d_dos, energies, v.efermi, integration=integration)
            aggregate_full_val = d_band_center(aggregate_d_dos, energies, v.efermi, integration="full")
        else:
            aggregate_val = None
            aggregate_full_val = None
        result["selected_site_indices"] = selected_indices
        result["selected_site_description"] = selected_label
        result["selected_site_d_band_center_rel_fermi_eV"] = aggregate_val
        result["selected_site_full_d_band_center_rel_fermi_eV"] = aggregate_full_val
        result["site_d_band_centers_rel_fermi_eV"] = site_d or None

    return result


def plot_dos(parsed: ParsedDosRun, output_dir: Path, xmin: float, xmax: float) -> dict[str, Any]:
    cdos = parsed.complete_dos
    e_fermi = cdos.efermi
    energies = cdos.energies - e_fermi
    tdos = cdos.densities
    is_spin_polarized = Spin.down in tdos

    fig, ax = plt.subplots(figsize=(8, 6), dpi=300)
    if is_spin_polarized:
        ax.fill_between(energies, 0, tdos[Spin.up], color="grey", alpha=0.3, label="TDOS (Up)")
        ax.fill_between(energies, 0, -tdos[Spin.down], color="grey", alpha=0.3, label="TDOS (Down)")
    else:
        ax.fill_between(energies, 0, tdos[Spin.up], color="grey", alpha=0.3, label="TDOS")

    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
    for idx, (element, pdos) in enumerate(cdos.get_element_dos().items()):
        color = colors[idx % len(colors)]
        if is_spin_polarized:
            ax.plot(energies, pdos[Spin.up], color=color, linewidth=1.5, label=f"{element} (Up)")
            ax.plot(energies, -pdos[Spin.down], color=color, linewidth=1.5, linestyle="--")
        else:
            ax.plot(energies, pdos[Spin.up], color=color, linewidth=1.5, label=f"{element}")

    ax.axvline(x=0, color="black", linestyle="--", linewidth=1)
    ax.axhline(y=0, color="black", linewidth=0.5)
    ax.set_xlim(xmin, xmax)
    ax.set_xlabel(r"Energy - $E_f$ (eV)", fontsize=14)
    ax.set_ylabel("Density of States (states/eV)", fontsize=14)
    ax.set_title("Total and Partial Density of States", fontsize=16)
    ax.legend(loc="upper right", frameon=False, fontsize=10)
    ax.tick_params(axis="both", which="major", labelsize=12)

    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / "DOS_plot.png"
    plt.tight_layout()
    plt.savefig(image_path)
    plt.close()
    return {"plot_path": str(image_path)}


def analyze_dos(
    work_dir: str,
    site_indices: list[int] | None = None,
    site_selection: str | None = None,
    target_element: str | None = None,
    integration: str = "occupied",
    render_plot: bool = False,
    plot_output_dir: str | None = None,
    xmin: float = -6.0,
    xmax: float = 6.0,
) -> dict[str, Any]:
    work_path = Path(work_dir).resolve()
    if not work_path.is_dir():
        return {"status": "missing_inputs", "message": f"Directory does not exist: {work_path}"}

    try:
        parsed = load_parsed_dos_run(work_path)
        result = {
            "status": "success",
            "message": "DOS analysis completed successfully",
            **extract_dos_physics(
                parsed,
                site_indices=site_indices,
                site_selection=site_selection,
                target_element=target_element,
                integration=integration,
            ),
        }
        if render_plot:
            output_dir = Path(plot_output_dir).resolve() if plot_output_dir else work_path / "analysis_artifacts"
            result.update(plot_dos(parsed, output_dir=output_dir, xmin=xmin, xmax=xmax))
        return result
    except FileNotFoundError as exc:
        return {"status": "missing_inputs", "message": str(exc)}
    except Exception as exc:
        return {"status": "error", "message": f"DOS feature extraction failed: {exc}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze DOS-related physical features from vasprun.xml.")
    parser.add_argument("--work-dir", required=True, help="Directory containing vasprun.xml.")
    parser.add_argument(
        "--site-indices",
        default=None,
        help="Comma-separated 0-based atom indices for site-resolved d-band center (e.g. 0,1,4).",
    )
    parser.add_argument(
        "--site-selection",
        choices=["top-layer", "bottom-layer", "both-surfaces"],
        default=None,
        help="Deterministically select slab surface-layer sites from POSCAR/vasprun structure.",
    )
    parser.add_argument(
        "--target-element", default=None, help="Optional element symbol used to filter selected sites, e.g. Ag or Pt."
    )
    parser.add_argument(
        "--d-band-integration",
        choices=["occupied", "full"],
        default="occupied",
        help="Energy range for the primary d-band center. The full moment is always reported separately when available.",
    )
    parser.add_argument("--render-plot", action="store_true", help="Render DOS_plot.png.")
    parser.add_argument("--plot-output-dir", default=None, help="Output directory for DOS_plot.png.")
    parser.add_argument("--xmin", type=float, default=-6.0, help="Minimum energy relative to Fermi level for plot.")
    parser.add_argument("--xmax", type=float, default=6.0, help="Maximum energy relative to Fermi level for plot.")
    parser.add_argument("--json", action="store_true", help="Print a single compact JSON object to stdout.")
    args = parser.parse_args()

    try:
        site_indices = [int(x) for x in args.site_indices.split(",")] if args.site_indices else None
        result = analyze_dos(
            args.work_dir,
            site_indices=site_indices,
            site_selection=args.site_selection,
            target_element=args.target_element,
            integration=args.d_band_integration,
            render_plot=args.render_plot,
            plot_output_dir=args.plot_output_dir,
            xmin=args.xmin,
            xmax=args.xmax,
        )
        if result.get("status") != "success":
            raise RuntimeError(result.get("message", "DOS analysis failed"))

        if args.json:
            print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))
            return

        ef = result["fermi_level_eV"]
        print(f"Fermi level: {ef} eV")
        if result["is_metal"]:
            print("System is metallic (bandgap < 0.05 eV).")
        else:
            print(
                f"Bandgap: {result['bandgap_eV']} eV  CBM(abs): {result['cbm_abs_eV']} eV  VBM(abs): {result['vbm_abs_eV']} eV"
            )
        dbc = result.get("d_band_centers_rel_fermi_eV")
        if dbc:
            entries = ", ".join(f"{el}: {v} eV" for el, v in dbc.items())
            print(f"d-band center (relative to E_f): {entries}")
        site_dbc = result.get("site_d_band_centers_rel_fermi_eV")
        if site_dbc:
            entries = ", ".join(f"{k}: {v} eV" for k, v in site_dbc.items())
            print(f"Site d-band center (relative to E_f): {entries}")
        selected_dbc = result.get("selected_site_d_band_center_rel_fermi_eV")
        if selected_dbc is not None:
            print(f"Selected-site d-band center (relative to E_f): {selected_dbc} eV")

        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        error_result = {"status": "error", "message": str(e)}
        if args.json:
            print(json.dumps(error_result, separators=(",", ":"), ensure_ascii=False))
        else:
            print(f"Error: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
