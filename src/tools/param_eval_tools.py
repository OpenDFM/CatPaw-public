import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import Tool, ToolExecutor, register_tool
from pydantic import Field

from src.utils.config import database_dir, select_hpc_resource_profile


class PotcarEncutEvaluatorAction(Action):
    elements: list[str] = Field(description="List of element symbols in the system, e.g. ['Ti', 'O', 'C'].")


class PotcarEncutEvaluatorObservation(Observation):
    evaluation_text: str = Field(default="")

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=f"⚛️ Pseudopotential and cutoff-energy evaluation report:\n{self.evaluation_text}")]


class PotcarEncutEvaluatorExecutor(ToolExecutor[PotcarEncutEvaluatorAction, PotcarEncutEvaluatorObservation]):
    def __call__(self, action: PotcarEncutEvaluatorAction, conversation=None) -> PotcarEncutEvaluatorObservation:
        try:
            json_path = database_dir() / "potcar_info.json"

            if not json_path.exists():
                return PotcarEncutEvaluatorObservation(evaluation_text=f"Error: file not found: {json_path}")

            with json_path.open("r", encoding="utf-8") as f:
                potcar_db = json.load(f)

            report_lines = []

            for el in action.elements:
                if el in potcar_db:
                    info_list = potcar_db[el]
                    if isinstance(info_list, list) and len(info_list) > 0:
                        report_lines.append(f"Element {el}: {len(info_list)} available pseudopotential variant(s):")
                        for info in info_list:
                            tag = info.get("tag", "Unknown")
                            enmax = info.get("ENMAX", 0)
                            # Precompute 1.3x ENMAX as a safe ENCUT suggestion.
                            encut_suggest = enmax * 1.3
                            report_lines.append(
                                f"  - Tag: [{tag}], ENMAX = {enmax} eV (safe ENCUT ≈ {encut_suggest:.1f} eV)"
                            )
                    else:
                        report_lines.append(f"Element {el}: ⚠️ Invalid database format (empty or non-list entry).")
                else:
                    report_lines.append(f"Element {el}: ⚠️ Element not found in database.")

            # Append explicit action guidance for ParamWorker.
            report_lines.append("\n[ParamWorker action guide]:")
            report_lines.append(
                "1. Select a suitable pseudopotential tag for each element based on the system's physical/chemical context. If no special condition is specified, begin with standard unsuffixed variants."
            )
            report_lines.append("2. Determine the maximum ENMAX among the selected pseudopotentials.")
            report_lines.append(
                "3. Pass the safe ENCUT derived from that maximum ENMAX to CreateIncar as the global ENCUT."
            )

            return PotcarEncutEvaluatorObservation(evaluation_text="\n".join(report_lines))

        except Exception as e:
            return PotcarEncutEvaluatorObservation(evaluation_text=f"Pseudopotential evaluation failed: {str(e)}")


class PotcarEncutEvaluatorTool(ToolDefinition[PotcarEncutEvaluatorAction, PotcarEncutEvaluatorObservation]):
    """Read element list and return recommended POTCAR variants with ENMAX basis for ENCUT selection."""

    name: ClassVar[str] = "PotcarEncutEvaluator"
    DESCRIPTION: ClassVar[str] = "Recommend POTCAR variants and ENCUT from the element list."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=PotcarEncutEvaluatorAction,
                observation_type=PotcarEncutEvaluatorObservation,
                executor=PotcarEncutEvaluatorExecutor(),
            )
        ]


class DftPlusUEvaluatorAction(Action):
    poscar_path: str = Field(
        description="Relative or absolute POSCAR path. The tool reads the full ordered element list from POSCAR to ensure LDAU array lengths match VASP expectations."
    )


class DftPlusUEvaluatorObservation(Observation):
    evaluation_text: str = Field(default="")

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=f"🧲 DFT+U evaluation report:\n{self.evaluation_text}")]


