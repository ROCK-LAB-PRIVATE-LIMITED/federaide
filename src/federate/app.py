import sys
import os
import time
import json

# --- MILLISECOND-ACCURATE TELEMETRY PROFILER ---
_START_TIME = time.time()

def log_trace(step: str):
    elapsed = time.time() - _START_TIME
    sys.stdout.write(f"[{elapsed:6.2f}s]  {step}...\n")
    sys.stdout.flush()

log_trace("Bootstrapping package pathways")

# Ensure package directory is in sys.path to resolve absolute imports of submodules
package_dir = os.path.dirname(os.path.abspath(__file__))
if package_dir not in sys.path:
    sys.path.insert(0, package_dir)

import chatgpt_auth

import types
from pathlib import Path

log_trace("Setting up TUI TextIDE dependency mockups")
mock_textIDE = types.ModuleType("textIDE")
mock_textIDE.EXT_MAP = {
    ".py": "python", ".go": "go", ".c": "c", ".h": "c", 
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".rs": "rust",
    ".html": "html", ".htm": "html", ".js": "javascript",
    ".jl": "julia", ".f90": "fortran", ".f95": "fortran", ".f": "fortran",
    ".nim": "nim", ".zig": "zig"
}
sys.modules["textIDE"] = mock_textIDE

import platform
from pathlib import Path

def get_safe_starting_dir() -> str:
    if platform.system() == "Windows":
        path = Path.home() / "FederateWorkspace"
    elif platform.system() == "Darwin": # macOS
        path = Path.home() / "Documents" / "FederateWorkspace"
    else: # Linux / Others
        path = Path.home() / "FederateWorkspace"
    
    path.mkdir(parents=True, exist_ok=True)
    return str(path.absolute())

log_trace("Resolving workspace starting directory")
SAFE_START_DIR = get_safe_starting_dir()

# Profile Keyring loading separately BEFORE importing agent.py
# (Since agent.py imports toolbox, which executes keyring checks)
log_trace("Checking OS Keyring backend (evaluating DBus timeout risk)")
try:
    import keyring
    kr_backend = keyring.get_keyring()
    log_trace(f"Keyring active backend detected: {type(kr_backend).__name__}")
except Exception as e:
    log_trace(f"Keyring trace failed: {e}")

# Profile the sqlite episodic memory DB loading next
log_trace("Probing episodic memory SQLite database state")
try:
    import sqlite3
    db_path = os.path.join(str(Path.home()), ".federate", "episodic_memory.db")
    # Open connection to test for disk locking/congestion hangs
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.close()
    log_trace("SQLite state path verified")
except Exception as e:
    log_trace(f"SQLite trace failed: {e}")

log_trace("Compiling AIAgentView core components and NLP imports")
# This is where agent.py gets imported, which loads LangChain, Textual, etc.
from agent import AIAgentView

log_trace("Initializing Textual TUI wrapper framework")
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.widgets import Header, Footer, DirectoryTree, Label, Button
from textual.screen import ModalScreen
from textual import on

log_trace("Building standard tool configurations")
# Required for agent's execute_code tool since textIDE is stripped away
DEFAULT_RUN_CONFIGS = {
    "python": {"executable": "python", "flags": "-u \"{file}\""},
    "go": {"executable": "go", "flags": "run \"{file}\""},
    "c": {"executable": "gcc", "flags": "\"{file}\" -o \"{file_no_ext}\" && \"{file_no_ext}\""},
    "cpp": {"executable": "g++", "flags": "\"{file}\" -o \"{file_no_ext}\" && \"{file_no_ext}\""},
    "rust": {"executable": "rustc", "flags": "\"{file}\" && \"{file_no_ext}\""},
    "javascript": {"executable": "node", "flags": "\"{file}\""},
    "bash": {"executable": "bash" if os.name != "nt" else "sh", "flags": "\"{file}\""},
    "batch": {"executable": "cmd.exe" if os.name == "nt" else "echo", "flags": "/c \"{file}\"" if os.name == "nt" else "\"Batch files are not natively supported on this OS.\""},
    "powershell": {"executable": "powershell" if os.name == "nt" else "pwsh", "flags": "-ExecutionPolicy Bypass -File \"{file}\""},
}

