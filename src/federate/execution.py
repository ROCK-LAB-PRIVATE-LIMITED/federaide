import os
import sys
import re
import subprocess
from rich.markup import escape
from textual import on, work, events
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Select, Input, Button, Static, TabbedContent, TabPane

# Unix PTY Support
try:
    import pty
    import fcntl
    import termios
    import struct
    HAS_PTY = True
except ImportError:
    HAS_PTY = False

# Windows PTY Support
try:
    import pywinpty
    HAS_PYWINPTY = True
except ImportError:
    HAS_PYWINPTY = False

# Strip ANSI codes so rendering doesn't print garbage. We manually intercept specific cursor commands.
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

DEFAULT_RUN_CONFIGS = {
    "python": {"executable": "python", "flags": "-u \"{file}\""},
    "go": {"executable": "go", "flags": "run \"{file}\""},
    "c": {"executable": "gcc", "flags": "\"{file}\" -o \"{file_no_ext}\" && \"{file_no_ext}\""},
    "cpp": {"executable": "g++", "flags": "\"{file}\" -o \"{file_no_ext}\" && \"{file_no_ext}\""},
    "rust": {"executable": "rustc", "flags": "\"{file}\" && \"{file_no_ext}\""},
    "javascript": {"executable": "node", "flags": "\"{file}\""},
    "html": {"executable": "echo", "flags": "\"HTML execution is currently not supported via CLI IDE\""},
    "julia": {"executable": "julia", "flags": "\"{file}\""},
    "fortran": {"executable": "gfortran", "flags": "\"{file}\" -o \"{file_no_ext}\" && \"{file_no_ext}\""},
    "nim": {"executable": "nim", "flags": "c -r \"{file}\""},
    "zig": {"executable": "zig", "flags": "run \"{file}\""},
    "bash": {"executable": "bash" if os.name != "nt" else "sh", "flags": "\"{file}\""},
    "batch": {"executable": "cmd.exe" if os.name == "nt" else "echo", "flags": "/c \"{file}\"" if os.name == "nt" else "\"Batch files are not natively supported on this OS.\""},
    "powershell": {"executable": "powershell" if os.name == "nt" else "pwsh", "flags": "-ExecutionPolicy Bypass -File \"{file}\""},
}

class SettingsModal(ModalScreen[dict]):
    """Modal to configure run parameters and executables for supported languages."""
    
    DEFAULT_CSS = """
    SettingsModal { align: center middle; background: $background 60%; }
    #settings_dialog { width: 60; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    .form-row { margin-bottom: 1; }
    #btn_container { width: 100%; align: right middle; margin-top: 1; }
    Button { margin-left: 1; }
    """

    def __init__(self, configs: dict):
        super().__init__()
        self.configs = {k: v.copy() for k, v in configs.items()}
        self.current_lang = "python"

    def compose(self) -> ComposeResult:
        with Vertical(id="settings_dialog"):
            yield Label(" Execution Settings", classes="pane_title")
            yield Select(((k, k) for k in self.configs.keys()), value=self.current_lang, id="lang_select")
            yield Label("Executable Path:")
            yield Input(self.configs[self.current_lang].get("executable", ""), id="exec_input", classes="form-row")
            yield Label("Parameters / Flags (Variables: {file}, {file_no_ext}):")
            yield Input(self.configs[self.current_lang].get("flags", ""), id="flags_input", classes="form-row")
            with Horizontal(id="btn_container"):
                yield Button("Save", id="save_btn", variant="success")
                yield Button("Cancel", id="cancel_btn", variant="error")

    @on(Select.Changed, "#lang_select")
    def on_lang_changed(self, event: Select.Changed):
        self.current_lang = event.value
        if self.current_lang in self.configs:
            self.query_one("#exec_input", Input).value = self.configs[self.current_lang].get("executable", "")
            self.query_one("#flags_input", Input).value = self.configs[self.current_lang].get("flags", "")

    @on(Input.Changed, "#exec_input")
    def on_exec_changed(self, event: Input.Changed):
        if self.current_lang:
            self.configs[self.current_lang]["executable"] = event.value

    @on(Input.Changed, "#flags_input")
    def on_flags_changed(self, event: Input.Changed):
        if self.current_lang:
            self.configs[self.current_lang]["flags"] = event.value

    @on(Button.Pressed, "#save_btn")
    def save(self):
        self.dismiss(self.configs)

    @on(Button.Pressed, "#cancel_btn")
    def cancel(self):
        self.dismiss(None)


