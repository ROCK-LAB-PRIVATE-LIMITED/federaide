import os
import sys

# Ensure package directory is in sys.path to resolve absolute imports of submodules
package_dir = os.path.dirname(os.path.abspath(__file__))
if package_dir not in sys.path:
    sys.path.insert(0, package_dir)

import inspect
import importlib
from pathlib import Path
from textual.screen import ModalScreen
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Header, Footer, DirectoryTree, TabbedContent, TabPane, 
    TextArea, Tree, Static, ContentSwitcher, Tabs, RichLog, Label, Input, Button, Checkbox
)
from textual.widgets.text_area import Selection
from textual.message import Message
from execution import ExecutionManager, SettingsModal, DEFAULT_RUN_CONFIGS

try:
    from agent import AIAgentView
    HAS_AI_AGENT = True
except ImportError:
    HAS_AI_AGENT = False

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

import ctypes
import importlib

try:
    from tree_sitter import Language, Parser
    HAS_TREESITTER = True
except ImportError:
    HAS_TREESITTER = False

TS_LANG_CACHE = {}
HIGHLIGHTS_CACHE = {}

def get_ts_language(lang_name: str):
    if not HAS_TREESITTER or not lang_name: return None
    if lang_name in TS_LANG_CACHE: return TS_LANG_CACHE[lang_name] # Return cached
    
    # 1. Try tree-sitter-languages (covers most common languages out of the box)
    try:
        from tree_sitter_languages import get_language
        lang = get_language(lang_name)
        if lang:
            TS_LANG_CACHE[lang_name] = lang
            return lang
    except (ImportError, Exception):
        pass

    # 2. Try importing the individual Python-native package (e.g. tree-sitter-python)
    try:
        mod = importlib.import_module(f"tree_sitter_{lang_name}")
        if hasattr(mod, "language"):
            raw_lang = mod.language()
        elif hasattr(mod, "LANGUAGE"):
            raw_lang = mod.LANGUAGE()
        else:
            raw_lang = None
            
        if raw_lang:
            # If raw_lang is already a Language object (modern tree-sitter >= 0.21.0),
            # return it directly. Do not wrap it to prevent a TypeError.
            if isinstance(raw_lang, Language):
                lang = raw_lang
            else:
                try:
                    lang = Language(raw_lang)
                except (TypeError, Exception):
                    try:
                        lang = Language(raw_lang, lang_name)
                    except Exception:
                        lang = None
        else:
            lang = None
            
        if lang:
            TS_LANG_CACHE[lang_name] = lang
            return lang
    except (ImportError, Exception): 
        pass

    # 3. Try loading from Termux/system-specific compiled packages as a final fallback
    dirs = [
        "/data/data/com.termux/files/usr/lib",
        "/data/data/com.termux/files/usr/lib/tree_sitter",
        "/usr/lib",
        "/usr/lib/tree_sitter",
        "/usr/local/lib",
        "/usr/local/lib/tree_sitter"
    ]
    names = [
        f"{lang_name}.so",
        f"libtree-sitter-{lang_name}.so",
        f"libtree_sitter_{lang_name}.so"
    ]
    for d in dirs:
        for name in names:
            p = os.path.join(d, name)
            if os.path.exists(p):
                try:
                    lib = ctypes.CDLL(p)
                    symbol_name = f"tree_sitter_{lang_name}"
                    if hasattr(lib, symbol_name):
                        symbol = getattr(lib, symbol_name)
                        symbol.restype = ctypes.c_void_p
                        lang = Language(symbol())
                        TS_LANG_CACHE[lang_name] = lang
                        return lang
                except Exception:
                    pass

        return None

def get_highlights(lang_name: str) -> str:
    if lang_name in HIGHLIGHTS_CACHE: return HIGHLIGHTS_CACHE[lang_name] # Return cached
    
    # 1. Try individual package resources (e.g. tree_sitter_python)
    try:
        import importlib.resources as pkg_resources
        mod_name = f"tree_sitter_{lang_name}"
        res = None
        # Try to read the highlights.scm file using modern or legacy methods
        if hasattr(pkg_resources, "files"):
            try:
                res = (pkg_resources.files(mod_name) / "queries" / "highlights.scm").read_text()
            except (FileNotFoundError, Exception):
                pass
        if res is None:
            try:
                res = pkg_resources.read_text(f"{mod_name}.queries", "highlights.scm")
            except (ModuleNotFoundError, Exception):
                pass
        
        if res is not None:
            if lang_name == "cpp": res = get_highlights("c") + "\n" + res
            HIGHLIGHTS_CACHE[lang_name] = res
            return res
    except Exception:
        pass

    # 2. Try tree-sitter-languages packaged queries
    try:
        import tree_sitter_languages
        ts_languages_dir = os.path.dirname(tree_sitter_languages.__file__)
        possible_query_paths = [
            os.path.join(ts_languages_dir, "languages", "queries", f"{lang_name}.scm"),
            os.path.join(ts_languages_dir, "languages", "queries", lang_name, "highlights.scm"),
            os.path.join(ts_languages_dir, "repos", lang_name, "queries", "highlights.scm"),
            os.path.join(ts_languages_dir, "repos", f"tree-sitter-{lang_name}", "queries", "highlights.scm")
        ]
        for qp in possible_query_paths:
            if os.path.exists(qp):
                res = Path(qp).read_text(encoding="utf-8")
                if lang_name == "cpp": res = get_highlights("c") + "\n" + res
                HIGHLIGHTS_CACHE[lang_name] = res
                return res
    except Exception:
        pass

    # 3. Try system-specific paths (e.g., Termux) as a final query fallback
    system_query_paths = [
        f"/data/data/com.termux/files/usr/share/tree-sitter/queries/{lang_name}/highlights.scm",
        f"/data/data/com.termux/files/usr/share/tree-sitter/queries/tree-sitter-{lang_name}/highlights.scm",
        f"/usr/share/tree-sitter/queries/{lang_name}/highlights.scm",
        f"/usr/share/tree-sitter/queries/tree-sitter-{lang_name}/highlights.scm"
    ]
    for p in system_query_paths:
        if os.path.exists(p):
            try:
                res = Path(p).read_text(encoding="utf-8")
                if lang_name == "cpp": res = get_highlights("c") + "\n" + res
                HIGHLIGHTS_CACHE[lang_name] = res
                return res
            except Exception:
                pass
    
    HIGHLIGHTS_CACHE[lang_name] = ""
    return ""