class ExplorerTree(DirectoryTree):
    """A directory tree with bindings for modal navigation."""
    BINDINGS =[
        Binding("backspace", "go_up", "Dir Up", show=True),
        Binding("ctrl+b", "enter_dir", "Enter Dir", show=True)
    ]
    
    def action_go_up(self):
        """Navigate up one level."""
        self.path = str(Path(self.path).parent.absolute())
        
    def action_enter_dir(self):
        """Set the highlighted directory as the new visual root."""
        if self.cursor_node and self.cursor_node.data:
            path = self.cursor_node.data.path
            if path.is_dir():
                self.path = str(path.absolute())


class DirectoryModal(ModalScreen[str]):
    """Modal allowing the user to browse and select a workspace directory."""
    
    DEFAULT_CSS = """
    DirectoryModal {
        align: center middle;
        background: $background 60%;
    }
    #dir_dialog {
        width: 80;
        height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    .pane_title {
        background: $primary; 
        color: $text; 
        text-align: center; 
        text-style: bold; 
        width: 100%; 
    }
    .help_text {
        color: $text-muted;
        text-align: center;
        margin-bottom: 1;
    }
    #dir_tree_modal {
        height: 1fr;
        border: round $accent;
    }
    #dir_tree_modal:focus {
        border: round $success;
    }
    .buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    Button { margin-left: 1; }
    """

    def __init__(self, current_path: str):
        super().__init__()
        self.current_path = current_path

    def compose(self) -> ComposeResult:
        with Vertical(id="dir_dialog"):
            yield Label(" Select Working Directory", classes="pane_title")
            yield Label("Navigate: ↑/↓ | Expand: Enter | Enter Dir: Ctrl+B | Go Up: Backspace", classes="help_text")
            yield ExplorerTree(self.current_path, id="dir_tree_modal")
            
            with Horizontal(classes="buttons"):
                yield Button("Select Current Root", id="btn_select", variant="success")
                yield Button("Cancel", id="btn_cancel", variant="error")

    def on_mount(self):
        self.query_one("#dir_tree_modal").focus()

    @on(Button.Pressed, "#btn_select")
    def select_dir(self):
        tree = self.query_one("#dir_tree_modal", ExplorerTree)
        self.dismiss(tree.path)

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self):
        self.dismiss(None)


import toolbox