class TerminalEmulator(VerticalScroll, can_focus=True):
    """A seamless single-view terminal emulator capable of tracking cursor movement (\r, \b) natively."""
    
    DEFAULT_CSS = """
    TerminalEmulator {
        background: $surface;
        color: $text;
        width: 100%;
        height: 1fr; /* Force fill */
        border: none;
        padding: 0 1;
    }
    #render_text { 
        width: 100%; 
        height: auto; /* Allow text to grow */
        padding-bottom: 2;
    }
    """

    def __init__(self, file_path: str, executable: str, flags: str, venv_path: str = None, **kwargs):
        super().__init__(**kwargs)
        self.file_path = file_path
        self.executable = executable
        self.flags = flags
        self.venv_path = venv_path
        
        self.process = None
        self.pty_master = None
        self.winpty_proc = None
        
        # Virtual Buffer State
        self.lines =[]
        self.current_line = ""
        self.cursor_col = 0
        self.max_lines = 1000
        self._process_active = True

    def compose(self) -> ComposeResult:
        yield Static(id="render_text", markup=False)

    def on_mount(self):
        self.execute_and_read()

    @work(exclusive=True, thread=True)
    def execute_and_read(self):
        file_no_ext = os.path.splitext(self.file_path)[0]
        try:
            formatted_flags = self.flags.format(file=self.file_path, file_no_ext=file_no_ext)
            run_cmd = f"\"{self.executable}\" {formatted_flags}"
        except KeyError as e:
            self.app.call_from_thread(self._write_raw, f"Configuration error: invalid variable {e}\n")
            return

        # Resolve user's actual macOS shell (defaulting to bash)
        shell_cmd = "cmd.exe" if sys.platform == "win32" else os.environ.get("SHELL", "bash")
        self.app.call_from_thread(self._write_raw, f" Launching Shell: {shell_cmd}\n{'-'*40}\n")
        
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        
        newline = "\r\n" if sys.platform == "win32" else "\n"
        startup_payload = ""
        if self.venv_path:
            if sys.platform == "win32":
                activate_script = os.path.join(self.venv_path, "Scripts", "activate.bat")
                startup_payload += f"call \"{activate_script}\"{newline}"
            else:
                activate_script = os.path.join(self.venv_path, "bin", "activate")
                startup_payload += f"source \"{activate_script}\"{newline}"
                
        startup_payload += f"cd \"{os.path.dirname(self.file_path)}\"{newline}"
        startup_payload += f"{run_cmd}{newline}"

        try:
            if sys.platform == "win32" and HAS_PYWINPTY:
                self.winpty_proc = pywinpty.PTY(120, 30)
                self.winpty_proc.spawn(None, cmdline=shell_cmd, cwd=os.path.dirname(self.file_path), env=env)
                self.winpty_proc.write(startup_payload)
                
                while self._process_active:
                    try:
                        data = self.winpty_proc.read(1024)
                        if not data: break
                        self.app.call_from_thread(self.process_output, data)
                    except pywinpty.PTYError:
                        break
            
            elif HAS_PTY:
                master, slave = pty.openpty()
                self.pty_master = master
                winsize = struct.pack("HHHH", 30, 120, 0, 0)
                fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)
                
                # Direct invocation (shell=False) passing '-i' (interactive) keeps macOS zsh alive
                self.process = subprocess.Popen(
                    [shell_cmd, "-i"], shell=False, stdin=slave, stdout=slave, stderr=slave, 
                    cwd=os.path.dirname(self.file_path), env=env
                )
                os.close(slave) 
                
                os.write(master, startup_payload.encode('utf-8'))

                while self._process_active:
                    try:
                        data = os.read(master, 1024)
                        if not data: break
                        text = data.decode('utf-8', errors='replace')
                        self.app.call_from_thread(self.process_output, text)
                    except OSError:
                        break
                self.process.wait()

            else:
                self.app.call_from_thread(self._write_raw, "[WARNING: PTY libraries missing. Interactive execution limited.]\n")
                self.process = subprocess.Popen(
                    shell_cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                    cwd=os.path.dirname(self.file_path), env=env
                )
                self._stdin = self.process.stdin
                self._stdin.write(startup_payload.encode('utf-8'))
                self._stdin.flush()
                
                while self._process_active:
                    data = self.process.stdout.read(1)
                    if not data: break
                    text = data.decode('utf-8', errors='replace')
                    self.app.call_from_thread(self.process_output, text)
                self.process.wait()

            return_code = self.process.returncode if self.process else getattr(self.winpty_proc, 'get_exitstatus', lambda: 0)()
            self.app.call_from_thread(self._write_raw, f"\n{'-'*40}\nShell session finished with exit code {return_code}\n")

        except Exception as e:
            self.app.call_from_thread(self._write_raw, f"\nExecution error: {e}\n")
        finally:
            self._process_active = False
            self.app.call_from_thread(self.update_display)
            if self.pty_master is not None:
                try: os.close(self.pty_master)
                except OSError: pass

    def _write_raw(self, text: str):
        self.process_output(text)

    def process_output(self, text: str):
        text = text.replace('\x1b[K', '\x00').replace('\033[K', '\x00') # Convert Clear to EOL to Null byte
        text = ANSI_ESCAPE.sub('', text)

        for char in text:
            if char == '\x00':
                self.current_line = self.current_line[:self.cursor_col]
            elif char == '\r':
                self.cursor_col = 0
            elif char == '\n':
                self.lines.append(self.current_line)
                self.current_line = ""
                self.cursor_col = 0
            elif char == '\b':
                self.cursor_col = max(0, self.cursor_col - 1)
            elif char == '\t':
                spaces = 4 - (self.cursor_col % 4)
                for _ in range(spaces): self._insert_char(' ')
            else:
                self._insert_char(char)
                
        if len(self.lines) > self.max_lines:
            self.lines = self.lines[-self.max_lines:]
            
        self.update_display()

    def _insert_char(self, char: str):
        if len(self.current_line) < self.cursor_col:
            self.current_line += " " * (self.cursor_col - len(self.current_line))
        self.current_line = self.current_line[:self.cursor_col] + char + self.current_line[self.cursor_col+1:]
        self.cursor_col += 1

    def update_display(self):
        display = "\n".join(self.lines)
        if display: display += "\n"
        
        if self._process_active:
            current = self.current_line[:self.cursor_col] + "█" + self.current_line[self.cursor_col+1:]
            self.query_one("#render_text", Static).update(display + current)
        else:
            self.query_one("#render_text", Static).update(display + self.current_line)
        self.scroll_end(animate=False)

    @on(events.Key)
    def on_key(self, event: events.Key):
        if not self._process_active: return
        
        char_map = {"enter": '\r', "backspace": '\x7f', "tab": '\t'}
        byte_map = {"enter": b'\r', "backspace": b'\x7f', "tab": b'\t'}

        if HAS_PYWINPTY and self.winpty_proc:
            if event.character: self.winpty_proc.write(event.character)
            elif event.key in char_map: self.winpty_proc.write(char_map[event.key])
            event.prevent_default()
            
        elif HAS_PTY and self.pty_master is not None:
            if event.character: os.write(self.pty_master, event.character.encode('utf-8'))
            elif event.key in byte_map: os.write(self.pty_master, byte_map[event.key])
            event.prevent_default()

    def terminate(self):
        if self._process_active:
            self._process_active = False
            try:
                if self.process: self.process.terminate()
                if self.winpty_proc: del self.winpty_proc
                self._write_raw("\n[ Process terminated by user.]\n")
            except Exception: pass