EXT_MAP = {
    ".py": "python", ".go": "go", ".c": "c", ".h": "c", 
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".rs": "rust",
    ".html": "html", ".htm": "html", ".js": "javascript",
    ".jl": "julia", ".f90": "fortran", ".f95": "fortran", ".f": "fortran",
    ".nim": "nim", ".zig": "zig", ".sh": "bash", ".bash": "bash",
    ".bat": "batch", ".ps1": "powershell",
    ".sql": "sql", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".md": "markdown", ".css": "css"
}

import platform
from pathlib import Path
import json

def get_safe_starting_dir() -> str:
    """Returns a highly permissive, user-specific safe directory and creates it if missing."""
    if platform.system() == "Windows":
        # Safe, highly permissive path in the user profile (no admin rights needed)
        path = Path.home() / "FederateWorkspace"
    elif platform.system() == "Darwin": # macOS
        path = Path.home() / "Documents" / "FederateWorkspace"
    else: # Linux / Others
        path = Path.home() / "FederateWorkspace"
    
    # Automatically create the folder (and any parent folders) if it doesn't exist yet
    path.mkdir(parents=True, exist_ok=True)
    return str(path.absolute())

SAFE_START_DIR = get_safe_starting_dir()
PERSIST_FILE = "last_dir.json"

def save_last_dir(path: str):
    """Saves the path to a JSON file."""
    try:
        with open(PERSIST_FILE, "w") as f:
            json.dump({"last_dir": str(path)}, f)
    except Exception:
        pass

def load_last_dir() -> str:
    """Loads the path from JSON, or returns a safe default."""
    try:
        if os.path.exists(PERSIST_FILE):
            with open(PERSIST_FILE, "r") as f:
                return json.load(f).get("last_dir", get_safe_starting_dir())
    except:
        pass
    return get_safe_starting_dir()

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

class FindReplaceRequest(Message):
    def __init__(self, action: str, find: str, replace: str, case_sensitive: bool):
        super().__init__()
        self.action = action
        self.find = find
        self.replace = replace
        self.case_sensitive = case_sensitive

