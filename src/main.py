"""CatPaw run-only entry point."""

import argparse
import sys
from pathlib import Path

from src.graph.runner import SupervisorRunner
from src.graph.state import DAGState
from src.utils.project_env import resolve_workspace_reference, setup_workspace
from src.utils.results import (
    normalize_completion_policy,
    read_completion_policy,
    read_pipeline_context,
    set_pipeline_completion_profile,
    set_pipeline_review_mode,
    write_pipeline_context,
)
from src.utils.ui import (
    print_banner,
    print_info,
    print_success,
    print_warning,
)

def is_interactive_shell() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def prompt_required(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print_warning(f"{label} cannot be empty. Please re-enter.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CatPaw run-only entry point",
        epilog=(
            "Examples:\n"
            '  python -m src.main "Compute OER on Co3O4(001) surface"\n'
            '  python -m src.main "Compute surface adsorption for this structure" -i ./Fe2O3.cif\n'
            '  python -m src.main "Compare clean slab and OH-adsorbed states" -w oer_demo --review\n'
            "  python -m src.monitor.service status oer_demo\n"
            "  python -m src.web.dashboard"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("goal", nargs="?", help="Research objective (natural-language description)")
    parser.add_argument(
        "-i",
        "--input",
        type=str,
        default=None,
        help="Initial structure file path (POSCAR/.cif/.xsd); leave blank for de novo modeling",
    )
    parser.add_argument("-w", "--workspace", type=str, default=None, help="Custom workspace name")
    parser.add_argument(
        "--resume",
        metavar="WORKSPACE",
        help="Resume an existing workspace by name or path instead of creating a new one",
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Print resume decisions without changing state or calling an LLM",
    )
    parser.add_argument(
        "--restart-nodes",
        nargs="+",
        default=None,
        metavar="NODE_ID",
        help="Explicitly archive and restart selected DAG nodes and all transitive descendants",
    )
    parser.add_argument(
        "--run-node",
        metavar="NODE_ID",
        help="Run or resume one ready/running DAG node without starting the full scheduler",
    )
    review_group = parser.add_mutually_exclusive_group()
    review_group.add_argument(
        "--review",
        dest="review",
        action="store_true",
        help="Enable human review gates; persisted for future resumes",
    )
    review_group.add_argument(
        "--no-review",
        dest="review",
        action="store_false",
        help="Disable human review gates; persisted for future resumes",
    )
    parser.add_argument(
        "--completion-profile",
        choices=("strict", "demonstration"),
        default=None,
        help="Workspace completion policy; explicit values are persisted on resume",
    )
    parser.set_defaults(review=None)
    return parser


def run_pipeline_command(
    args: argparse.Namespace, *, prepared_workspace: tuple[str, str | None] | None = None, review_handler=None
) -> None:
    resume = bool(args.resume)
    restart_nodes: list[str] = []
    for raw in getattr(args, "restart_nodes", None) or []:
        restart_nodes.extend(part.strip() for part in str(raw).split(",") if part.strip())
    restart_nodes = list(dict.fromkeys(restart_nodes))
    if restart_nodes and not resume:
        raise ValueError("--restart-nodes requires --resume.")
    if resume:
        if args.input or args.workspace or args.goal:
            raise ValueError("--resume cannot be combined with goal, --input, or --workspace.")
        workspace_path = resolve_workspace_reference(args.resume)
        if not DAGState.exists(workspace_path):
            raise FileNotFoundError(f"Workspace has no .dag_state.json: {workspace_path}")
        user_command = DAGState.load(workspace_path).user_goal
        input_path = None
        print_info(f"Resuming computation workspace: {workspace_path}")
    else:
        if args.input and not Path(args.input).expanduser().exists():
            raise FileNotFoundError(f"Initial structure file does not exist: {args.input}")
        workspace_path, input_path = prepared_workspace or setup_workspace(
            input_file=args.input,
            workspace_name=args.workspace,
        )
        user_command = args.goal
        print_info(f"Current computation workspace: {workspace_path}")
        if input_path:
            print_info(f"Imported initial structure file: {Path(input_path).name}")

    if not resume:
        human_review = bool(args.review)
    elif args.review is None:
        context = read_pipeline_context(workspace_path) or {}
        human_review = context.get("review_mode") == "enabled"
    else:
        human_review = bool(args.review)
        if not args.reconcile_only:
            set_pipeline_review_mode(
                workspace_path,
                "enabled" if human_review else "disabled",
                user_command=user_command,
            )
    review_status = "enabled" if human_review else "disabled"
    print_info(f"Human review: {review_status}")

    completion_profile = getattr(args, "completion_profile", None)
    if completion_profile is None:
        completion_policy = read_completion_policy(workspace_path) if resume else normalize_completion_policy(None)
    else:
        completion_policy = normalize_completion_policy(completion_profile)
        if resume and not args.reconcile_only:
            set_pipeline_completion_profile(
                workspace_path,
                completion_profile,
                user_command=user_command,
            )
    profile_name = completion_policy["profile"]
    print_info(f"Completion profile: {profile_name}")

    if not resume:
        write_pipeline_context(
            workspace_path,
            user_command=user_command,
            input_path=input_path,
            review_mode="enabled" if human_review else "disabled",
            completion_policy=completion_policy,
        )
    pipeline = SupervisorRunner(
        workspace_path=workspace_path,
        user_command=user_command,
        human_review=human_review,
        review_handler=review_handler,
        input_path=input_path,
        resume=resume,
        reconcile_only=bool(args.reconcile_only),
        restart_nodes=restart_nodes,
        run_node=str(args.run_node).strip() if args.run_node else None,
    )

    job_id = pipeline.run()
    if args.reconcile_only:
        print_success("Resume reconciliation inspection completed.")
    elif args.run_node:
        print_success(f"Single-node worker completed: {args.run_node}")
    else:
        print_success(f"Pipeline execution completed. Final Job ID: {job_id}")


def main() -> None:
    parser = build_parser()
    if len(sys.argv) == 1 and is_interactive_shell():
        print_banner("CATPAW QUICKSTART")
        print_info("The first-use wizard will generate an equivalent run command.")
        goal = prompt_required("1) Research objective")
        input_file = input("2) Initial structure file path (leave blank for de novo modeling): ").strip() or None
        workspace_name = input("3) Workspace name (leave blank for auto-generation): ").strip() or None
        review_raw = input("4) Enable human review after validators? [y/N]: ").strip().lower()
        review_enabled = review_raw in {"y", "yes", "on", "true", "1", "enable", "enabled"}

        print_info("Equivalent command example:")
        preview = [f"python -m src.main \"{goal}\""]
        if input_file:
            preview.append(f"-i \"{input_file}\"")
        if workspace_name:
            preview.append(f"-w \"{workspace_name}\"")
        if review_enabled:
            preview.append("--review")
        print_info("  " + " ".join(preview))
        args = argparse.Namespace(
            goal=goal,
            input=input_file,
            workspace=workspace_name,
            review=review_enabled,
            resume=None,
            reconcile_only=False,
            restart_nodes=None,
            run_node=None,
            completion_profile=None,
        )
        run_pipeline_command(args)
    elif len(sys.argv) == 1:
        parser.print_help()
    else:
        args = parser.parse_args()
        if args.reconcile_only and not args.resume:
            raise ValueError("--reconcile-only requires --resume.")
        if args.restart_nodes and not args.resume:
            raise ValueError("--restart-nodes requires --resume.")
        if args.run_node and not args.resume:
            raise ValueError("--run-node requires --resume.")
        if args.run_node and args.reconcile_only:
            raise ValueError("--run-node cannot be combined with --reconcile-only.")
        if args.run_node and args.restart_nodes:
            raise ValueError("--run-node cannot be combined with --restart-nodes.")
        if not args.resume and not args.goal:
            if is_interactive_shell():
                args.goal = prompt_required("Research objective")
            else:
                raise ValueError(
                    "A research objective is required, e.g., python -m src.main \"Compute OER on Co3O4(001) surface\""
                )
        run_pipeline_command(args)


if __name__ == "__main__":
    main()