class ExecutionManager(Vertical):
    """A Dashboard view managing multiple running scripts inside Tabs."""

    def compose(self) -> ComposeResult:
        yield Label(" ACTIVE EXECUTIONS", classes="pane_title")
        with TabbedContent(id="exec_tabs"):
            yield TabPane("Idle", Label("\n   No active executions.\n   Press Ctrl+R in the editor to run a file.", id="idle_label"), id="idle_pane")

    def start_run(self, file_path: str, executable: str, flags: str, run_id: int, venv_path: str = None):
        tabs = self.query_one("#exec_tabs", TabbedContent)
        
        try: tabs.remove_pane("idle_pane")
        except Exception: pass
        
        pane_id = f"run_pane_{run_id}"
        term = TerminalEmulator(file_path, executable, flags, venv_path=venv_path, id=f"term_{run_id}")
        pane = TabPane(f"{os.path.basename(file_path)} #{run_id}", term, id=pane_id)
        
        tabs.add_pane(pane)
        tabs.active = pane_id
        term.focus()

    def terminate_active(self) -> bool:
        """Terminates active tab. Returns True if there are still other runs alive."""
        tabs = self.query_one("#exec_tabs", TabbedContent)
        active = tabs.active
        
        if active and active.startswith("run_pane_"):
            try:
                term = self.query_one(f"#{active}", TabPane).query_children(TerminalEmulator).first()
                term.terminate()
            except Exception: pass
            
            # Synchronously check active run panes prior to scheduled removal
            run_panes = [pane for pane in tabs.query(TabPane) if pane.id.startswith("run_pane_")]
            is_last = len(run_panes) <= 1
            
            try:
                tabs.remove_pane(active)
            except Exception: pass
            
            # If this was the last running script, restore the Idle state and signal autoclose
            if is_last:
                tabs.add_pane(TabPane("Idle", Label("\n   No active executions.\n   Press Ctrl+R in the editor to run a file.", id="idle_label"), id="idle_pane"))
                tabs.active = "idle_pane"
                return False
            return True
        return False
        
