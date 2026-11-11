class Colors:
    """Color palette for VASP Agents."""

    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    END = "\033[0m"


def print_banner(title: str, color: str = Colors.CYAN) -> None:
    """Print a major header banner for VASP tasks."""
    width = 70
    padding = (width - len(title) - 2) // 2
    header = f"{color}╔{'═' * (width - 2)}╗{Colors.END}"
    content = f"{color}║{' ' * padding} {Colors.BOLD}{title}{Colors.END}{color} {' ' * (width - padding - len(title) - 3)}║{Colors.END}"
    footer = f"{color}╚{'═' * (width - 2)}╝{Colors.END}"
    print(f"\n{header}\n{content}\n{footer}\n")


def print_step(step_name: str) -> None:
    """Print a pipeline step heading."""
    print(f"{Colors.BLUE}{Colors.BOLD}> {step_name}{Colors.END}")


def print_info(text: str) -> None:
    print(f"{Colors.CYAN}{text}{Colors.END}")


def print_success(message: str) -> None:
    print(f"{Colors.GREEN}{message}{Colors.END}")


def print_warning(text: str) -> None:
    print(f"{Colors.YELLOW}Warning: {text}{Colors.END}")


def print_error(text: str) -> None:
    print(f"{Colors.RED}{Colors.BOLD}Error: {text}{Colors.END}")


def print_monitor_header() -> None:
    """Print the Phase 3 monitoring header."""
    print_banner("PHASE 3: JOB MONITORING", color=Colors.YELLOW)


def print_job_status(subdir: str, state: str, detail: str = "") -> None:
    """Print one monitoring status line for a single task."""
    color_map = {
        "SUBMITTED": Colors.CYAN,
        "RUNNING": Colors.BLUE,
        "COMPLETED": Colors.GREEN,
        "CONVERGED": Colors.GREEN,
        "NOT_CONVERGED": Colors.YELLOW,
        "FAILED": Colors.RED,
    }
    color = color_map.get(state, Colors.END)
    suffix = f"  {detail}" if detail else ""
    print(f"  {color}{subdir:<22s} {state}{suffix}{Colors.END}")


def print_monitor_summary(elapsed_sec: float, next_check_sec: int) -> None:
    """Print the polling summary footer."""
    elapsed_m = int(elapsed_sec // 60)
    elapsed_h = elapsed_m // 60
    elapsed_m = elapsed_m % 60
    if elapsed_h > 0:
        elapsed_str = f"{elapsed_h}h{elapsed_m:02d}m"
    else:
        elapsed_str = f"{elapsed_m}m"
    next_str = f"{next_check_sec // 60}m" if next_check_sec >= 60 else f"{next_check_sec}s"
    print(f"\n{Colors.CYAN}Next check: in {next_str} | Monitored: {elapsed_str}{Colors.END}\n")