class FindReplaceModal(ModalScreen[dict]):
    DEFAULT_CSS = """
    FindReplaceModal { align: center middle; background: $background 60%; }
    #fr_dialog { 
        width: 100; /* Increased width */
        height: auto; 
        border: thick $primary; 
        background: $surface; 
        padding: 0 1; 
    }
    .fr_row { height: 3; align: left middle; margin: 0; }
    .fr_label { width: 10; content-align: left middle; }
    .fr_options { height: 3; margin-left: 10; }
    Input { width: 1fr; border: round $accent; }
    Checkbox { border: none; background: transparent; }
    .fr_buttons { 
        margin: 1 0; 
        align: right middle; 
        width: 100%; 
        height: 3; 
    }
    Button { 
        margin-left: 1; 
        /* Removed min-width to prevent cutoff */
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="fr_dialog"):
            yield Label(" FIND & REPLACE", classes="pane_title")
            with Horizontal(classes="fr_row"):
                yield Label("Find:", classes="fr_label")
                yield Input(placeholder="Search term...", id="find_input")
            with Horizontal(classes="fr_row"):
                yield Label("Replace:", classes="fr_label")
                yield Input(placeholder="Replace with...", id="replace_input")
            
            with Horizontal(classes="fr_options"):
                # Default to False (unchecked) as requested
                yield Checkbox("Match Case", value=False, id="case_cb")

            with Horizontal(classes="fr_buttons"):
                yield Button("Prev", id="btn_prev")
                yield Button("Next", id="btn_find", variant="primary")
                yield Button("Replace", id="btn_replace", variant="warning")
                yield Button("All", id="btn_all", variant="error")
                yield Button("Close", id="btn_cancel")

    def on_mount(self):
        self.query_one("#find_input").focus()

    @on(Button.Pressed)
    def handle_buttons(self, event: Button.Pressed):
        if event.button.id == "btn_cancel":
            self.app.pop_screen() # Reliability fix for closing
        else:
            self.post_message(FindReplaceRequest(
                action=event.button.id,
                find=self.query_one("#find_input", Input).value,
                replace=self.query_one("#replace_input", Input).value,
                case_sensitive=self.query_one("#case_cb", Checkbox).value
            ))

class ExplorerTree(DirectoryTree):
    BINDINGS =[
        Binding("backspace", "go_up", "Dir Up", show=False),
        Binding("ctrl+b", "enter_dir", "Enter Dir", show=False)
    ]
    
    def action_go_up(self):
        self.path = str(Path(self.path).parent)
        
    def action_enter_dir(self):
        # Set the selected directory as the new active root
        if self.cursor_node and self.cursor_node.data:
            path = self.cursor_node.data.path
            if path.is_dir():
                self.path = str(path)

class FilenameModal(ModalScreen[str]):
    """A minimal modal to ask for a file name or path."""
    
    DEFAULT_CSS = """
    FilenameModal { align: center middle; background: $background 60%; }
    #filename_dialog { width: 50; height: auto; border: thick $primary; background: $surface; padding: 0 2; }
    #filename_btn_container { width: 100%; height: auto; align: right middle; margin-top: 1; }
    Button { margin-left: 1; }
    """

    def __init__(self, prompt: str, default_text: str = ""):
        super().__init__()
        self.prompt = prompt
        self.default_text = default_text

    def compose(self) -> ComposeResult:
        with Vertical(id="filename_dialog"):
            yield Label(self.prompt, classes="pane_title")
            yield Input(self.default_text, id="filename_input")
            with Horizontal(id="filename_btn_container"):
                yield Button("OK", id="ok_btn", variant="success")
                yield Button("Cancel", id="cancel_btn", variant="error")

    def on_mount(self):
        self.query_one("#filename_input", Input).focus()

    @on(Input.Submitted, "#filename_input")
    @on(Button.Pressed, "#ok_btn")
    def submit(self):
        self.dismiss(self.query_one("#filename_input", Input).value)

    @on(Button.Pressed, "#cancel_btn")
    def cancel(self):
        self.dismiss(None)

class FEDERaiDE(App):
    CSS = """
    #main_switcher { height: 1fr; width: 100%; }
    #code_view { height: 100%; width: 100%; }
    #explorer_pane { width: 22%; height: 100%; }
    #editor_pane { width: 56%; height: 100%; }
    #outline_pane { width: 22%; height: 100%; }

    #dir_tree, #editor_tabs TextArea, #outline_tree {
        border: round transparent;
    }
    #dir_tree:focus, #editor_tabs TextArea:focus, #outline_tree:focus {
        border: round $accent;
    }

    Underline { display: none; }
    Tabs { border-bottom: solid $primary; }
    
    .pane_title { 
        background: $primary; color: $text; text-align: center; text-style: bold; width: 100%; height: 1;
    }
    
    #ai_chat_view { height: 100%; width: 100%; }
    #ai_placeholder { text-align: center; border: solid $accent; padding: 2 4; }
    """

    BINDINGS = [
        Binding("f6", "cycle_view", "Switch View", priority=True),
        Binding("ctrl+w", "close_tab", "Close Tab", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        
        Binding("ctrl+l", "cycle_tab_forward", "Next Tab", show=False),
        Binding("ctrl+o", "cycle_tab_backward", "Prev Tab", show=False),
        Binding("ctrl+c", "copy_selected", "Copy", show=True),
        
        Binding("ctrl+r", "run_file", "Run File", priority=True),
        Binding("ctrl+e", "terminate_execution", "Terminate Run", priority=True),
        Binding("f9", "open_settings", "Run Configs", priority=True),
        Binding("f7", "new_folder", "New Folder", priority=True),
        Binding("f8", "change_directory", "Change Dir", priority=True),
        Binding("f10", "open_venv_manager", "Venv Mgr", priority=True),
        
        Binding("ctrl+s", "save_file", "Save", priority=True),
        Binding("ctrl+shift+s", "save_as", "Save As", priority=True),
        Binding("ctrl+n", "new_file", "New File", priority=True),
        Binding("ctrl+f", "find_replace", "Find/Replace", priority=True),
        Binding("ctrl+k", "new_chat", "New Chat", priority=True),

        # Bubble up agent bindings so they are accessible directly from the Editor
        Binding("f2", "open_chat_manager", "Sessions", show=False),
        Binding("f4", "open_active_config", "Manage Agents", show=False),
        Binding("f5", "switch_agent", "Switch Agent", show=False),
    ]
    
    def notify(self, message, *args, **kwargs):
        """Catch-all to prevent bracket-parsing crashes in UI toasts."""
        from rich.markup import escape
        safe_msg = escape(str(message)) if isinstance(message, str) else message
        super().notify(safe_msg, *args, **kwargs)
    
    def __init__(self, initial_file: str = None):
        super().__init__()
        self.initial_file = initial_file
        self.views =["ai_chat_view", "code_view", "exec_manager_view"]
        self.current_view_index = 0
        self.open_files = {} 
        self._outline_timer = None
        self.native_highlights = {"css", "json", "markdown", "sql", "yaml"}
        
        self.run_configs = {k: v.copy() for k, v in DEFAULT_RUN_CONFIGS.items()}
        self.exec_count = 0
        self.active_venv_name = "defaultVenv"
        self.active_venv_path = os.path.join(os.path.expanduser("~"), ".federate", "defaultVenv")
    
    def action_quit(self):
        """Signals background threads to stop, restores terminal state, and terminates process."""
        import threading
        import os
        
        # 1. Signal all internal logic to stop
        try:
            import toolbox
            toolbox.ABORT_EVENT.set()
        except Exception:
            pass
            
        try:
            self.query_one("#ai_agent_view").action_abort()
        except Exception:
            pass
            
        # 2. Start a 'Dead Man's Switch' timer. 
        # This gives Textual 500ms to cleanly tear down the TUI screen and restore terminal modes
        # before we forcefully terminate the process and any hung background threads.
        def hard_kill():
            print('\033[?25h', end='', flush=True)
            os._exit(0)
        
        kill_timer = threading.Timer(0.5, hard_kill)
        kill_timer.daemon = True
        kill_timer.start()
        
        # 3. Standard polite exit (restores terminal state)
        self.exit()
        
        # 4. Clear terminal after TUI closes
        try:
            os.system('cls' if os.name == 'nt' else 'clear')
        except Exception:
            pass
    
    def compose(self) -> ComposeResult:
        yield Header()
        with ContentSwitcher(initial="ai_chat_view", id="main_switcher"):
            with Horizontal(id="code_view"):
                with Vertical(id="explorer_pane"):
                    yield Static(" EXPLORER", classes="pane_title")
                    yield ExplorerTree(load_last_dir(), id="dir_tree")
                with Vertical(id="editor_pane"):
                    yield TabbedContent(id="editor_tabs")
                with Vertical(id="outline_pane"):
                    yield Static(" STRUCTURE", classes="pane_title")
                    yield Tree("Outline", id="outline_tree")
            
            yield ExecutionManager(id="exec_manager_view")
            
            with Vertical(id="ai_chat_view"):
                if HAS_AI_AGENT:
                    yield AIAgentView(id="ai_agent_view")
                else:
                    yield Static(" AI Assistant Chat System\n\n(Install requirements: langgraph, langchain-openai, duckduckgo-search, bs4)", id="ai_placeholder")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "monokai"
        try:
            import os
            os.chdir(self.query_one("#dir_tree").path)
        except Exception:
            pass
        tabs_widget = self.query_one("#editor_tabs", TabbedContent).query_one(Tabs)
        tabs_widget.can_focus = False
        
        # Activate default venv
        self.activate_venv(self.active_venv_name, self.active_venv_path)

        if self.initial_file:
            path = Path(self.initial_file).absolute()
            if path.exists() and path.is_dir():
                self.query_one("#dir_tree", DirectoryTree).path = str(path)
            else:
                # File doesn't exist or is a file: open it
                # Set dir tree to parent
                self.query_one("#dir_tree", DirectoryTree).path = str(path.parent)
                # Open the file (open_file now handles non-existent paths)
                self.open_file(path)
                # Switch to code view
                self.query_one("#main_switcher").current = "code_view"
                self.current_view_index = self.views.index("code_view")
                return 
        
        # --- FIX: Change focus from dir_tree to chat input ---
        try:
            # We look for the AI input inside the AIAgentView component
            self.query_one("#ai_chat_input").focus()
        except Exception:
            # Fallback if AI agent isn't loaded/visible
            self.query_one("#dir_tree").focus()
    
    def action_open_chat_manager(self):
        if HAS_AI_AGENT:
            try: self.query_one("#ai_agent_view").action_open_chat_manager()
            except Exception: pass

    def action_open_active_config(self):
        if HAS_AI_AGENT:
            try: self.query_one("#ai_agent_view").action_open_active_config()
            except Exception: pass

    def action_switch_agent(self):
        if HAS_AI_AGENT:
            try: self.query_one("#ai_agent_view").action_switch_agent()
            except Exception: pass
    
    def action_cycle_view(self):
        self.current_view_index = (self.current_view_index + 1) % len(self.views)
        
        if self.views[self.current_view_index] == "exec_manager_view":
            # Skip if no executions started, OR if the active execution tab is the idle panel
            exec_manager = self.query_one("#exec_manager_view")
            is_empty = self.exec_count == 0 or exec_manager.query_one("#exec_tabs").active == "idle_pane"
            if is_empty:
                self.current_view_index = (self.current_view_index + 1) % len(self.views)
            
        view_id = self.views[self.current_view_index]
        self.query_one("#main_switcher").current = view_id
        
        if view_id == "code_view":
            tabs = self.query_one("#editor_tabs", TabbedContent)
            if tabs.active:
                try: self.query_one(f"#{tabs.active} TextArea").focus()
                except Exception: self.query_one("#dir_tree").focus()
            else: self.query_one("#dir_tree").focus()
    
    def action_new_chat(self):
        """Switch to AI view and initiate a fresh multi-agent chat."""
        switcher = self.query_one("#main_switcher")
        self.current_view_index = self.views.index("ai_chat_view")
        switcher.current = "ai_chat_view"
        if HAS_AI_AGENT:
            try:
                ai_view = self.query_one("#ai_agent_view")
                ai_view.action_clear_all_contexts()
                self.query_one("#ai_chat_input").focus()
            except Exception:
                pass
    
    def action_close_tab(self):
        if self.query_one("#main_switcher").current == "exec_manager_view":
            try:
                manager = self.query_one("#exec_manager_view")
                manager.terminate_active()  # Stop process if still running
                exec_tabs = manager.query_one(TabbedContent)
                if exec_tabs.active:
                    is_last = len(exec_tabs.query("TabPane")) <= 1
                    exec_tabs.remove_pane(exec_tabs.active)
                    if is_last:
                        self.action_cycle_view()
            except Exception: pass
            return

        tabs = self.query_one("#editor_tabs", TabbedContent)
        if not tabs.active: return
        path_to_remove = next((path for path, data in self.open_files.items() if data["pane_id"] == tabs.active), None)
        if path_to_remove:
            del self.open_files[path_to_remove]
            tabs.remove_pane(tabs.active)
            if not self.open_files:
                self.query_one("#outline_tree").clear()
                self.query_one("#outline_tree").root.set_label("Outline")
                self.query_one("#dir_tree").focus()

    def _cycle_tabs(self, direction: int):
        switcher = self.query_one("#main_switcher")
        
        # Scenario A: Code View (cycle through open editor files)
        if switcher.current == "code_view":
            tabs = self.query_one("#editor_tabs", TabbedContent)
            if not tabs.active or len(self.open_files) < 2: return
            panes = tabs.query(TabPane)
            pane_ids = [p.id for p in panes]
            try:
                next_index = (pane_ids.index(tabs.active) + direction + len(pane_ids)) % len(pane_ids)
                tabs.active = pane_ids[next_index]
                self.query_one(f"#{tabs.active} TextArea").focus()
            except ValueError: pass

        # Scenario B: Execution View (cycle through active script runs)
        elif switcher.current == "exec_manager_view":
            try:
                manager = self.query_one("#exec_manager_view")
                tabs = manager.query_one("#exec_tabs", TabbedContent)
                panes = tabs.query(TabPane)
                if len(panes) < 2: return
                pane_ids = [p.id for p in panes]
                
                next_index = (pane_ids.index(tabs.active) + direction + len(pane_ids)) % len(pane_ids)
                tabs.active = pane_ids[next_index]
                
                # Automatically focus the terminal inside the newly active tab
                try:
                    from execution import TerminalEmulator
                    term = manager.query_one(f"#{tabs.active}", TabPane).query_children(TerminalEmulator).first()
                    term.focus()
                except Exception:
                    pass
            except Exception:
                pass

    def action_cycle_tab_forward(self): self._cycle_tabs(1)
    def action_cycle_tab_backward(self): self._cycle_tabs(-1)

    def action_copy_selected(self):
        if self.query_one("#main_switcher").current == "code_view":
            try:
                editor = self.focused
                if isinstance(editor, TextArea) and editor.selected_text:
                    if HAS_PYPERCLIP:
                        pyperclip.copy(editor.selected_text)
                        self.notify("Copied to clipboard!")
                    else: self.notify("Install 'pyperclip' for copy support.", severity="error")
                    return
            except Exception: pass
        self.notify("No text selected to copy.", severity="warning")

    def action_open_settings(self):
        def handle_settings(new_configs):
            if new_configs: self.run_configs = new_configs
        self.push_screen(SettingsModal(self.run_configs), handle_settings)
    
    def action_find_replace(self):
        tabs = self.query_one("#editor_tabs", TabbedContent)
        if not tabs.active:
            self.notify("No file open.", severity="warning")
            return
        # Just push the screen; no callback needed here anymore!
        self.push_screen(FindReplaceModal())
    
    @on(FindReplaceRequest)
    def handle_find_replace_request(self, message: FindReplaceRequest):
        tabs = self.query_one("#editor_tabs", TabbedContent)
        if not tabs.active: return
        
        editor = self.query_one(f"#{tabs.active} TextArea", TextArea)
        if not message.find: return

        if message.action == "btn_find":
            self._do_find(editor, message.find, editor.cursor_location, direction="next", case_sensitive=message.case_sensitive)
        
        elif message.action == "btn_prev":
            self._do_find(editor, message.find, editor.cursor_location, direction="prev", case_sensitive=message.case_sensitive)

        elif message.action == "btn_replace":
            # Replacement is always case-sensitive to exactly what user typed in replace box
            current_text = editor.selected_text
            match = (current_text == message.find) if message.case_sensitive else (current_text.lower() == message.find.lower())
            
            if match:
                editor.replace(message.replace)
            self._do_find(editor, message.find, editor.cursor_location, direction="next", case_sensitive=message.case_sensitive)

        elif message.action == "btn_all":
            content = editor.text
            if message.case_sensitive:
                count = content.count(message.find)
                new_content = content.replace(message.find, message.replace)
            else:
                import re
                insensitive_find = re.compile(re.escape(message.find), re.IGNORECASE)
                new_content, count = insensitive_find.subn(message.replace, content)
            
            editor.load_text(new_content)
            self.notify(f"Replaced {count} occurrences.")
    
    def _do_find(self, editor: TextArea, term: str, cursor_loc: tuple, direction="next", case_sensitive=False):
        from textual.widgets.text_area import Selection
        content = editor.text
        
        # 1. Convert cursor location to absolute index
        l, c = cursor_loc
        lines = content.splitlines(keepends=True)
        abs_cursor = sum(len(lines[i]) for i in range(min(l, len(lines)))) + c
        
        # 2. Prepare content for case sensitivity
        search_content = content if case_sensitive else content.lower()
        search_term = term if case_sensitive else term.lower()

        idx = -1
        if direction == "next":
            # Search from cursor + 1 to move forward
            idx = search_content.find(search_term, abs_cursor + 1)
            if idx == -1: idx = search_content.find(search_term, 0) # Wrap to top
        else:
            # Search backward: current selection logic fix
            # If we have a selection, we start before the current selection start
            sel_start, _ = editor.selection
            sl, sc = sel_start
            abs_sel_start = sum(len(lines[i]) for i in range(min(sl, len(lines)))) + sc
            
            idx = search_content.rfind(search_term, 0, abs_sel_start)
            if idx == -1: idx = search_content.rfind(search_term) # Wrap to bottom

        if idx != -1:
            def get_pos(target_idx):
                before = content[:target_idx]
                line = before.count('\n')
                last_nl = before.rfind('\n')
                col = target_idx if last_nl == -1 else target_idx - last_nl - 1
                return (line, col)

            editor.selection = Selection(get_pos(idx), get_pos(idx + len(term)))
            editor.scroll_cursor_visible() 
        else:
            self.notify("String not found.", severity="warning")
    
    def action_run_file(self):
        tabs = self.query_one("#editor_tabs", TabbedContent)
        if not tabs.active:
            self.notify("No file open to run.", severity="warning")
            return

        file_data = next((v for v in self.open_files.values() if v["pane_id"] == tabs.active), None)
        if not file_data: return

        try: file_data["path"].write_text(file_data["text_area"].text, encoding="utf-8")
        except Exception as e: self.notify(f"Auto-save failed: {e}", severity="warning")

        lang = file_data["language"]
        if not lang or lang not in self.run_configs:
            self.notify(f"No run config for language: {lang}. Press F8 to configure.", severity="error")
            return
            
        config = self.run_configs[lang]
        
        self.exec_count += 1
        manager = self.query_one("#exec_manager_view", ExecutionManager)
        manager.start_run(str(file_data["path"]), config.get("executable", ""), config.get("flags", ""), self.exec_count, venv_path=getattr(self, "active_venv_path", None))
        
        switcher = self.query_one("#main_switcher")
        self.current_view_index = self.views.index("exec_manager_view")
        switcher.current = "exec_manager_view"

    def action_terminate_execution(self):
        switcher = self.query_one("#main_switcher")
        if switcher.current == "exec_manager_view":
            manager = self.query_one("#exec_manager_view", ExecutionManager)
            if not manager.terminate_active():
                self.current_view_index = self.views.index("code_view")
                switcher.current = "code_view"
                try: self.query_one(f"#{self.query_one('#editor_tabs', TabbedContent).active} TextArea").focus()
                except Exception: self.query_one("#dir_tree").focus()
            self.notify("Execution terminated.")
        else:
            self.notify("Switch to Executions View (F6) to terminate runs.", severity="warning")

    @on(DirectoryTree.FileSelected)
    def on_file_selected(self, event: DirectoryTree.FileSelected):
        if event.path.is_file(): self.open_file(event.path)

    def open_file(self, path: Path):
        str_path = str(path.absolute())
        tabs = self.query_one("#editor_tabs", TabbedContent)
        
        if str_path in self.open_files:
            tabs.active = self.open_files[str_path]["pane_id"]
            self.query_one(f"#{tabs.active} TextArea").focus()
            return

        content = ""
        if path.exists():
            try: content = path.read_text(encoding="utf-8")
            except Exception as e:
                self.notify(f"Cannot open file: {e}", severity="error")
                return

        pane_id = f"tab_{hash(str_path)}"
        text_area = TextArea(content, id=f"editor_{pane_id}", show_line_numbers=True, soft_wrap=False)
        mapped_lang = EXT_MAP.get(path.suffix.lower())
        
        if mapped_lang:
            if mapped_lang not in self.native_highlights:
                ts_lang = get_ts_language(mapped_lang)
                if ts_lang:
                    query = get_highlights(mapped_lang)
                    if query:
                        try:
                            sig = inspect.signature(text_area.register_language)
                            if len(sig.parameters) == 2: text_area.register_language(ts_lang, query)
                            else: text_area.register_language(mapped_lang, ts_lang, query)
                        except Exception: pass 
            try: text_area.language = mapped_lang
            except Exception: text_area.language = None

        pane = TabPane(path.name, text_area, id=pane_id)
        self.open_files[str_path] = {"pane_id": pane_id, "path": path, "text_area": text_area, "language": mapped_lang}
        
        tabs.add_pane(pane)
        tabs.active = pane_id
        text_area.focus()
        self.update_outline(content, mapped_lang)

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self, event: TabbedContent.TabActivated):
        self.update_outline_for_active_tab()

    @on(TextArea.Changed)
    def on_text_changed(self, event: TextArea.Changed):
        if self._outline_timer is not None: self._outline_timer.stop()
        self._outline_timer = self.set_timer(0.5, self.update_outline_for_active_tab)

    def update_outline_for_active_tab(self):
        tabs = self.query_one("#editor_tabs", TabbedContent)
        if not tabs.active: return
        file_data = next((v for v in self.open_files.values() if v["pane_id"] == tabs.active), None)
        if file_data: self.update_outline(file_data["text_area"].text, file_data["language"])
    
    def action_save_file(self):
        tabs = self.query_one("#editor_tabs", TabbedContent)
        if not tabs.active:
            self.notify("No file to save.", severity="warning")
            return
            
        file_data = next((v for v in self.open_files.values() if v["pane_id"] == tabs.active), None)
        if file_data:
            try:
                file_data["path"].write_text(file_data["text_area"].text, encoding="utf-8")
                self.notify(f"Saved {file_data['path'].name}")
            except Exception as e:
                self.notify(f"Save failed: {e}", severity="error")

    def action_save_as(self):
        tabs = self.query_one("#editor_tabs", TabbedContent)
        if not tabs.active:
            self.notify("No file to save.", severity="warning")
            return
            
        file_data = next((v for v in self.open_files.values() if v["pane_id"] == tabs.active), None)
        if not file_data: return

        def handle_save_as(name: str):
            if name:
                new_path = Path(name) if Path(name).is_absolute() else Path.cwd() / name
                try:
                    new_path.write_text(file_data["text_area"].text, encoding="utf-8")
                    self.query_one("#dir_tree", ExplorerTree).reload()
                    
                    # Open new tab and kill the old one
                    old_active = tabs.active
                    self.open_file(new_path)
                    if old_active != tabs.active:
                        path_to_remove = next((p for p, d in self.open_files.items() if d["pane_id"] == old_active), None)
                        if path_to_remove:
                            del self.open_files[path_to_remove]
                            tabs.remove_pane(old_active)
                    self.notify(f"Saved as {new_path.name}")
                except Exception as e:
                    self.notify(f"Save As failed: {e}", severity="error")

        self.push_screen(FilenameModal("Save As (Enter name or path):", file_data["path"].name), handle_save_as)
    
    def action_new_folder(self):
        def handle_foldername(name: str):
            if name:
                tree = self.query_one("#dir_tree", ExplorerTree)
                # Resolve against the currently active tree root directory
                new_path = Path(name) if Path(name).is_absolute() else Path(tree.path) / name
                
                if new_path.exists():
                    self.notify("Folder already exists!", severity="error")
                    return
                try:
                    new_path.mkdir(parents=True, exist_ok=True)
                    tree.reload()
                    self.notify(f"Created folder {new_path.name}")
                except Exception as e:
                    self.notify(f"Failed to create folder: {e}", severity="error")
                    
        self.push_screen(FilenameModal("Enter new folder name:"), handle_foldername)
    
    def action_new_file(self):
        def handle_filename(name: str):
            if name:
                new_path = Path(name) if Path(name).is_absolute() else Path.cwd() / name
                if new_path.exists():
                    self.notify("File already exists!", severity="error")
                    return
                try:
                    new_path.touch()
                    self.open_file(new_path)
                    self.query_one("#dir_tree", ExplorerTree).reload()
                    self.notify(f"Created {new_path.name}")
                except Exception as e:
                    self.notify(f"Failed to create file: {e}", severity="error")
                    
        self.push_screen(FilenameModal("Enter new file name:"), handle_filename)
    
    @work(thread=True, exclusive=True)
    def update_outline(self, code: str, lang_name: str):
        display_name = (lang_name.title() if lang_name else "Plain Text")
        try:
            if not lang_name:
                self.app.call_from_thread(self._render_outline, f"{display_name} Outline", [])
                return

            import tempfile, subprocess, json, os
            bin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "federate_bridge" + (".exe" if os.name == "nt" else ""))
            ext = next((k for k, v in EXT_MAP.items() if v == lang_name), ".txt")
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f: f.write(code.encode('utf-8'))
            raw_nodes = json.loads(subprocess.check_output([bin_path, "parse", f.name], stderr=subprocess.DEVNULL).decode('utf-8'))
            os.remove(f.name)
            format_nodes = lambda ns: [{"label": f"{'Cl' if n['type'] in ('class','struct','interface','module','namespace','type') else 'ƒ'} {n['name']}", "children": format_nodes(n.get('children',[]))} for n in ns]
            self.app.call_from_thread(self._render_outline, f"{display_name} Outline", format_nodes(raw_nodes))
        except Exception:
            self.app.call_from_thread(self._render_outline, f"{display_name} Outline", [])

    def _render_outline(self, root_label: str, virtual_nodes: list):
        """Runs on the main thread to instantly update the UI without calculations."""
        tree = self.query_one("#outline_tree", Tree)
        tree.clear()
        from rich.markup import escape
        tree.root.set_label(escape(root_label))
        
        def populate(ui_node, v_nodes):
            for v_node in v_nodes:
                branch = ui_node.add(escape(v_node["label"]))
                populate(branch, v_node["children"])
                
        populate(tree.root, virtual_nodes)
        tree.root.expand_all()
    
    def action_change_directory(self):
        # Load the last saved directory
        current_path = load_last_dir()
            
        def handle_dir_selection(new_path):
            if new_path:
                # Save the new selection
                save_last_dir(new_path)
                
                # Update the tree
                tree = self.query_one("#dir_tree", DirectoryTree)
                tree.path = new_path
                tree.reload()
                
                # Sync process directory
                try:
                    import os
                    os.chdir(new_path)
                except Exception:
                    pass
                
                # Refresh agent UI (the fix from the previous step)
                agent_view = self.query_one("#ai_agent_view", AIAgentView)
                if hasattr(agent_view, "update_status_bar"):
                    agent_view.update_status_bar()
                    
        self.push_screen(DirectoryModal(current_path), handle_dir_selection)
    
    def _update_tree_ui(self, root_label: str, virtual_tree: list):
        """This runs on the main thread and applies the virtual tree to the actual widget"""
        tree = self.query_one("#outline_tree", Tree)
        tree.clear()
        tree.root.set_label(root_label)
        
        def populate(ui_node, v_nodes):
            for v_node in v_nodes:
                branch = ui_node.add(f"{v_node['icon']} {v_node['name']}")
                populate(branch, v_node['children'])
                
        populate(tree.root, virtual_tree)
        tree.root.expand_all()

    def get_venv_root(self) -> str:
        path = os.path.join(os.path.expanduser("~"), ".federate")
        os.makedirs(path, exist_ok=True)
        return path

    def resolve_uv_path(self) -> str:
        import shutil
        sys_path = shutil.which("uv")
        if sys_path:
            return sys_path
        return "uv"

    @work(thread=True)
    def create_custom_venv(self, name: str, py_version: str = None, custom_path: str = None):
        if custom_path:
            new_path = os.path.abspath(custom_path)
            if name and not new_path.endswith(name):
                new_path = os.path.join(new_path, name)
            display_name = name or os.path.basename(new_path)
        else:
            new_path = os.path.join(self.get_venv_root(), name)
            display_name = name

        self.notify(f"Creating venv at {new_path}...", severity="information")
        
        try:
            uv_path = self.resolve_uv_path()
            args = [uv_path, "venv", new_path, "--seed"]
            if py_version:
                args.extend(["--python", py_version])
            
            proc = subprocess.run(args, capture_output=True, text=True)
            if proc.returncode != 0:
                self.notify("uv failed, falling back to standard venv...", severity="warning")
                args = [sys.executable, "-m", "venv", new_path]
                subprocess.run(args, check=True, capture_output=True, text=True)
                
            self.app.call_from_thread(self.activate_venv, display_name, new_path)
            self.app.call_from_thread(self.notify, f"Venv Created & Activated: {display_name}", severity="information")
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Failed to create venv: {e}", severity="error")

    def activate_venv(self, name: str, path: str):
        self.active_venv_name = name
        self.active_venv_path = path
        python_bin = os.path.join(path, "Scripts" if os.name == "nt" else "bin", "python" + (".exe" if os.name == "nt" else ""))
        if "python" in self.run_configs:
            self.run_configs["python"]["executable"] = python_bin

    def action_open_venv_manager(self):
        from execution import VenvManagerModal
        def handle_venv_mgr(result):
            if result == "default":
                default_path = os.path.join(self.get_venv_root(), "defaultVenv")
                self.activate_venv("defaultVenv", default_path)
                self.notify("Reset to Default Venv", severity="information")
            elif isinstance(result, dict):
                if result.get("action") == "switch":
                    target = result["name"]
                    if os.path.isabs(target):
                        name = os.path.basename(target)
                        path = target
                    else:
                        name = target
                        path = os.path.join(self.get_venv_root(), target)
                    self.activate_venv(name, path)
                    self.notify(f"Activated Venv: {name}", severity="information")
                elif result.get("action") == "create":
                    self.create_custom_venv(result.get("name"), result.get("version"), result.get("path"))
                    
        self.app.push_screen(VenvManagerModal(self.get_venv_root(), self.active_venv_name), handle_venv_mgr)

def main():
    import sys
    import shutil
    import subprocess
    import shlex

    if "-record" in sys.argv and shutil.which("asciinema"):
        args_without_record = [arg for arg in sys.argv[1:] if arg != "-record"]
        if sys.argv[0].endswith(".py"):
            cmd_list = [sys.executable, sys.argv[0]] + args_without_record
        else:
            cmd_list = [sys.argv[0]] + args_without_record
        quoted_cmd = " ".join(shlex.quote(arg) for arg in cmd_list)
        res = subprocess.run(["asciinema", "rec", "-c", quoted_cmd, "footage.cast"])
        sys.exit(res.returncode)

    args = [arg for arg in sys.argv[1:] if arg != "-record"]
    initial_file = args[0] if args else None
    app = FEDERaiDE(initial_file=initial_file)
    app.run()

if __name__ == "__main__":
    main()