class VenvManagerModal(ModalScreen[str]):
    DEFAULT_CSS = """
    VenvManagerModal { align: center middle; background: $background 60%; }
    #venv_dialog { width: 60; height: auto; border: round $primary; background: $surface; padding: 1 2; }
    .form_row { margin-bottom: 1; }
    .buttons { height: auto; align: right middle; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    """

    def __init__(self, venv_root: str, current_active: str):
        super().__init__()
        self.venv_root = venv_root
        self.current_active = current_active

    def compose(self) -> ComposeResult:
        options = []
        default_path = os.path.join(self.venv_root, "defaultVenv")
        if os.path.exists(default_path):
            options.append(("defaultVenv", "defaultVenv"))
        
        # 1. Auto-detect local workspace virtual environments (.venv or venv)
        try:
            from toolbox import CURRENT_APP
            if CURRENT_APP:
                workspace_dir = str(CURRENT_APP.query_one("#dir_tree").path)
                for local_name in [".venv", "venv"]:
                    local_path = os.path.abspath(os.path.join(workspace_dir, local_name))
                    if os.path.isdir(local_path):
                        options.append((f"Workspace {local_name} ({local_path})", local_path))
        except Exception: pass

        # 2. List global venvs inside .federate
        if os.path.exists(self.venv_root):
            try:
                for d in sorted(os.listdir(self.venv_root)):
                    if d != "defaultVenv" and os.path.isdir(os.path.join(self.venv_root, d)):
                        options.append((f"Global: {d}", d))
            except Exception: pass
        
        with Vertical(id="venv_dialog"):
            yield Label(" UV Virtual Env Manager", classes="pane_title")
            yield Label(f"Current Active: [bold green]{self.current_active}[/]")
            yield Select(options, id="venv_select", prompt="Switch Active Venv...")
            
            yield Label("\nOr Activate Existing Venv Path:")
            yield Input(placeholder="/path/to/existing/venv", id="direct_venv_path", classes="form_row")
            
            yield Label("\nCreate New Venv:")
            yield Input(placeholder="Venv Name", id="new_venv_name", classes="form_row")
            yield Input(placeholder="Python Version (optional, e.g. 3.11)", id="new_venv_version", classes="form_row")
            yield Input(placeholder="Absolute Create Path (optional, defaults to .federate)", id="new_venv_path", classes="form_row")
            
            with Horizontal(classes="buttons"):
                yield Button("Create", id="create_btn", variant="success")
                yield Button("Default", id="default_btn", variant="warning")
                yield Button("Cancel", id="cancel_btn", variant="error")

    @on(Button.Pressed)
    def handle_buttons(self, event: Button.Pressed):
        btn_id = event.button.id
        if btn_id == "cancel_btn":
            self.dismiss(None)
        elif btn_id == "default_btn":
            self.dismiss("default")
        elif btn_id == "create_btn":
            name = self.query_one("#new_venv_name", Input).value.strip()
            version = self.query_one("#new_venv_version", Input).value.strip() or None
            path = self.query_one("#new_venv_path", Input).value.strip() or None
            if name or path:
                self.dismiss({"action": "create", "name": name, "version": version, "path": path})
            else:
                self.notify("Please enter a name or path to create.", severity="error")
                
    @on(Input.Submitted, "#direct_venv_path")
    def on_direct_path_submitted(self):
        direct_path = self.query_one("#direct_venv_path", Input).value.strip()
        if direct_path:
            resolved = os.path.abspath(direct_path)
            if os.path.isdir(resolved):
                self.dismiss({"action": "switch", "name": resolved})
            else:
                self.notify("The specified path is not a valid directory.", severity="error")

    @on(Select.Changed, "#venv_select")
    def on_select_changed(self, event: Select.Changed):
        if event.value != Select.BLANK:
            self.dismiss({"action": "switch", "name": str(event.value)})