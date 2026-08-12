from rich.console import Console
from rich.theme import Theme

# Custom status color palette
custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
})

console = Console(theme=custom_theme)


def print_banner():
    """Displays simple CLI header text without boxes."""
    console.print("[bold cyan]Multi-Agent Smart Contract Security Auditor[/bold cyan] [dim](Phase 1)[/dim]\n")


def print_info(message: str):
    """Prints an info indicator."""
    console.print(f"[info][i][/info] {message}")


def print_success(message: str):
    """Prints a success indicator."""
    console.print(f"[success][✓][/success] {message}")


def print_warning(message: str):
    """Prints a warning indicator."""
    console.print(f"[warning][!][/warning] {message}")


def print_error(message: str):
    """Prints an error indicator."""
    console.print(f"[error][✘][/error] {message}")