class Federate(App):
    """The main harness wrapper for agent.py"""
    
    CSS = """
    #dir_tree { 
        display: none; 
    }
    AIAgentView { 
        height: 1fr; 
        width: 100%; 
    }
    /* Force override the CSS in agent.py that hides the footer */
    Footer { 
        display: block !important; 
        visibility: visible !important; 
        height: 1 !important; 
    }
    """
    
    BINDINGS = [
        Binding("f8", "change_directory", "Change Dir", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]
    
    def notify(self, message, *args, **kwargs):
        """Catch-all to prevent bracket-parsing crashes in UI toasts."""
        from rich.markup import escape
        safe_msg = escape(str(message)) if isinstance(message, str) else message
        super().notify(safe_msg, *args, **kwargs)
    
    def __init__(self, initial_path: str = None):
        super().__init__()
        self.initial_path = initial_path
        # Satisfy agent.py's execute_code configuration dependencies
        self.run_configs = {k: v.copy() for k, v in DEFAULT_RUN_CONFIGS.items()}
    
    def on_mount(self):
        self.theme = "tokyo-night"
        if self.initial_path:
            path = Path(self.initial_path).resolve()
            if path.exists() and path.is_dir():
                target_dir = str(path)
            else:
                target_dir = str(path.parent)
        else:
            target_dir = get_safe_starting_dir()

        try:
            os.chdir(target_dir)
        except Exception:
            pass

        try:
            tree = self.query_one("#dir_tree", DirectoryTree)
            tree.path = target_dir
            tree.reload()
        except Exception:
            pass

        try:
            agent_view = self.query_one("#ai_agent_view", AIAgentView)
            if hasattr(agent_view, "update_status_bar"):
                agent_view.update_status_bar()
        except Exception:
            pass
        
    def action_quit(self):
        """Signals background threads to stop, clears the UI, and kills the process after a short delay."""
        import threading
        import os
        
        # 1. Signal all internal logic to stop
        toolbox.ABORT_EVENT.set()
        try:
            self.query_one("#ai_agent_view").action_abort()
        except:
            pass
            
        # 2. Start a 'Dead Man's Switch' timer. 
        # This gives the UI 200ms to clear the screen and restore the terminal 
        # before we forcefully terminate the process (and any hung threads).
        def hard_kill():
            print('\033[?25h', end='', flush=True)
            os._exit(0)
        
        kill_timer = threading.Timer(2.0, hard_kill)
        kill_timer.daemon = True
        kill_timer.start()
        
        # 3. Standard polite exit (restores terminal state)
        self.exit()
        
        # 4. Clear terminal after TUI closes
        os.system('cls' if os.name == 'nt' else 'clear')
        
    def compose(self) -> ComposeResult:
        yield Header()
        yield DirectoryTree(get_safe_starting_dir(), id="dir_tree") 
        yield AIAgentView(id="ai_agent_view")
        yield Footer()
        
    def action_change_directory(self):
        # Fallback to SAFE_START_DIR if the tree is somehow unavailable
        try:
            current_path = self.query_one("#dir_tree").path
        except:
            current_path = SAFE_START_DIR
            
        def handle_dir_selection(new_path):
            if new_path:
                hidden_tree = self.query_one("#dir_tree", DirectoryTree)
                hidden_tree.path = new_path
                hidden_tree.reload()
                
                # Sync process directory
                try:
                    import os
                    os.chdir(new_path)
                except Exception:
                    pass
                
                agent_view = self.query_one("#ai_agent_view", AIAgentView)
                if hasattr(agent_view, "update_status_bar"):
                    agent_view.update_status_bar()
                    
        self.push_screen(DirectoryModal(current_path), handle_dir_selection)

def get_positional_args(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    pos_args = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-r", "--resume"):
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                i += 2
                continue
            i += 1
            continue
        elif arg.startswith("-r=") or arg.startswith("--resume="):
            i += 1
            continue
        elif arg in ("-record", "-rec"):
            i += 1
            continue
        elif arg.startswith("-"):
            i += 1
            continue
        else:
            pos_args.append(arg)
            i += 1
    return pos_args


def main():
    import sys
    import shutil
    import subprocess
    import shlex

    if any(flag in sys.argv for flag in ("-record", "-rec")) and shutil.which("asciinema"):
        args_without_record = [arg for arg in sys.argv[1:] if arg not in ("-record", "-rec")]
        positional_args = get_positional_args(args_without_record)
        if positional_args:
            p = Path(positional_args[0]).resolve()
            target_dir = str(p) if p.is_dir() else str(p.parent)
        else:
            target_dir = get_safe_starting_dir()

        cast_path = os.path.join(target_dir, "footage.cast")
        if os.path.exists(cast_path):
            idx = 1
            while os.path.exists(os.path.join(target_dir, f"footage{idx}.cast")):
                idx += 1
            cast_path = os.path.join(target_dir, f"footage{idx}.cast")

        if sys.argv[0].endswith(".py"):
            cmd_list = [sys.executable, sys.argv[0]] + args_without_record
        else:
            cmd_list = [sys.argv[0]] + args_without_record
        quoted_cmd = " ".join(shlex.quote(arg) for arg in cmd_list)
        res = subprocess.run(["asciinema", "rec", "-c", quoted_cmd, cast_path])
        sys.exit(res.returncode)

    positional_args = get_positional_args(sys.argv[1:])
    initial_path = positional_args[0] if positional_args else None

    log_trace("Spawning standard Textual Application loop")
    app = Federate(initial_path=initial_path)
    app.run()

if __name__ == "__main__":
    main()