class DftPlusUEvaluatorExecutor(ToolExecutor[DftPlusUEvaluatorAction, DftPlusUEvaluatorObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: DftPlusUEvaluatorAction, conversation=None) -> DftPlusUEvaluatorObservation:
        try:
            from pymatgen.core import Structure

            # Parse POSCAR and extract a complete ordered element list.
            poscar_path = action.poscar_path
            if not os.path.isabs(poscar_path):
                poscar_path = os.path.join(self.workspace_dir, poscar_path)
            if not os.path.exists(poscar_path):
                return DftPlusUEvaluatorObservation(evaluation_text=f"Error: POSCAR file does not exist: {poscar_path}")

            struct = Structure.from_file(poscar_path)
            # Extract elements in POSCAR order (order-preserving deduplication).
            elements = list(dict.fromkeys(site.specie.symbol for site in struct))

            json_path = database_dir() / "hubbard_u_info.json"

            if not json_path.exists():
                return DftPlusUEvaluatorObservation(evaluation_text=f"Error: file not found: {json_path}")

            with json_path.open("r", encoding="utf-8") as f:
                u_db = json.load(f)

            needs_u = False
            has_f_electron = False
            l_list, u_list, j_list = [], [], []

            # Detect anions in system for context matching.
            anion_symbols = {"O", "S", "Se", "Te", "N", "F", "Cl", "Br", "I"}
            anions_in_system = [el for el in elements if el in anion_symbols]
            # Use the first anion as context key (typically the most electronegative).
            context_anion = anions_in_system[0] if anions_in_system else None

            # For elemental/pure metallic alloy systems (no anion), DFT+U is usually unnecessary.
            if not anions_in_system:
                return DftPlusUEvaluatorObservation(
                    evaluation_text="DFT+U is not required for the current system (no anion detected; classified as elemental metal or metallic alloy with itinerant d electrons)."
                )

            for el in elements:
                if el in u_db:
                    info = u_db[el]
                    l_val = info.get("L", -1)
                    if l_val == 3:
                        has_f_electron = True
                    # Prefer U/J values in contexts matched to the system anion; otherwise use defaults.
                    contexts = info.get("contexts", {})
                    if context_anion and context_anion in contexts:
                        u_val = contexts[context_anion].get("U", 0.0)
                        j_val = contexts[context_anion].get("J", 0.0)
                    else:
                        defaults = info.get("default", info)
                        u_val = defaults.get("U", 0.0)
                        j_val = defaults.get("J", 0.0)
                    if u_val > 0:
                        needs_u = True
                else:
                    l_val, u_val, j_val = -1, 0.0, 0.0

                l_list.append(str(l_val))
                u_list.append(str(u_val))
                j_list.append(str(j_val))

            if not needs_u:
                return DftPlusUEvaluatorObservation(
                    evaluation_text="DFT+U is not required for the current system (no strongly correlated element detected)."
                )

            # LMAXMIX: 6 for f-electron systems, 4 for d-electron systems.
            lmaxmix = 6 if has_f_electron else 4

            u_dict = {
                "LDAU": ".TRUE.",
                "LDAUTYPE": 2,
                "LDAUL": " ".join(l_list),
                "LDAUU": " ".join(u_list),
                "LDAUJ": " ".join(j_list),
                "LMAXMIX": lmaxmix,
            }

            report = (
                f"Potentially correlated elements were detected; carefully evaluate whether DFT+U correction is required (typically for transition-metal oxides/sulfides, but usually not for elemental transition metals).\n"
                f"POSCAR element order: {elements} (total {len(elements)} element type(s); LDAU array lengths are aligned).\n"
                f"If DFT+U is required, pass the following dictionary as `custom_params` to `CreateIncar` in the ParamWorker execution plan:\n"
                f"{json.dumps(u_dict, indent=4)}"
            )
            return DftPlusUEvaluatorObservation(evaluation_text=report)

        except Exception as e:
            return DftPlusUEvaluatorObservation(evaluation_text=f"DFT+U evaluation failed: {str(e)}")


class DftPlusUEvaluatorTool(ToolDefinition[DftPlusUEvaluatorAction, DftPlusUEvaluatorObservation]):
    """Read POSCAR element list and determine whether +U is needed; if needed, return formatted LDAU INCAR parameters."""

    name: ClassVar[str] = "DftPlusUEvaluator"
    DESCRIPTION: ClassVar[str] = "Evaluate whether DFT+U settings are needed for the system."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=DftPlusUEvaluatorAction,
                observation_type=DftPlusUEvaluatorObservation,
                executor=DftPlusUEvaluatorExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


class SystemComplexityEvaluatorAction(Action):
    poscar_path: str = Field(description="Relative or absolute POSCAR path, e.g. 'workspace/POSCAR'.")
    kpoints_mesh: list[int] | None = Field(
        default=None, description="Planned Gamma/Monkhorst-Pack mesh, e.g. [2, 2, 1]."
    )
    encut: float | None = Field(default=None, description="Planned ENCUT in eV when known.")


class SystemComplexityEvaluatorObservation(Observation):
    evaluation_text: str = Field(default="")
    resource_plan: dict[str, object] = Field(default_factory=dict)

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        plan = json.dumps(self.resource_plan, indent=2, ensure_ascii=False)
        return [
            TextContent(
                text=(
                    "⚙️ System complexity and resource-allocation evaluation report:\n"
                    f"{self.evaluation_text}\n\nStructured resource plan:\n{plan}"
                )
            )
        ]


class SystemComplexityEvaluatorExecutor(
    ToolExecutor[SystemComplexityEvaluatorAction, SystemComplexityEvaluatorObservation]
):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(
        self, action: SystemComplexityEvaluatorAction, conversation=None
    ) -> SystemComplexityEvaluatorObservation:
        try:
            # Support both absolute and relative paths.
            poscar_full_path = Path(action.poscar_path)
            if not poscar_full_path.is_absolute():
                poscar_full_path = Path(self.workspace_dir) / poscar_full_path

            if not poscar_full_path.exists():
                return SystemComplexityEvaluatorObservation(
                    evaluation_text=f"Error: file not found: {poscar_full_path}"
                )

            from pymatgen.core import Structure

            struct = Structure.from_file(poscar_full_path)
            num_atoms = len(struct)

            profile = select_hpc_resource_profile(num_atoms)
            resource_plan = {
                "profile_name": profile.name,
                "atom_count": num_atoms,
                "total_cpus": profile.total_cpus,
                "num_dcus": 1,
                "omp_threads": profile.omp_threads,
                "kpar": profile.kpar,
                "ncore": profile.ncore,
                "kpoints_mesh": list(action.kpoints_mesh or []),
                "encut": action.encut,
            }

            report = (
                f"Structure loaded successfully: {poscar_full_path.name}\n"
                f"Total atom count: {num_atoms}\n"
                f"Selected configured resource profile: {profile.name}\n\n"
                "[ParamWorker resource-scheduling guide]:\n"
                f"1. Use total_cpus={profile.total_cpus} MPI ranks on one configured CPU node; "
                f"OMP_NUM_THREADS={profile.omp_threads}.\n"
                f"2. Set NCORE={profile.ncore} and KPAR={profile.kpar} in INCAR.\n"
                "3. Copy the structured resource plan exactly into INCAR and "
                "parameter_summary.json submit_params; num_dcus=1 is a compatibility field."
            )
            return SystemComplexityEvaluatorObservation(
                evaluation_text=report,
                resource_plan=resource_plan,
            )

        except Exception as e:
            return SystemComplexityEvaluatorObservation(
                evaluation_text=f"System-complexity evaluation failed: {str(e)}"
            )


class SystemComplexityEvaluatorTool(
    ToolDefinition[SystemComplexityEvaluatorAction, SystemComplexityEvaluatorObservation]
):
    """Read atom count from POSCAR, evaluate task complexity, and return recommended Total Cores, NCORE, and KPAR."""

    name: ClassVar[str] = "SystemComplexityEvaluator"
    DESCRIPTION: ClassVar[str] = "Estimate resource settings from the structure size."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=SystemComplexityEvaluatorAction,
                observation_type=SystemComplexityEvaluatorObservation,
                executor=SystemComplexityEvaluatorExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


register_tool("PotcarEncutEvaluator", PotcarEncutEvaluatorTool)
register_tool("DftPlusUEvaluator", DftPlusUEvaluatorTool)
register_tool("SystemComplexityEvaluator", SystemComplexityEvaluatorTool)


def get_param_eval_tools_config() -> list[Tool]:
    """Parameter-planning tools used before job submission."""
    return [
        Tool(name="PotcarEncutEvaluator"),
        Tool(name="DftPlusUEvaluator"),
        Tool(name="SystemComplexityEvaluator"),
    ]
