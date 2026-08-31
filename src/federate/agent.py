"""
    FEDERaiDE is a multi-agent multi-modal automation and orchestration harness.
    Copyright (C) 2026  ROCK LAB PRIVATE LIMITED

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import os
import sys
import glob
import threading
import subprocess
import requests
import fnmatch
import re
import json
import time
from datetime import datetime
from pathlib import Path

from markdownify import markdownify as md

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.binding import Binding
from textual.widgets import RichLog, Input, Label, Button, Select, Static, ProgressBar, Checkbox, ListView, ListItem, TextArea, Switch
from textual.screen import ModalScreen
from textual import work, on
from textual.message import Message
from textual import events

from rich.markup import escape
from rich.markdown import Markdown
from rich.text import Text
from rich.rule import Rule
from rich.spinner import Spinner

from typing import Any, List, Dict, Optional

try:
    import tiktoken
    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False

import toolbox
import agent_core
from commands import ChatSuggester, process_shell_command, process_slash_command, handle_ampersand_commands

from audio_handler import TTSManager, STTManager, AudioConfigModal
from chatgpt_auth import is_chatgpt_oauth_agent, has_valid_chatgpt_token, ChatGPTAuthModal
from telegram_handler import TelegramManager
from orchestration import AgentManager, SessionManager, AgentConfig, HistoryMessage, ScheduleManager


def parse_resume_index(argv=None) -> Optional[int]:
    if argv is None:
        argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-r", "--resume"):
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                return int(argv[i + 1])
            return 1
        elif arg.startswith("-r=") or arg.startswith("--resume="):
            val = arg.split("=", 1)[1]
            if val.isdigit():
                return int(val)
            return 1
        i += 1
    return None

def get_installed_version() -> str:
    try:
        import importlib.metadata
        return importlib.metadata.version("federaide")
    except Exception:
        try:
            import federate
            return getattr(federate, "__version__", "1.2.9")
        except Exception:
            return "1.2.9"

SLASH_COMMAND_DESCS = {
    "/tools": "List status of all available AI tools",
    "/update": "Check for software updates and view release notes",
    "/version": "Show currently installed Federate version",
    "/arm": "Toggle ARM/SAFE (Execute/Plan) mode",
    "/config": "Open active agent configuration",
    "/safe": "Lock system to SAFE (Plan, read-only) mode",
    "/init": "Create Federate.md project instructions file",
    "/compress": "Compress chat context to save tokens",
    "/copy": "Copy last AI response to system clipboard",
    "/directory": "Open interactive directory picker",
    "/dir": "Open interactive directory picker",
    "/theme": "Set UI theme (e.g. /theme tokyo-night, /theme nord, /theme monokai)",
    "/tts": "Toggle Text-to-Speech (TTS) voice output",
    "/stt": "Toggle Speech-to-Text (STT) hotword listening",
    "/readback": "Read back the last AI response with TTS",
    "/speech": "Open Audio/Voice configuration modal",
    "/telegram": "Configure Telegram Bot integration",
    "/select_agent": "Switch the active host agent",
    "/clear_all": "Wipe memory and history of all agents",
    "/skills": "List all passive and active skills for the active agent",
    "/settings": "Open global harness settings modal",
    "/help": "Show this detailed help menu",
    "/backstory": "Force update and translate all agent backstories",
    "/consolidate": "Consolidate, summarize, and prune core memorylets",
    "/schedule": "Open the automated daily task scheduler menu",
    "/dpi": "Set the image DPI resolution for generated PDF documents"
}

# --- MODAL SCREENS ---

class ToolConfirmationModal(ModalScreen[str]):
    DEFAULT_CSS = """
    ToolConfirmationModal { align: center middle; background: $background 60%; }
    #confirm_dialog { width: 70; height: 75%; border: thick $warning; background: $surface; padding: 1 2; }
    #args_scroll { margin: 1 0; height: 1fr; border: round $primary; background: $boost; padding: 1 2; }
    .buttons { height: auto; align: right middle; margin-top: 0; }
    .buttons Button { margin-left: 1; }
    """
    def __init__(self, tool_name: str, arguments: dict, agent_name: str = "Agent"):
        super().__init__()
        self.tool_name = tool_name
        self.arguments = arguments
        self.agent_name = agent_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm_dialog"):
            yield Label(f" Tool Authorization: [bold yellow]{self.tool_name}[/] requested by [bold cyan]{self.agent_name}[/]", classes="pane_title")
            yield Label("[dim]Verify the requested arguments before executing:[/dim]")
            
            with VerticalScroll(id="args_scroll"):
                try:
                    formatted_args = json.dumps(self.arguments, indent=4)
                except Exception:
                    formatted_args = str(self.arguments)
                yield Static(formatted_args, markup=False)
                
            with Horizontal(classes="buttons"):
                yield Button("Approve (Execute)", id="approve", variant="success")
                yield Button("Reject", id="reject", variant="warning")
                yield Button("Stop", id="stop", variant="error")

    def on_mount(self):
        self.query_one("#approve").focus()

    @on(Button.Pressed, "#approve")
    def on_approve(self):
        self.dismiss("approve")

    @on(Button.Pressed, "#reject")
    def on_reject(self):
        self.dismiss("reject")

    @on(Button.Pressed, "#stop")
    def on_stop(self):
        self.dismiss("stop")
        
class ClarificationModal(ModalScreen[str]):
    DEFAULT_CSS = """
    ClarificationModal { align: center middle; background: $background 60%; }
    #clarify_dialog { width: 60; max-height: 80%; border: thick $primary; background: $surface; padding: 1 2; }
    #options_list { margin: 1 0; height: auto; max-height: 15; border: round $primary; background: $boost; }
    #options_list ListItem { padding: 1; border-bottom: solid $primary 10%; }
    #options_list ListItem:hover { background: $accent 20%; }
    #clarify_input { margin-top: 1; border: tall $primary; }
    .buttons { height: auto; align: right middle; margin-top: 1; }
    """
    def __init__(self, options: Optional[List[str]] = None, agent_name: str = "Agent"):
        super().__init__()
        self.options = options or []
        self.agent_name = agent_name

    def compose(self) -> ComposeResult:
        with Vertical(id="clarify_dialog"):
            yield Label(f" Clarification: [bold cyan]{self.agent_name}[/]", classes="pane_title")
            if self.options:
                yield Label(f"[dim]Select an option for {self.agent_name} or type below:[/dim]")
                with ListView(id="options_list"):
                    for opt in self.options:
                        yield ListItem(Label(opt))
            else:
                yield Label(f"[dim]{self.agent_name} needs more information:[/dim]")
            
            yield Input(placeholder="Type your response and press Enter...", id="clarify_input")
            
            with Horizontal(classes="buttons"):
                yield Button("Cancel (Abort)", id="cancel", variant="error")

    def on_mount(self):
        self.query_one("#clarify_input").focus()

    @on(ListView.Selected)
    def on_option_selected(self, event: ListView.Selected):
        try:
            idx = event.list_view.index
            if idx is not None and 0 <= idx < len(self.options):
                self.dismiss(self.options[idx])
        except:
            pass

    @on(Input.Submitted, "#clarify_input")
    def on_input_submitted(self, event: Input.Submitted):
        if event.value.strip():
            self.dismiss(event.value.strip())

    @on(Button.Pressed, "#cancel")
    def on_cancel(self):
        self.dismiss("")

class ChatLoadModal(ModalScreen[str]):
    DEFAULT_CSS = """
    ChatLoadModal { align: center middle; background: $background 60%; }
    #chat_load_dialog { width: 60; height: 70%; border: thick $primary; background: $surface; padding: 1 2; }
    #chat_list { margin: 1 0; height: 1fr; border: round $primary; background: $boost; }
    #chat_list Button { width: 100%; margin-bottom: 0; border: none; content-align: left middle; }
    """
    def on_mount(self) -> None:
        if self.query("#chat_list Button"):
            self.query("#chat_list Button").first().focus()
        else:
            self.query_one("#cancel").focus()

    def compose(self) -> ComposeResult:
        files = sorted(glob.glob(toolbox.get_storage_path("sessions", "*.json")), key=os.path.getmtime, reverse=True)
        name_map = agent_core.get_session_name_map()
        files = [
            f for f in files 
            if (parts := os.path.basename(f).replace(".json", "").split("_")) 
            and len(parts) >= 2 
            and f"{parts[0]}_{parts[1]}" in name_map
        ]
        self.file_map = {f"c_{i}": f for i, f in enumerate(files)}
        with Vertical(id="chat_load_dialog"):
            yield Label(" Load Session History", classes="pane_title")
            with VerticalScroll(id="chat_list"):
                if not files: yield Label("  No sessions found.")
                for btn_id, path in self.file_map.items():
                    base = os.path.basename(path).replace(".json", "")
                    parts = base.split("_")
                    session_id = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else ""
                    agent_name = " ".join(parts[2:]) if len(parts) >= 3 else ""
                    
                    friendly_name = name_map.get(session_id)
                    if friendly_name:
                        btn_label = f" {friendly_name} ({agent_name})"
                    else:
                        btn_label = f" {base}"
                        
                    yield Button(btn_label, id=btn_id)
            yield Button("Cancel", id="cancel", variant="error")

    @on(Button.Pressed)
    def handle_click(self, event: Button.Pressed):
        if event.button.id == "cancel": self.dismiss(None)
        else: self.dismiss(self.file_map.get(event.button.id))

class SwitchAgentModal(ModalScreen[dict]):
    DEFAULT_CSS = """
    SwitchAgentModal { align: center middle; background: $background 60%; }
    #switch_dialog { width: 45; max-height: 70%; border: thick $primary; background: $surface; padding: 1 2; }
    #agent_list { margin: 1 0; height: auto; max-height: 12; overflow-y: scroll; border: round $primary; background: $boost; }
    #agent_list Button { width: 100%; margin-bottom: 0; border: none; content-align: left middle; }
    """
    def __init__(self, agents: List[str], current_default: str):
        super().__init__()
        self.agents = agents
        self.current_default = current_default

    def compose(self) -> ComposeResult:
        with Vertical(id="switch_dialog"):
            yield Label(" Switch Active Agent", classes="pane_title")
            yield Checkbox("Set as Default Agent", id="set_default")
            yield Label(f"[dim]Default: {self.current_default}[/dim]", classes="status_center")
            with VerticalScroll(id="agent_list"):
                for name in self.agents:
                    yield Button(f" {name}", id=f"sel_{name}")
            yield Button("Cancel", id="cancel", variant="error")

    @on(Button.Pressed)
    def handle_click(self, event: Button.Pressed):
        if event.button.id == "cancel": self.dismiss(None)
        elif event.button.id.startswith("sel_"):
            self.dismiss({"name": event.button.id[4:], "default": self.query_one("#set_default", Checkbox).value})

class ChatManagerModal(ModalScreen[str]):
    DEFAULT_CSS = """
    ChatManagerModal { align: center middle; background: $background 60%; }
    #chat_mgr_dialog { width: 40; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    #chat_mgr_dialog Button { width: 100%; margin-bottom: 1; }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="chat_mgr_dialog"):
            yield Label(" Session Manager", classes="pane_title")
            yield Button(" New Chat Session", id="new_session", variant="success")
            yield Button(" Load Saved Session", id="load_chat", variant="primary")
            yield Button(" Cancel", id="cancel", variant="error")
    @on(Button.Pressed)
    def handle_click(self, event: Button.Pressed): self.dismiss(event.button.id)

class KeyringUnlockModal(ModalScreen[tuple]):
    DEFAULT_CSS = """
    KeyringUnlockModal { align: center middle; background: $background 60%; }
    #unlock_dialog { width: 70; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    #unlock_dialog Input { margin-bottom: 1; }
    #unlock_dialog .pane_title { background: $primary; color: $text; padding: 0 1; margin-bottom: 1; text-style: bold; width: 100%; text-align: center; }
    .warning_text { color: $error; margin-bottom: 1; text-style: italic; text-wrap: wrap; height: auto; width: 100%; }
    .modal_buttons { layout: horizontal; height: auto; margin-top: 1; padding: 0 1; }
    .modal_buttons Button { margin-right: 1; }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="unlock_dialog"):
            yield Label(" Keyring Locked", classes="pane_title")
            yield Label("Please enter your Master Password:")
            yield Input(placeholder="Master Password", id="master_pwd", password=True)
            yield Label(" Resetting or setting a new password will permanently delete any previously saved keys in this keyring.", classes="warning_text")
            with Horizontal(classes="modal_buttons"):
                yield Button("Unlock", id="unlock_btn", variant="success")
                yield Button("Set New / Reset", id="reset_btn", variant="warning")
                yield Button("Cancel", id="cancel_btn", variant="error")

    @on(Button.Pressed, "#unlock_btn")
    def unlock(self):
        pwd = self.query_one("#master_pwd").value
        self.dismiss(("unlock", pwd))

    @on(Button.Pressed, "#reset_btn")
    def reset(self):
        pwd = self.query_one("#master_pwd").value
        self.dismiss(("reset", pwd))

    @on(Button.Pressed, "#cancel_btn")
    def cancel(self):
        self.dismiss(None)

class CopyrightWarningModal(ModalScreen[bool]):
    DEFAULT_CSS = """
    CopyrightWarningModal { align: center middle; background: $background 60%; }
    #warning_dialog { width: 70; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    .warning_content { margin: 1 0; text-style: bold; text-wrap: wrap; height: auto; width: 100%; color: $error; }
    .buttons { height: auto; align: right middle; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="warning_dialog"):
            yield Label(" Copyright Warning", classes="pane_title")
            yield Label(
                "You are attempting to include potentially copyrighted images in the research output. "
                "You are solely responsible for any and all copyright violations that may result if you share the document publicly or use the document commercially. "
                "This feature is meant for personal usage only. Rock Lab Private Limited accepts no legal liability and you use this at your sole risk. "
                "All legal issues arising from using this feature are between the user and the (potential) copyright holders. "
                "By choosing to use this feature, you proceed at your own risk, legal or otherwise.",
                classes="warning_content"
            )
            with Horizontal(classes="buttons"):
                yield Button("I Accept & Proceed", id="btn_accept", variant="error")
                yield Button("Cancel", id="btn_cancel", variant="success")

    def on_mount(self):
        self.query_one("#btn_cancel").focus()

    @on(Button.Pressed, "#btn_accept")
    def on_accept(self):
        self.dismiss(True)

    @on(Button.Pressed, "#btn_cancel")
    def on_cancel(self):
        self.dismiss(False)


class UpdateModal(ModalScreen[str]):
    DEFAULT_CSS = """
    UpdateModal { align: center middle; background: $background 60%; }
    #update_dialog { width: 75; height: 80%; border: thick $primary; background: $surface; padding: 1 2; }
    #update_notes_scroll { margin: 1 0; height: 1fr; border: round $accent; background: $boost; padding: 1 2; }
    .update_status_msg { margin: 1 0; text-style: bold; text-align: center; }
    .buttons { height: auto; align: right middle; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    """

    def __init__(self, current_ver: str, latest_ver: str, release_notes: str, **kwargs):
        super().__init__(**kwargs)
        self.current_ver = current_ver
        self.latest_ver = latest_ver
        self.release_notes = release_notes
        self.is_updating = False

    def compose(self) -> ComposeResult:
        with Vertical(id="update_dialog"):
            yield Label(f" Software Update Available: v{self.current_ver} ➔ v{self.latest_ver}", classes="pane_title")
            yield Label(f"Release Notes for v{self.latest_ver}:", classes="field_label")
            with VerticalScroll(id="update_notes_scroll"):
                if self.release_notes:
                    yield Static(Markdown(self.release_notes))
                else:
                    yield Label("[dim]No release notes available.[/dim]")
            
            yield Label("", id="update_status", classes="update_status_msg")
            
            with Horizontal(classes="buttons", id="update_btn_container"):
                yield Button("Update Now", id="btn_update_now", variant="success")
                yield Button("Skip Version", id="btn_skip_ver", variant="warning")
                yield Button("Remind Later", id="btn_defer_update", variant="primary")
                yield Button("Close", id="btn_close_update", variant="error")

    def on_mount(self):
        self.query_one("#btn_update_now").focus()

    @on(Button.Pressed)
    def handle_buttons(self, event: Button.Pressed):
        btn_id = event.button.id
        if btn_id in ("btn_close_update", "btn_cancel"):
            try:
                self.dismiss("close")
            except Exception:
                pass
            try:
                self.app.pop_screen()
            except Exception:
                pass
        elif btn_id == "btn_defer_update":
            try: self.dismiss("defer")
            except Exception: self.app.pop_screen()
        elif btn_id == "btn_skip_ver":
            try: self.dismiss("skip")
            except Exception: self.app.pop_screen()
        elif btn_id == "btn_update_now":
            self.start_update_process()

    def start_update_process(self):
        if self.is_updating:
            return
        self.is_updating = True
        
        status_lbl = self.query_one("#update_status", Label)
        status_lbl.update("[bold yellow] Exiting application to launch terminal updater...[/bold yellow]")
        
        try:
            for btn in self.query("#update_btn_container Button"):
                btn.disabled = True
        except Exception:
            pass
            
        def _run_external_update():
            time.sleep(1.0)  
            print('\033[?25h', end='', flush=True) 
            
            if os.name == "nt" or sys.platform == "win32":
                ps_cmd = (
                    "try { irm https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/update.ps1 | iex } "
                    "catch { irm https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/install.ps1 | iex }; "
                    "Write-Host 'Update process finished. Restarting...'; "
                    "Start-Sleep -Seconds 2; "
                    "federaide"
                )
                cmd = f'cmd.exe /c "ping 127.0.0.1 -n 2 > nul & powershell.exe -ExecutionPolicy Bypass -Command \"{ps_cmd}\"'
                subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
                os._exit(0)
            else:
                sh_cmd = (
                    "echo -e '\\n\\033[1;36m[ FEDERaiDE Updater ]\\033[0m Starting system update...\\n'; "
                    "(curl -LsSf https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/update.sh | bash) || "
                    "(curl -LsSf https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/federaide/main/install.sh | bash); "
                    "exec federaide"
                )
                os.execvp("bash", ["bash", "-c", sh_cmd])

        threading.Thread(target=_run_external_update, daemon=False).start()
        self.app.exit()

class GlobalSettingsModal(ModalScreen[str]):
    DEFAULT_CSS = """
    GlobalSettingsModal { align: center middle; background: $background 60%; }
    #global_config_dialog { width: 75; height: 90%; border: round $primary; background: $surface; padding: 0; }
    #global_scroll { padding: 1 2; }
    .section_label { background: $primary; color: $text; padding: 0 1; margin-top: 1; text-style: bold; width: 100%; }
    .field_container { margin-top: 1; height: auto; width: 100%; }
    .field_label { color: $text; text-style: bold; margin-bottom: 0; width: 100%; }
    .field_help { color: $text-muted; text-style: italic; margin-bottom: 0; text-wrap: wrap; height: auto; width: 100%; }
    Input { width: 100%; margin-top: 0; margin-bottom: 1; border: round $accent; }
    #global_actions { margin-top: 1; align: right middle; height: auto; border-top: solid $primary; padding: 1 2; }
    #global_actions Button { margin-left: 1; }
    """
    
    def compose(self) -> ComposeResult:
        with Vertical(id="global_config_dialog"):
            yield Label(" Global Harness Settings", classes="pane_title")
            
            with VerticalScroll(id="global_scroll"):
                yield Label("User Identity", classes="section_label")
                
                with Vertical(classes="field_container"):
                    yield Label("User Name", classes="field_label")
                    yield Label("Display name for user messages in the chat panel.", classes="field_help")
                    yield Input(id="user_name", placeholder="User")
                    
                with Vertical(classes="field_container"):
                    yield Label("User Color", classes="field_label")
                    yield Label("Color formatting for user label in the chat panel (e.g. #dda0dd, purple).", classes="field_help")
                    yield Input(id="user_color", placeholder="#dda0dd")

                with Vertical(classes="field_container"):
                    yield Label("Model Name Color", classes="field_label")
                    yield Label("Color formatting for active model names in the bottom status bar (e.g. #ffd700, yellow, orange, green).", classes="field_help")
                    yield Input(id="model_color", placeholder="#ffd700")

                yield Label("Search & Scraping Parameters", classes="section_label")
                
                with Vertical(classes="field_container"):
                    yield Label("Search Pacing Delay (Seconds)", classes="field_label")
                    yield Label("Baseline delay in seconds between consecutive web searches to protect your IP from rate limits.", classes="field_help")
                    yield Input(id="search_pacing_delay", placeholder="e.g. 65.0")
                    
                with Vertical(classes="field_container"):
                    yield Label("Max Web Search Results", classes="field_label")
                    yield Label("The maximum number of search result snippets requested per query (typically 5 to 20).", classes="field_help")
                    yield Input(id="max_search_results", placeholder="e.g. 10")
                    
                with Vertical(classes="field_container"):
                    yield Label("Scraper Max Size Limit (Bytes)", classes="field_label")
                    yield Label("Maximum download limit for a scraped web page to prevent fetching massive documents.", classes="field_help")
                    yield Input(id="scraper_max_bytes", placeholder="e.g. 1000000")
                    
                with Vertical(classes="field_container"):
                    yield Label("Scraper Timeout (Seconds)", classes="field_label")
                    yield Label("Maximum download time in seconds allowed for fetching/scraping web pages.", classes="field_help")
                    yield Input(id="scraper_timeout", placeholder="e.g. 120.0")

                yield Label("API Connection & Error Recovery", classes="section_label")
                
                with Vertical(classes="field_container"):
                    yield Checkbox("Check for updates automatically on launch", id="autoupdate_on_launch")

                with Vertical(classes="field_container"):
                    yield Label("Max Connection Retries", classes="field_label")
                    yield Label("Maximum retry attempts for dropped, uncompleted, or rate-limited LLM API calls.", classes="field_help")
                    yield Input(id="max_api_retries", placeholder="e.g. 20")
                    
                with Vertical(classes="field_container"):
                    yield Label("Dropped Connection Wait (Seconds)", classes="field_label")
                    yield Label("Wait duration in seconds before retrying an LLM API call after a dropped connection.", classes="field_help")
                    yield Input(id="api_retry_delay", placeholder="e.g. 15.0")

                yield Label("Deep Research Orchestrator", classes="section_label")
                
                with Vertical(classes="field_container"):
                    yield Label("Maximum Parallel Sub-agents", classes="field_label")
                    yield Label("Number of concurrent sub-agents spawned to execute deep research modules (typically 1 to 10).", classes="field_help")
                    yield Input(id="max_research_agents", placeholder="e.g. 4")
                    
                with Vertical(classes="field_container"):
                    yield Label("Research Context Token Limit", classes="field_label")
                    yield Label("The target context token limit reached by gathered material before research stops.", classes="field_help")
                    yield Input(id="research_context_tokens", placeholder="e.g. 28000")
                    
                with Vertical(classes="field_container"):
                    yield Label("Final Report Minimum Token Length", classes="field_label")
                    yield Label("Minimum tokens required for the synthesized report to prevent automatic rewrite retries.", classes="field_help")
                    yield Input(id="research_min_length", placeholder="e.g. 5000")
                    
                with Vertical(classes="field_container"):
                    yield Label("Context Shrink Max Attempts", classes="field_label")
                    yield Label("Maximum context truncation/reduction loops allowed to salvage context overflow situations.", classes="field_help")
                    yield Input(id="max_shrink_attempts", placeholder="e.g. 15")

                with Vertical(classes="field_container"):
                    yield Label("Scraper Max Token Limit", classes="field_label")
                    yield Label("The maximum number of tokens allowed per web page fetch before content is immediately truncated.", classes="field_help")
                    yield Input(id="scraper_max_tokens", placeholder="e.g. 30000")

                with Vertical(classes="field_container"):
                    yield Label("API Quota Block Wait (Seconds)", classes="field_label")
                    yield Label("Cooldown duration in seconds to pause execution when a 429 quota exhaustion error is encountered.", classes="field_help")
                    yield Input(id="quota_retry_delay", placeholder="e.g. 120.0")

                yield Label("Parsing & PDF Options", classes="section_label")
                
                with Vertical(classes="field_container"):
                    yield Label("PDF Rendering DPI", classes="field_label")
                    yield Label("DPI resolution used when converting PDF document pages to images for vision parsing.", classes="field_help")
                    yield Input(id="pdf_dpi", placeholder="e.g. 150")

                with Vertical(classes="field_container"):
                    yield Label("PDF Footer Text", classes="field_label")
                    yield Label("Text displayed in bottom-right corner of generated PDF reports.", classes="field_help")
                    yield Input(id="pdf_footer_text", placeholder="e.g. FEDERATE RESEARCH REPORT")

                with Vertical(classes="field_container"):
                    yield Label("PDF Body Font", classes="field_label")
                    yield Label("Font family for the main body text (e.g. Space Grotesk, Arial, Georgia).", classes="field_help")
                    yield Input(id="pdf_body_font", placeholder="Space Grotesk")

                with Vertical(classes="field_container"):
                    yield Label("PDF Header Font", classes="field_label")
                    yield Label("Font family for section headings (e.g. Michroma, Space Grotesk, Times New Roman).", classes="field_help")
                    yield Input(id="pdf_header_font", placeholder="Michroma")

                with Vertical(classes="field_container"):
                    yield Label("PDF Code Font", classes="field_label")
                    yield Label("Font family for code blocks (e.g. Space Mono, Courier New).", classes="field_help")
                    yield Input(id="pdf_code_font", placeholder="Space Mono")

                with Vertical(classes="field_container"):
                    yield Label("PDF Body Font Size", classes="field_label")
                    yield Label("Font size for main body text (e.g. 11pt, 12pt).", classes="field_help")
                    yield Input(id="pdf_body_font_size", placeholder="11pt")

                with Vertical(classes="field_container"):
                    yield Label("PDF Title (H1) Font Size", classes="field_label")
                    yield Label("Font size for H1 title text (e.g. 28pt, 24pt).", classes="field_help")
                    yield Input(id="pdf_h1_font_size", placeholder="28pt")

                yield Label("Deep Research Image Companion (Vision Subsystem)", classes="section_label")
                
                with Vertical(classes="field_container"):
                    yield Checkbox("Enable Deep Research Image Companion", id="research_image_system_enabled")
                    
                with Vertical(classes="field_container"):
                    yield Label("Maximum Images to Gather", classes="field_label")
                    yield Label("Limit the maximum number of images requested and verified for the consolidated report.", classes="field_help")
                    yield Input(id="research_images_max", placeholder="e.g. 10")
                    
                with Vertical(classes="field_container"):
                    yield Label("Image Search Retries Per Turn", classes="field_label")
                    yield Label("Number of candidate image downloads and verification checks attempted per turn.", classes="field_help")
                    yield Input(id="research_image_retries", placeholder="e.g. 1")
                    
                with Vertical(classes="field_container"):
                    yield Checkbox("Embed images directly (instead of links)", id="research_images_as_links")

                yield Label("Multi-Agent Intercom", classes="section_label")
                with Vertical(classes="field_container"):
                    yield Label("Tool Result Availability", classes="field_label")
                    yield Label("Controls whether tool outputs shared between agents are hidden behind stubs (Private) or broadcasted immediately (Public).", classes="field_help")
                    yield Select([("Private (Token Efficient)", "private"), ("Public", "public")], value="private", id="tool_result_visibility", allow_blank=False)

                yield Label("Context Compression", classes="section_label")
                with Vertical(classes="field_container"):
                    yield Label("Verbatim Messages to Keep", classes="field_label")
                    yield Label("Number of recent dialogue messages kept verbatim at the end of the context (Minimum: 1).", classes="field_help")
                    yield Input(id="keep_verbatim_count", placeholder="e.g. 2")
                    
            with Horizontal(id="global_actions"):
                yield Button("Save Changes", id="save_global_btn", variant="success")
                yield Button("Cancel", id="cancel_global_btn", variant="error")

    def on_mount(self):
        config = toolbox.load_global_settings()
        self.query_one("#user_name", Input).value = str(config.get("user_name", "User"))
        self.query_one("#user_color", Input).value = str(config.get("user_color", "#dda0dd"))
        self.query_one("#model_color", Input).value = str(config.get("model_color", "#ffd700"))
        self.query_one("#search_pacing_delay", Input).value = str(config.get("search_pacing_delay", 65.0))
        self.query_one("#max_search_results", Input).value = str(config.get("max_search_results", 10))
        self.query_one("#scraper_max_bytes", Input).value = str(config.get("scraper_max_bytes", 1000000))
        self.query_one("#scraper_timeout", Input).value = str(config.get("scraper_timeout", 120.0))
        self.query_one("#scraper_max_tokens", Input).value = str(config.get("scraper_max_tokens", 30000))
        self.query_one("#max_api_retries", Input).value = str(config.get("max_api_retries", 20))
        self.query_one("#api_retry_delay", Input).value = str(config.get("api_retry_delay", 15.0))
        self.query_one("#quota_retry_delay", Input).value = str(config.get("quota_retry_delay", 120.0))
        self.query_one("#max_research_agents", Input).value = str(config.get("max_research_agents", 4))
        self.query_one("#research_context_tokens", Input).value = str(config.get("research_context_tokens", 28000))
        self.query_one("#research_min_length", Input).value = str(config.get("research_min_length", 5000))
        self.query_one("#max_shrink_attempts", Input).value = str(config.get("max_shrink_attempts", 15))
        self.query_one("#pdf_dpi", Input).value = str(config.get("pdf_dpi", 150))
        self.query_one("#pdf_footer_text", Input).value = str(config.get("pdf_footer_text", "FEDERATE RESEARCH REPORT"))
        self.query_one("#pdf_body_font", Input).value = str(config.get("pdf_body_font", "Space Grotesk"))
        self.query_one("#pdf_header_font", Input).value = str(config.get("pdf_header_font", "Michroma"))
        self.query_one("#pdf_code_font", Input).value = str(config.get("pdf_code_font", "Space Mono"))
        self.query_one("#pdf_body_font_size", Input).value = str(config.get("pdf_body_font_size", "11pt"))
        self.query_one("#pdf_h1_font_size", Input).value = str(config.get("pdf_h1_font_size", "28pt"))
        self.query_one("#keep_verbatim_count", Input).value = str(config.get("keep_verbatim_count", 1))
        self.query_one("#tool_result_visibility", Select).value = config.get("tool_result_visibility", "private")
        
        self.query_one("#research_images_max", Input).value = str(config.get("research_images_max", 10))
        self.query_one("#research_image_retries", Input).value = str(config.get("research_image_retries", 1))
        with self.prevent(Checkbox.Changed):
            self.query_one("#research_image_system_enabled", Checkbox).value = config.get("research_image_system_enabled", False)
            self.query_one("#research_images_as_links", Checkbox).value = not config.get("research_images_as_links", False)
            self.query_one("#autoupdate_on_launch", Checkbox).value = config.get("autoupdate_on_launch", False)

    @on(Checkbox.Changed, "#research_images_as_links")
    def on_as_links_changed(self, event: Checkbox.Changed):
        if event.value:  
            self.check_and_trigger_warning("research_images_as_links")

    @on(Checkbox.Changed, "#research_image_system_enabled")
    def on_system_enabled_changed(self, event: Checkbox.Changed):
        if event.value:  
            self.check_and_trigger_warning("research_image_system_enabled")

    def check_and_trigger_warning(self, changed_checkbox_id: str):
        system_enabled = self.query_one("#research_image_system_enabled", Checkbox).value
        embed_enabled = self.query_one("#research_images_as_links", Checkbox).value
        
        if system_enabled and embed_enabled:
            def handle_warning(accepted: bool):
                if not accepted:
                    with self.prevent(Checkbox.Changed):
                        self.query_one(f"#{changed_checkbox_id}", Checkbox).value = False
            self.app.push_screen(CopyrightWarningModal(), handle_warning)

    @on(Button.Pressed, "#save_global_btn")
    def save_btn(self):
        user_name = self.query_one("#user_name", Input).value.strip() or "User"
        user_color = self.query_one("#user_color", Input).value.strip() or "#dda0dd"
        model_color = self.query_one("#model_color", Input).value.strip() or "#ffd700"

        # Validate color syntax with Rich
        from rich.style import Style
        try:
            Style.parse(model_color)
        except Exception:
            self.notify(f"Invalid Model Name Color '{model_color}'. Use a standard name or hex (e.g. yellow, #ffd700).", severity="error")
            return

        try:
            Style.parse(user_color)
        except Exception:
            self.notify(f"Invalid User Color '{user_color}'. Use a standard name or hex (e.g. #dda0dd, purple).", severity="error")
            return
        
        try: pacing = float(self.query_one("#search_pacing_delay", Input).value.strip())
        except ValueError: pacing = 65.0
        
        try: max_results = int(self.query_one("#max_search_results", Input).value.strip())
        except ValueError: max_results = 10
        
        try: max_bytes = int(self.query_one("#scraper_max_bytes", Input).value.strip())
        except ValueError: max_bytes = 1000000
        
        try: timeout = float(self.query_one("#scraper_timeout", Input).value.strip())
        except ValueError: timeout = 120.0
        
        try: scraper_tokens = int(self.query_one("#scraper_max_tokens", Input).value.strip())
        except ValueError: scraper_tokens = 30000
        
        try: max_retries = int(self.query_one("#max_api_retries", Input).value.strip())
        except ValueError: max_retries = 20
        
        try: retry_delay = float(self.query_one("#api_retry_delay", Input).value.strip())
        except ValueError: retry_delay = 15.0
        
        try: quota_delay = float(self.query_one("#quota_retry_delay", Input).value.strip())
        except ValueError: quota_delay = 120.0
            
        try: max_agents = int(self.query_one("#max_research_agents", Input).value.strip())
        except ValueError: max_agents = 4
            
        try: tokens = int(self.query_one("#research_context_tokens", Input).value.strip())
        except ValueError: tokens = 28000
            
        try: min_len = int(self.query_one("#research_min_length", Input).value.strip())
        except ValueError: min_len = 5000
        
        try: max_shrink = int(self.query_one("#max_shrink_attempts", Input).value.strip())
        except ValueError: max_shrink = 15
        
        try: pdf_dpi_val = int(self.query_one("#pdf_dpi", Input).value.strip())
        except ValueError: pdf_dpi_val = 150

        pdf_footer = self.query_one("#pdf_footer_text", Input).value.strip() or "FEDERATE RESEARCH REPORT"
        pdf_body_font = self.query_one("#pdf_body_font", Input).value.strip() or "Space Grotesk"
        pdf_header_font = self.query_one("#pdf_header_font", Input).value.strip() or "Michroma"
        pdf_code_font = self.query_one("#pdf_code_font", Input).value.strip() or "Space Mono"
        pdf_body_size = self.query_one("#pdf_body_font_size", Input).value.strip() or "11pt"
        pdf_h1_size = self.query_one("#pdf_h1_font_size", Input).value.strip() or "28pt"

        try: keep_verbatim = int(self.query_one("#keep_verbatim_count", Input).value.strip())
        except ValueError: keep_verbatim = 1

        if keep_verbatim < 1:
            self.notify("Invalid: Verbatim Messages to Keep must be at least 1.", severity="error")
            return
            
        try: img_max = int(self.query_one("#research_images_max", Input).value.strip())
        except ValueError: img_max = 10
        
        try: img_retries = int(self.query_one("#research_image_retries", Input).value.strip())
        except ValueError: img_retries = 1
        
        img_system_enabled = self.query_one("#research_image_system_enabled", Checkbox).value
        images_as_links = not self.query_one("#research_images_as_links", Checkbox).value
        autoupdate_on_launch = self.query_one("#autoupdate_on_launch", Checkbox).value
        
        tool_result_vis_val = self.query_one("#tool_result_visibility", Select).value
        tool_result_visibility = str(tool_result_vis_val) if tool_result_vis_val != Select.BLANK else "private"

        config = {
            "tool_result_visibility": tool_result_visibility,
            "user_name": user_name,
            "autoupdate_on_launch": autoupdate_on_launch,
            "user_color": user_color,
            "model_color": model_color,
            "search_pacing_delay": pacing,
            "max_search_results": max_results,
            "scraper_max_bytes": max_bytes,
            "scraper_timeout": timeout,
            "scraper_max_tokens": scraper_tokens,
            "max_api_retries": max_retries,
            "api_retry_delay": retry_delay,
            "quota_retry_delay": quota_delay,
            "max_research_agents": max_agents,
            "research_context_tokens": tokens,
            "research_min_length": min_len,
            "max_shrink_attempts": max_shrink,
            "pdf_dpi": pdf_dpi_val,
            "pdf_footer_text": pdf_footer,
            "pdf_body_font": pdf_body_font,
            "pdf_header_font": pdf_header_font,
            "pdf_code_font": pdf_code_font,
            "pdf_body_font_size": pdf_body_size,
            "pdf_h1_font_size": pdf_h1_size,
            "keep_verbatim_count": keep_verbatim,
            "research_image_system_enabled": img_system_enabled,
            "research_images_max": img_max,
            "research_image_retries": img_retries,
            "research_images_as_links": images_as_links
        }
        toolbox.save_global_settings(config)
        
        toolbox._DYNAMIC_PACING_DELAY = pacing
        
        try:
            agent_view = self.app.query_one("AIAgentView")
            agent_view.pdf_dpi = pdf_dpi_val
        except Exception:
            pass
            
        self.dismiss("update")

    @on(Button.Pressed, "#cancel_global_btn")
    def cancel_btn(self):
        self.dismiss("cancel")

VOICE_OPTIONS = [
    # American English
    ("af_heart (US Female )", "af_heart"),
    ("af_alloy (US Female)", "af_alloy"),
    ("af_aoede (US Female)", "af_aoede"),
    ("af_bella (US Female )", "af_bella"),
    ("af_jessica (US Female)", "af_jessica"),
    ("af_kore (US Female)", "af_kore"),
    ("af_nicole (US Female )", "af_nicole"),
    ("af_nova (US Female)", "af_nova"),
    ("af_river (US Female)", "af_river"),
    ("af_sarah (US Female)", "af_sarah"),
    ("af_sky (US Female )", "af_sky"),
    ("am_adam (US Male)", "am_adam"),
    ("am_echo (US Male)", "am_echo"),
    ("am_eric (US Male)", "am_eric"),
    ("am_fenrir (US Male)", "am_fenrir"),
    ("am_liam (US Male)", "am_liam"),
    ("am_michael (US Male)", "am_michael"),
    ("am_onyx (US Male)", "am_onyx"),
    ("am_puck (US Male)", "am_puck"),
    ("am_santa (US Male )", "am_santa"),
    # British English
    ("bf_alice (UK Female)", "bf_alice"),
    ("bf_emma (UK Female)", "bf_emma"),
    ("bf_isabella (UK Female)", "bf_isabella"),
    ("bf_lily (UK Female)", "bf_lily"),
    ("bm_daniel (UK Male)", "bm_daniel"),
    ("bm_fable (UK Male)", "bm_fable"),
    ("bm_george (UK Male)", "bm_george"),
    ("bm_lewis (UK Male)", "bm_lewis"),
    # Japanese
    ("jf_alpha (JP Female)", "jf_alpha"),
    ("jf_gongitsune (JP Female)", "jf_gongitsune"),
    ("jf_nezumi (JP Female )", "jf_nezumi"),
    ("jf_tebukuro (JP Female)", "jf_tebukuro"),
    ("jm_kumo (JP Male )", "jm_kumo"),
    # Mandarin Chinese
    ("zf_xiaobei (ZH Female)", "zf_xiaobei"),
    ("zf_xiaoni (ZH Female)", "zf_xiaoni"),
    ("zf_xiaoxiao (ZH Female)", "zf_xiaoxiao"),
    ("zf_xiaoyi (ZH Female)", "zf_xiaoyi"),
    ("zm_yunjian (ZH Male)", "zm_yunjian"),
    ("zm_yunxi (ZH Male)", "zm_yunxi"),
    ("zm_yunxia (ZH Male)", "zm_yunxia"),
    ("zm_yunyang (ZH Male)", "zm_yunyang"),
    # Spanish
    ("ef_dora (ES Female)", "ef_dora"),
    ("em_alex (ES Male)", "em_alex"),
    ("em_santa (ES Male)", "em_santa"),
    # French
    ("ff_siwis (FR Female)", "ff_siwis"),
    # Hindi
    ("hf_alpha (HI Female)", "hf_alpha"),
    ("hf_beta (HI Female)", "hf_beta"),
    ("hm_omega (HI Male)", "hm_omega"),
    ("hm_psi (HI Male)", "hm_psi"),
    # Italian
    ("if_sara (IT Female)", "if_sara"),
    ("im_nicola (IT Male)", "im_nicola"),
    # Brazilian Portuguese
    ("pf_dora (PT Female)", "pf_dora"),
    ("pm_alex (PT Male)", "pm_alex"),
    ("pm_santa (PT Male)", "pm_santa"),
]

BASE_URL_PRESETS = [
    ("OpenRouter", "https://openrouter.ai/api/v1"),
    ("OpenAI", "https://api.openai.com/v1/"),
    ("Anthropic", "https://api.anthropic.com/v1/"),
    ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    ("ChatGPT Subscription (OAuth)", "https://chatgpt.com/backend-api/codex"),
    ("Custom", "custom")
]

class OnboardingModal(ModalScreen[dict]):
    DEFAULT_CSS = """
    OnboardingModal { align: center middle; background: $background 60%; }
    #onboard_dialog { width: 75; max-height: 90vh; border: round $success; background: $surface; padding: 0 0; }
    #onboard_scroll { padding: 1 2; }
    .details_box { padding: 0 1; margin-bottom: 0; height: auto; background: $surface; }
    .pane_title { background: $success; color: $text; text-style: bold; text-align: center; width: 100%; height: 3; content-align: center middle; }
    .section_label { color: $success; text-style: bold; text-align: center; width: 100%; margin-top: 1; margin-bottom: 1; }
    Input, Select, SelectCurrent { background: black !important; color: white !important; border: round $success; height: 3; margin-bottom: 1; }
    #onboard_backstory { background: black !important; color: white !important; border: round $success; height: auto; min-height: 3; max-height: 18; margin-bottom: 1; }
    Input:focus, Select:focus, SelectCurrent:focus { background: black !important; color: white !important; border: round $accent; }
    #onboard_chatgpt_auth_btn { display: none; margin-top: 1; margin-bottom: 1; width: 100%; }
    #onboard_actions { height: 4; align: center middle; border-top: solid $primary; margin-top: 1; }
    """

    def __init__(self, initial_data: dict = None, **kwargs):
        super().__init__()
        self.initial_data = initial_data or {}

    def update_auth_btn_visibility(self):
        btn = self.query_one("#onboard_chatgpt_auth_btn", Button)
        api_input = self.query_one("#onboard_api_key", Input)
        api_label = self.query_one("#onboard_api_key_label", Label)
        preset_val = self.query_one("#onboard_base_url_preset", Select).value
        if preset_val == "https://chatgpt.com/backend-api/codex":
            btn.styles.display = "block"
            api_input.styles.display = "none"
            api_label.styles.display = "none"
        else:
            btn.styles.display = "none"
            api_input.styles.display = "block"
            api_label.styles.display = "block"

    def on_mount(self):
        if self.initial_data:
            if self.initial_data.get("api_key"):
                self.query_one("#onboard_api_key", Input).value = self.initial_data["api_key"]
            if self.initial_data.get("model"):
                self.query_one("#onboard_model", Input).value = self.initial_data["model"]
            if self.initial_data.get("base_url"):
                b_url = self.initial_data["base_url"]
                self.query_one("#onboard_base_url", Input).value = b_url
                matched = "custom"
                for label, val in BASE_URL_PRESETS:
                    if val == b_url:
                        matched = val
                        break
                with self.prevent(Select.Changed):
                    self.query_one("#onboard_base_url_preset", Select).value = matched
            if self.initial_data.get("name"):
                self.query_one("#onboard_name", Input).value = self.initial_data["name"]
            if self.initial_data.get("backstory"):
                self.query_one("#onboard_backstory", TextArea).text = self.initial_data["backstory"]
        self.update_auth_btn_visibility()
        self.query_one("#onboard_api_key", Input).focus()

    def compose(self) -> ComposeResult:
        with Vertical(id="onboard_dialog"):
            yield Label(" Welcome to Federate Multiagent Harness", classes="pane_title")
            with VerticalScroll(id="onboard_scroll"):
                with Vertical(classes="details_box"):
                    yield Label("Please configure your first agent to get started:", classes="section_label")
                    yield Label("API Key:", id="onboard_api_key_label")
                    yield Input(placeholder="Enter your API Key...", id="onboard_api_key", password=True)
                    yield Button("Authenticate with OAuth", id="onboard_chatgpt_auth_btn", variant="primary")
                    yield Label("Base URL Preset:")
                    yield Select(BASE_URL_PRESETS, value="https://generativelanguage.googleapis.com/v1beta/openai/", id="onboard_base_url_preset", allow_blank=False)
                    yield Label("Model:")
                    yield Input("gemini-3.1-flash-lite", id="onboard_model")
                    yield Label("Base URL:")
                    yield Input("https://generativelanguage.googleapis.com/v1beta/openai/", id="onboard_base_url")
                    yield Label("Agent Name:")
                    yield Input("Rita", id="onboard_name")
                    yield Label("Agent Backstory:")
                    yield TextArea("You are Rita, a general purpose senior developer.", id="onboard_backstory", show_line_numbers=False)
            with Horizontal(id="onboard_actions"):
                yield Button("Get Started", id="onboard_submit_btn", variant="success")

    @on(Select.Changed, "#onboard_base_url_preset")
    def on_preset_changed(self, event: Select.Changed):
        if event.value != "custom" and event.value != Select.BLANK:
            with self.prevent(Input.Changed):
                self.query_one("#onboard_base_url", Input).value = str(event.value)
        self.update_auth_btn_visibility()

    @on(Button.Pressed, "#onboard_chatgpt_auth_btn")
    def on_onboard_chatgpt_auth(self):
        def on_auth_done(success: bool):
            if success:
                self.query_one("#onboard_base_url", Input).value = "https://chatgpt.com/backend-api/codex"
                self.notify("ChatGPT Subscription authenticated successfully!", severity="information")
        self.app.push_screen(ChatGPTAuthModal(), on_auth_done)

    @on(Input.Changed, "#onboard_base_url")
    def on_base_url_changed(self, event: Input.Changed):
        input_val = event.value.strip()
        matched = "custom"
        check_val = "https://chatgpt.com/backend-api/codex" if input_val == "https://api.openai.com/v1/?oauth=chatgpt" else input_val
        for label, val in BASE_URL_PRESETS:
            if val == check_val:
                matched = val
                break
        select_widget = self.query_one("#onboard_base_url_preset", Select)
        if select_widget.value != matched:
            with self.prevent(Select.Changed):
                select_widget.value = matched
        self.update_auth_btn_visibility()

    @on(Input.Submitted, "#onboard_api_key")
    @on(Input.Submitted, "#onboard_name")
    @on(Button.Pressed, "#onboard_submit_btn")
    def submit(self):
        preset_val = self.query_one("#onboard_base_url_preset", Select).value
        base_url = self.query_one("#onboard_base_url", Input).value.strip()
        api_key = self.query_one("#onboard_api_key", Input).value.strip()
        if (preset_val == "https://chatgpt.com/backend-api/codex" or base_url == "https://chatgpt.com/backend-api/codex") and not api_key:
            api_key = "CHATGPT_OAUTH_ACTIVE"

        if not api_key:
            self.notify("API Key cannot be empty. Please enter your API key to proceed.", severity="error")
            self.query_one("#onboard_api_key", Input).focus()
            return

        name = self.query_one("#onboard_name", Input).value.strip()
        if not name:
            self.notify("Agent name cannot be empty.", severity="error")
            self.query_one("#onboard_name", Input).focus()
            return

        self.dismiss({
            "name": name,
            "backstory": self.query_one("#onboard_backstory", TextArea).text.strip(),
            "model": self.query_one("#onboard_model", Input).value.strip(),
            "base_url": self.query_one("#onboard_base_url", Input).value.strip(),
            "api_key": api_key,
        })

class AbilitiesModal(ModalScreen[dict]):
    DEFAULT_CSS = """
    AbilitiesModal { align: center middle; background: $background 60%; }
    #abilities_dialog { width: 70; height: 80%; border: thick $primary; background: $surface; padding: 0 0; }
    #abilities_scroll { padding: 1 2; }
    .section_label { background: $primary; color: $text; padding: 0 1; margin-top: 1; text-style: bold; }
    .ability_row { layout: horizontal; height: auto; align: left middle; margin-bottom: 0; }
    .enable_cb { width: 65%; }
    .disable_cb { width: 35%; color: $error; }
    #abilities_actions { height: 4; align: center middle; border-top: solid $primary; margin-top: 1; }
    #abilities_actions Button { margin-left: 1; }
    """

    def __init__(self, enabled_tools: list, disabled_tools: list, all_manageable_tools: list):
        super().__init__()
        self.enabled_tools = list(enabled_tools)
        self.disabled_tools = list(disabled_tools)
        self.all_manageable_tools = all_manageable_tools

    def compose(self) -> ComposeResult:
        with Vertical(id="abilities_dialog"):
            yield Label(" Agent Abilities", classes="pane_title")
            with VerticalScroll(id="abilities_scroll"):
                yield Label("Enable in SAFE mode / Disable tool", classes="section_label")
                for tool_name in self.all_manageable_tools:
                    is_enabled = tool_name in self.enabled_tools
                    is_disabled = tool_name in self.disabled_tools
                    label = "Autonomous Visual Computer Operation" if tool_name == "visual_computer_operation" else tool_name.replace("_", " ").title()
                    with Horizontal(classes="ability_row"):
                        yield Checkbox(f"Enable (Safe): {label}", id=f"ability_{tool_name}", value=is_enabled, classes="enable_cb")
                        yield Checkbox("Force Disable", id=f"disable_{tool_name}", value=is_disabled, classes="disable_cb")
            with Horizontal(id="abilities_actions"):
                yield Button("Save", id="abilities_save_btn", variant="success")
                yield Button("Cancel", id="abilities_cancel_btn", variant="error")

    @on(Button.Pressed, "#abilities_save_btn")
    def save_btn(self):
        enabled = []
        disabled = []
        for tool_name in self.all_manageable_tools:
            if self.query_one(f"#ability_{tool_name}", Checkbox).value:
                enabled.append(tool_name)
            if self.query_one(f"#disable_{tool_name}", Checkbox).value:
                disabled.append(tool_name)
        self.dismiss({"enabled_tools": enabled, "disabled_tools": disabled})

    @on(Button.Pressed, "#abilities_cancel_btn")
    def cancel_btn(self):
        self.dismiss(None)

class ConfigModal(ModalScreen[str]):
    DEFAULT_CSS = """
    ConfigModal { align: center middle; background: $background 60%; }
    #config_dialog { width: 75; max-height: 90vh; border: round $primary; background: $surface; padding: 0 0; }
    #config_scroll { padding: 1 2; }
    #actions_container {
        height: 6; 
        align: center middle;
        margin-bottom: 1;
        border-top: solid $primary;
    }
    .details_box { padding: 0 1; margin-bottom: 0; height: auto; background: $surface;}
    .config_row { layout: horizontal; height: auto; margin-top: 1; }
    .config_row Checkbox { width: 45%; }
    .section_label { background: $primary; color: $text; padding: 0 1; margin-top: 1; text-style: bold; }
    #ai_backstory { height: auto; min-height: 3; max-height: 18; margin-bottom: 1; }
    #ai_abilities_btn { margin-top: 1; margin-bottom: 1; width: 100%; }
    #ai_chatgpt_auth_btn { display: none; margin-top: 1; margin-bottom: 1; width: 100%; }
    #abilities_container {
        border: round $primary;
        height: auto;
        max-height: 14;
        padding: 0 1;
        margin-top: 1;
        background: $boost;
    }
    .ability_row { layout: horizontal; height: auto; align: left middle; margin-bottom: 0; }
    .enable_cb { width: 65%; }
    .disable_cb { width: 35%; color: $error; }
    """
    def __init__(self, agent_config: AgentConfig, agent_manager: AgentManager):
        super().__init__()
        self.agent_config = agent_config
        self.agent_manager = agent_manager
        self.enabled_tools = list(agent_config.enabled_tools)
        self.disabled_tools = list(getattr(agent_config, "disabled_tools", ["visual_computer_operation", "send_file_to_telegram"]))
        self.all_manageable_tools = [
            "list_files", "search_web", "perform_research", "render_pdf", "manage_agenda",
            "read_file", "fetch_url", "save_file", "edit_file", 
            "dispatch_coding_subagent", "run_terminal_command", "visual_computer_operation", "send_file_to_telegram"
        ]

    def update_auth_btn_visibility(self):
        btn = self.query_one("#ai_chatgpt_auth_btn", Button)
        api_input = self.query_one("#ai_api_key", Input)
        api_label = self.query_one("#ai_api_key_label", Label)
        preset_val = self.query_one("#ai_base_url_preset", Select).value
        if preset_val == "https://chatgpt.com/backend-api/codex":
            btn.styles.display = "block"
            api_input.styles.display = "none"
            api_label.styles.display = "none"
        else:
            btn.styles.display = "none"
            api_input.styles.display = "block"
            api_label.styles.display = "block"

    def on_mount(self):
        self.update_auth_btn_visibility()

    def compose(self) -> ComposeResult:
        with Vertical(id="config_dialog"):
            yield Label(f"Agent Editor: {self.agent_config.name}", classes="pane_title")
            with VerticalScroll(id="config_scroll"):
                with Vertical(classes="details_box"):
                    yield Label("Primary Settings", classes="section_label")
                    yield Label("Agent Name:")
                    yield Input(value=self.agent_config.name, id="ai_name")
                    yield Label("Backstory:")
                    yield TextArea(self.agent_config.backstory, id="ai_backstory", show_line_numbers=False)
                    yield Label("Model:")
                    yield Input(value=self.agent_config.model, id="ai_model")
                    yield Label("Reasoning Effort (Thinking models):")
                    yield Select([("Default", "none"), ("Low", "low"), ("Medium", "medium"), ("High", "high")], value=getattr(self.agent_config, "reasoning_effort", "none"), id="ai_reasoning_effort", allow_blank=False)
                    yield Label("Temperature:")
                    yield Input(value=str(getattr(self.agent_config, "temperature", 1.0)), id="ai_temperature")
                    yield Label("Base URL Preset:")
                    current_url = self.agent_config.base_url or "https://openrouter.ai/api/v1"
                    if current_url == "https://api.openai.com/v1/?oauth=chatgpt":
                        current_url = "https://chatgpt.com/backend-api/codex"
                        
                    matched_preset = "custom"
                    for label, val in BASE_URL_PRESETS:
                        if val == current_url:
                            matched_preset = val
                            break
                    yield Select(BASE_URL_PRESETS, value=matched_preset, id="ai_base_url_preset", allow_blank=False)
                    
                    yield Label("Base URL:")
                    yield Input(value=current_url, id="ai_base_url")
                    yield Label("API Key:", id="ai_api_key_label")
                    yield Input(value=self.agent_config.get_api_key(), id="ai_api_key", password=True)
                    yield Button("Authenticate with OAuth", id="ai_chatgpt_auth_btn", variant="primary")
                    yield Label("Agent Color (Hex):")
                    yield Input(value=self.agent_config.color, id="ai_color")
                    yield Label("TTS Voice:") 
                    
                    current_voice = self.agent_config.tts_voice or "af_sarah"
                    voice_options = VOICE_OPTIONS.copy()
                    if not any(opt[1] == current_voice for opt in voice_options):
                        voice_options.insert(0, (f"{current_voice} (Custom)", current_voice))
                        
                    yield Select(voice_options, value=current_voice, id="ai_tts_voice", allow_blank=False) 
                    yield Label("Pronouns:") 
                    yield Select([("He/Him", "he/him"), ("She/Her", "she/her"), ("Neither", "neither")], value=self.agent_config.pronouns or "neither", id="ai_pronouns", allow_blank=False) 
                    with Horizontal(classes="config_row"):
                        yield Checkbox("Vision Capable", id="ai_vision_capable", value=self.agent_config.is_capable_vision)
                        yield Checkbox("Disable All Tools", id="ai_disable_all_tools", value=self.agent_config.disable_all_tools) 

                    yield Label("Agent Abilities", classes="section_label")
                    yield Button("Manage Agent Abilities...", id="ai_abilities_btn", variant="primary")


                    yield Label("Backup Inference", classes="section_label")
                    agent_options = [("Manual Entry", "manual")] + [(name, name) for name in self.agent_manager.agents.keys()]
                    yield Label("Copy from Agent:")
                    yield Select(agent_options, value="manual", id="ai_copy_from")

                    yield Label("Backup Model:")
                    yield Input(value=self.agent_config.backup_model, id="ai_backup_model")
                    yield Label("Backup Base URL:")
                    yield Input(value=self.agent_config.backup_base_url, id="ai_backup_base_url")
                    yield Label("Backup API Key:")
                    yield Input(value=self.agent_config.get_backup_api_key(), id="ai_backup_api_key", password=True)

                    with Horizontal(classes="config_row"):
                        yield Checkbox("Use Backup Provider", id="ai_use_backup", value=self.agent_config.use_backup)

            yield Label("", id="ai_modal_status", classes="status_center")
            with Horizontal(id="actions_container"):
                yield Button("Update Active", id="ai_save_btn", variant="primary")
                yield Button("Save As New", id="ai_save_new_btn", variant="success")
                yield Button("Delete Agent", id="ai_delete_btn", variant="error")
                yield Button("Close", id="ai_cancel_btn")

    @on(Select.Changed, "#ai_copy_from")
    def on_copy_from_changed(self, event: Select.Changed):
        if event.value == "manual" or not event.value:
            return

        agent_name = str(event.value)
        agent = self.agent_manager.get_agent(agent_name)
        if agent:
            self.query_one("#ai_backup_model", Input).value = agent.model
            self.query_one("#ai_backup_base_url", Input).value = agent.base_url
            self.query_one("#ai_backup_api_key", Input).value = agent.get_api_key()
    
    @on(Select.Changed, "#ai_base_url_preset")
    def on_base_url_preset_changed(self, event: Select.Changed):
        if event.value != "custom" and event.value != Select.BLANK:
            with self.prevent(Input.Changed):
                self.query_one("#ai_base_url", Input).value = str(event.value)
        self.update_auth_btn_visibility()

    @on(Button.Pressed, "#ai_chatgpt_auth_btn")
    def on_config_chatgpt_auth(self):
        def on_auth_done(success: bool):
            if success:
                self.query_one("#ai_base_url", Input).value = "https://chatgpt.com/backend-api/codex"
                self.notify("ChatGPT Subscription authenticated successfully!", severity="information")
        self.app.push_screen(ChatGPTAuthModal(), on_auth_done)

    @on(Input.Changed, "#ai_base_url")
    def on_base_url_input_changed(self, event: Input.Changed):
        input_val = event.value.strip()
        matched = "custom"
        check_val = "https://chatgpt.com/backend-api/codex" if input_val == "https://api.openai.com/v1/?oauth=chatgpt" else input_val
        for label, val in BASE_URL_PRESETS:
            if val == check_val:
                matched = val
                break
        
        select_widget = self.query_one("#ai_base_url_preset", Select)
        if select_widget.value != matched:
            with self.prevent(Select.Changed):
                select_widget.value = matched
        self.update_auth_btn_visibility()
    
    @on(Button.Pressed, "#ai_abilities_btn")
    def open_abilities_btn(self):
        def handle_abilities(result):
            if result:
                self.enabled_tools = result["enabled_tools"]
                self.disabled_tools = result["disabled_tools"]
        self.app.push_screen(AbilitiesModal(self.enabled_tools, self.disabled_tools, self.all_manageable_tools), handle_abilities)
    
    def _get_current_fields(self):
        enabled_tools = self.enabled_tools
        disabled_tools = self.disabled_tools

        try:
            temp = float(self.query_one("#ai_temperature", Input).value.strip())
        except ValueError:
            temp = 1.0

        preset_val = self.query_one("#ai_base_url_preset", Select).value
        api_key_val = self.query_one("#ai_api_key", Input).value.strip()
        if preset_val == "https://chatgpt.com/backend-api/codex" and not api_key_val:
            api_key_val = "CHATGPT_OAUTH_ACTIVE"

        return {
            "name": self.query_one("#ai_name", Input).value.strip(),
            "backstory": self.query_one("#ai_backstory", TextArea).text.strip(),
            "model": self.query_one("#ai_model", Input).value.strip(),
            "base_url": self.query_one("#ai_base_url", Input).value.strip(),
            "is_capable_vision": self.query_one("#ai_vision_capable", Checkbox).value,
            "disable_all_tools": self.query_one("#ai_disable_all_tools", Checkbox).value,
            "api_key": api_key_val,
            "color": self.query_one("#ai_color", Input).value.strip() or "#00FFFF",
            "backup_model": self.query_one("#ai_backup_model", Input).value.strip(),
            "backup_base_url": self.query_one("#ai_backup_base_url", Input).value.strip(),
            "backup_api_key": self.query_one("#ai_backup_api_key", Input).value.strip(),
            "use_backup": self.query_one("#ai_use_backup", Checkbox).value,
            "enabled_tools": enabled_tools,
            "disabled_tools": disabled_tools,
            "tts_voice": (self.query_one("#ai_tts_voice", Select).value if self.query_one("#ai_tts_voice", Select).value != Select.BLANK else None) or "af_sarah",
            "pronouns": (self.query_one("#ai_pronouns", Select).value if self.query_one("#ai_pronouns", Select).value != Select.BLANK else None) or "she/her",
            "reasoning_effort": (self.query_one("#ai_reasoning_effort", Select).value if self.query_one("#ai_reasoning_effort", Select).value != Select.BLANK else None) or "none",
            "temperature": temp
        }

    def _apply_save(self, is_new: bool):
        fields = self._get_current_fields()
        if not fields["name"]:
            self.notify("Agent name cannot be empty.", severity="error")
            return

        existing_names = [a.lower() for a in self.agent_manager.agents.keys()]
        if is_new or fields["name"].lower() != self.agent_config.name.lower():
            if fields["name"].lower() in existing_names:
                self.notify(f"An agent named '{fields['name']}' already exists.", severity="error")
                return

        if not is_new and fields["name"] != self.agent_config.name and fields["backstory"] != self.agent_config.backstory:
            is_new = True

        def handle_unlock(result):
            if not result:
                return
            action, password = result
            if action == "unlock" and password:
                if toolbox.unlock_keyring(password):
                    self.notify("Keyring unlocked. Saving...", severity="information")
                    self._apply_save(is_new)
                else:
                    self.notify("Unlock failed.", severity="error")
            elif action == "reset" and password:
                try:
                    import keyring, os
                    backend = keyring.get_keyring()
                    backends = [backend]
                    if hasattr(backend, 'backends'):
                        backends.extend(backend.backends)
                    for b in backends:
                        if type(b).__name__ == "EncryptedKeyring":
                            if hasattr(b, "file_path") and b.file_path and os.path.exists(b.file_path):
                                os.remove(b.file_path)
                            b.__dict__['keyring_key'] = password
                    self.notify("Keyring reset successfully. Saving...", severity="warning")
                    self._apply_save(is_new)
                except Exception as e:
                    self.notify(f"Reset failed: {e}", severity="error")

        locked_keyring = toolbox.get_locked_keyring()
        if locked_keyring:
            self.app.push_screen(KeyringUnlockModal(), handle_unlock)
            return

        clean_fields = {k:v for k,v in fields.items() if k not in ["api_key", "backup_api_key"]}
        new_config = AgentConfig(**clean_fields)

        self._update_env(new_config.name, fields["api_key"], fields["backup_api_key"])

        for btn in self.query("#actions_container Button"):
            btn.disabled = True
        self.query_one("#ai_modal_status", Label).update(f"[bold yellow] Verifying API & backstory for '{new_config.name}'...[/bold yellow]")

        agent_view = self.app.query_one("AIAgentView")
        agent_view.process_agent_save(new_config, is_new, self.agent_config.name, modal=self)

    def _update_env(self, agent_name: str, primary_key: str, backup_key: str):
        import keyring
        primary_user = f"agent_key_{agent_name.lower().replace(' ', '_')}"
        backup_user = f"agent_backup_key_{agent_name.lower().replace(' ', '_')}"

        try:
            if primary_key:
                keyring.set_password("Federate", primary_user, primary_key)
                os.environ[f"AGENT_KEY_{agent_name.upper().replace(' ', '_')}"] = primary_key
            else:
                try: keyring.delete_password("Federate", primary_user)
                except Exception: pass

            if backup_key:
                keyring.set_password("Federate", backup_user, backup_key)
                os.environ[f"AGENT_BACKUP_KEY_{agent_name.upper().replace(' ', '_')}"] = backup_key
            else:
                try: keyring.delete_password("Federate", backup_user)
                except Exception: pass

        except Exception as e:
            self.notify(
                f"Storage Error: Could not save credentials securely to OS Keyring.\nDetail: {e}",
                severity="error", 
                title="Keychain Access Failed"
            )

    @on(Button.Pressed, "#ai_save_btn")
    def save_btn(self):
        self._apply_save(is_new=False)

    @on(Button.Pressed, "#ai_save_new_btn")
    def save_new_btn(self):
        self._apply_save(is_new=True)

    @on(Button.Pressed, "#ai_delete_btn")
    def delete_btn(self):
        self.dismiss(("delete", self.agent_config))

    @on(Button.Pressed, "#ai_cancel_btn")
    def cancel_btn(self):
        self.dismiss(("cancel", None))

class ScheduleModal(ModalScreen[None]):
    DEFAULT_CSS = """
    ScheduleModal { align: center middle; background: $background 60%; }
    #sched_dialog { width: 75; height: 85%; border: thick $primary; background: $surface; padding: 1 2; }
    #main_scroll { height: 1fr; border: round $accent; padding: 1; background: $boost; margin-bottom: 1; }
    #add_form { height: auto; margin-bottom: 1; }
    #task_list { height: auto; }
    .section_label { text-style: bold; margin-bottom: 1; color: $text; }
    .task_item { layout: horizontal; height: auto; padding: 1; border-bottom: solid $primary 50%; }
    .task_info { width: 1fr; }
    .task_del_btn { width: 10; margin-left: 1; margin-top: 1; }
    .task_edit_btn { width: 10; margin-left: 1; margin-top: 1; }
    .form_row { layout: horizontal; height: auto; margin-bottom: 1; }
    #new_agent { width: 40%; }
    #new_time { width: 60%; margin-left: 1; }
    #new_date { width: 50%; }
    #new_repeat { width: 50%; margin-left: 1; }
    #new_prompt { height: auto; min-height: 3; max-height: 18; margin-bottom: 1; }
    #action_buttons { height: auto; align: right middle; margin-top: 0; }
    #action_buttons Button { margin-left: 1; }
    """
    def __init__(self, agent_view):
        super().__init__()
        self.agent_view = agent_view
        
    def _get_next_run(self, task):
        import calendar
        now = datetime.now()
        try:
            task_date = datetime.strptime(getattr(task, "date_str", ""), "%Y-%m-%d") if getattr(task, "date_str", "") else now
            th, tm = map(int, task.time_str.split(":"))
            candidate = datetime(task_date.year, task_date.month, task_date.day, th, tm)
        except Exception:
            return None

        repeat_mode = getattr(task, "repeat", "daily")
        from datetime import timedelta
        while candidate <= now or (getattr(task, "last_run_date", "").startswith(now.strftime("%Y-%m-%d")) and candidate.date() <= now.date()):
            if repeat_mode == "daily":
                candidate += timedelta(days=1)
            elif repeat_mode == "weekly":
                candidate += timedelta(weeks=1)
            elif repeat_mode == "monthly":
                month = candidate.month
                year = candidate.year + (month // 12)
                month = (month % 12) + 1
                max_day = calendar.monthrange(year, month)[1]
                candidate = datetime(year, month, min(task_date.day, max_day), th, tm)
            elif repeat_mode == "annually":
                candidate = datetime(candidate.year + 1, task_date.month, task_date.day, th, tm)
            else:
                candidate += timedelta(days=1)
        return candidate

    def compose(self) -> ComposeResult:
        with Vertical(id="sched_dialog"):
            yield Label(" Scheduled Tasks", classes="pane_title")
            
            with VerticalScroll(id="main_scroll"):
                with Vertical(id="add_form"):
                    yield Label("Add New Scheduled Task:", classes="section_label")
                    with Horizontal(classes="form_row"):
                        agents = [(a, a) for a in self.agent_view.agent_manager.agents.keys()]
                        yield Select(agents, id="new_agent", prompt="Select Agent")
                        yield Input(placeholder="HH:MM (24h format, e.g., 14:30)", id="new_time")
                    with Horizontal(classes="form_row"):
                        yield Input(placeholder="YYYY-MM-DD (e.g., 2026-07-16, optional)", id="new_date")
                        repeats = [("Daily", "daily"), ("Weekly", "weekly"), ("Monthly", "monthly"), ("Annually", "annually")]
                        yield Select(repeats, value="daily", id="new_repeat", allow_blank=False)
                        
                    yield TextArea(id="new_prompt", show_line_numbers=False)
                
                yield Label("Saved Tasks:", classes="section_label")
                yield Vertical(id="task_list")
                
            with Horizontal(id="action_buttons"):
                yield Button("Add Task", id="add_task_btn", variant="success")
                yield Button("Close", id="close_btn", variant="error")

    def on_mount(self):
        self.refresh_list()
        
    def refresh_list(self):
        container = self.query_one("#task_list")
        container.query("*").remove()
        for t in self.agent_view.schedule_manager.tasks:
            agent = self.agent_view.agent_manager.get_agent(t.agent_name)
            a_color = agent.color if agent else "cyan"
            next_run = self._get_next_run(t)
            next_run_str = f"\nNext run: {next_run.strftime('%Y-%m-%d @ %H:%M')}" if next_run else f"Time: {t.time_str}"
            repeat_part = f" [{getattr(t, 'repeat', 'daily').title()}]"
            info = f"[bold {a_color}]{t.agent_name}[/]  [bold yellow]{next_run_str}[/bold yellow]{repeat_part}\n[dim]{t.prompt}[/dim]"
            row = Horizontal(
                Label(info, classes="task_info"),
                Button("Edit", id=f"edit_{t.id}", variant="primary", classes="task_edit_btn"),
                Button("Delete", id=f"del_{t.id}", variant="error", classes="task_del_btn"),
                classes="task_item"
            )
            container.mount(row)

    @on(Button.Pressed)
    def handle_buttons(self, event: Button.Pressed):
        btn_id = event.button.id
        if btn_id == "close_btn":
            self.dismiss()
        elif btn_id == "add_task_btn":
            agent = self.query_one("#new_agent", Select).value
            time_str = self.query_one("#new_time", Input).value.strip()
            prompt = self.query_one("#new_prompt", TextArea).text.strip()
            date_str = self.query_one("#new_date", Input).value.strip()
            repeat_val = self.query_one("#new_repeat", Select).value
            repeat = str(repeat_val) if repeat_val != Select.BLANK else "daily"
            
            if agent and Select.BLANK != agent and time_str and prompt:
                if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", time_str):
                    self.notify("Time must be a valid 24-hour time in HH:MM format (e.g., 14:30)", severity="error")
                    return
                
                if date_str:
                    try:
                        datetime.strptime(date_str, "%Y-%m-%d")
                    except ValueError:
                        self.notify("Date must be a valid calendar date in YYYY-MM-DD format (e.g., 2026-07-16)", severity="error")
                        return
                
                prompt = prompt.strip()
                
                self.agent_view.schedule_manager.add_task(agent, time_str, prompt, date_str=date_str, repeat=repeat)
                self.query_one("#new_time", Input).value = ""
                self.query_one("#new_date", Input).value = ""
                self.query_one("#new_prompt", TextArea).text = ""
                self.refresh_list()
                self.notify("Task added successfully.", severity="information")
                
        elif btn_id and btn_id.startswith("del_"):
            task_id = btn_id[4:]
            self.agent_view.schedule_manager.delete_task(task_id)
            self.refresh_list()
            
        elif btn_id and btn_id.startswith("edit_"):
            task_id = btn_id[5:]
            task = next((t for t in self.agent_view.schedule_manager.tasks if t.id == task_id), None)
            if task:
                self.query_one("#new_agent", Select).value = task.agent_name
                self.query_one("#new_time", Input).value = task.time_str
                self.query_one("#new_prompt", TextArea).text = task.prompt
                
                self.agent_view.schedule_manager.delete_task(task_id)
                self.refresh_list()
                self.notify("Loaded task into inputs for modification.", severity="information")

class ScheduledTaskPromptModal(ModalScreen[str]):
    DEFAULT_CSS = """
    ScheduledTaskPromptModal { align: center middle; background: $background 60%; }
    #sched_prompt_dialog { width: 65; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    .task_msg { margin: 1 0; text-style: bold; }
    .buttons { height: auto; align: right middle; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    """

    def __init__(self, task, agent_color="#00FFFF", **kwargs):
        super().__init__(**kwargs)
        self.sched_task = task
        self.agent_color = agent_color
        self.time_left = 60
        self.timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="sched_prompt_dialog"):
            yield Label(f" Scheduled Task Due: [bold {self.agent_color}]{self.sched_task.agent_name}[/]", classes="pane_title")
            yield Label(f"Auto-running in [bold yellow]{self.time_left}s[/bold yellow] if no action taken...", id="prompt_msg", classes="task_msg")
            with Horizontal(classes="buttons"):
                yield Button("Run Now", id="run_now", variant="success")
                yield Button("Defer 15 Min", id="defer", variant="warning")
                yield Button("Skip", id="skip", variant="error")

    def on_mount(self):
        self.query_one("#run_now").focus()
        self.timer = self.set_interval(1.0, self.update_countdown)

    def update_countdown(self):
        self.time_left -= 1
        if self.time_left <= 0:
            if self.timer:
                self.timer.stop()
            self.dismiss("run_now")
        else:
            try:
                self.query_one("#prompt_msg", Label).update(
                    f"Auto-running in [bold yellow]{self.time_left}s[/bold yellow] if no action taken..."
                )
            except Exception:
                pass

    @on(Button.Pressed)
    def handle_buttons(self, event: Button.Pressed):
        if self.timer:
            self.timer.stop()
        self.dismiss(event.button.id)

class ChatInput(TextArea):
    BINDINGS = [
        Binding("ctrl+a", "abort", "Abort", show=True, priority=True),
    ]

    class AbortRequest(Message):
        pass

    class Submitted(Message):
        def __init__(self, input_widget, value: str):
            super().__init__()
            self.input = input_widget
            self.value = value

    def action_abort(self) -> None:
        self.post_message(self.AbortRequest())

    def __init__(self, *args, **kwargs):
        kwargs.pop("suggester", None)
        kwargs.pop("placeholder", None)
        super().__init__(*args, **kwargs)
        self.show_line_numbers = False
        self._suggestion_matches = []
        self._suggestion_index = 0
        self._base_val = ""
        self._mode = "file"

    async def _on_message(self, message: Message) -> None:
        await super()._on_message(message)
        if message.__class__.__name__ == "Changed":
            self.handle_text_changed()

    def on_key(self, event: events.Key) -> None:
        if self._suggestion_matches:
            if event.key == "up":
                event.prevent_default()
                event.stop() 
                self.cycle_suggestions(-1)
                return
            elif event.key == "down":
                event.prevent_default()
                event.stop() 
                self.cycle_suggestions(1)
                return
            elif event.key == "right":
                event.prevent_default()
                event.stop() 
                self.commit_suggestion()
                return
        
        if event.key in ("shift+enter", "alt+enter", "ctrl+j"):
            event.prevent_default()
            self.insert("\n")
            
        elif event.key == "enter":
            event.prevent_default()
            event.stop()
            val = self.text.strip()
            self.post_message(self.Submitted(self, val))

    def handle_text_changed(self) -> None:
        val = self.text
        
        # Handle /theme suggestions
        if val.startswith("/theme"):
            if val.startswith("/theme ") or val == "/theme":
                partial_theme = val[len("/theme "):].strip().lower() if val.startswith("/theme ") else ""
                try:
                    available = list(getattr(self.app, "available_themes", {}).keys())
                except Exception:
                    available = []
                if not available:
                    try:
                        from textual.theme import BUILTIN_THEMES
                        available = list(BUILTIN_THEMES.keys())
                    except Exception:
                        available = ["tokyo-night", "monokai", "nord", "dracula", "gruvbox", "solarized-dark", "solarized-light", "textual-dark", "textual-light"]

                matches = [t for t in available if t.lower().startswith(partial_theme)]
                matches.sort()
                self._suggestion_matches = matches
                self._suggestion_index = 0
                self._base_val = "/theme "
                self._mode = "theme"
                self.update_suggestions_ui()
                return

        # Handle / for general slash commands
        if val.startswith("/"):
            from commands import SLASH_COMMANDS
            matches = [cmd for cmd in SLASH_COMMANDS if cmd.startswith(val)]
            matches.sort()
            self._suggestion_matches = matches
            self._suggestion_index = 0
            self._base_val = ""
            self._mode = "command"
            self.update_suggestions_ui()
            return

        last_amp = val.rfind("&")
        if last_amp != -1:
            partial_path = val[last_amp + 1:]
            if partial_path.replace(r"\ ", "").count(" ") == 0:
                try:
                    app = self.app
                    base_dir = str(app.query_one("#dir_tree").path) if app else os.getcwd()
                except:
                    base_dir = os.getcwd()
                search_str = partial_path.replace(r"\ ", " ")
                search_pattern = os.path.join(base_dir, search_str + "*")
                import glob
                matches = glob.glob(search_pattern)
                matches.sort()
                self._suggestion_matches = matches
                self._suggestion_index = 0
                self._base_val = val[:last_amp + 1]
                self._mode = "file"
                self.update_suggestions_ui()
                return
                
        last_at = val.rfind("@")
        if last_at != -1:
            partial_agent = val[last_at + 1:]
            if " " not in partial_agent:
                try:
                    agent_view = self.app.query_one("AIAgentView")
                    agent_names = list(agent_view.agent_manager.agents.keys()) + ["team", "room"]
                    matches = [name for name in agent_names if name.lower().startswith(partial_agent.lower())]
                    matches.sort()
                    self._suggestion_matches = matches
                    self._suggestion_index = 0
                    self._base_val = val[:last_at + 1]
                    self._mode = "agent"
                    self.update_suggestions_ui()
                    return
                except: pass

        self._suggestion_matches = []
        self.update_suggestions_ui()

    def _get_suggestion_desc(self, match: str, mode: str) -> str:
        if mode == "command":
            return SLASH_COMMAND_DESCS.get(match, "Slash Command")
        elif mode == "theme":
            try:
                theme_obj = getattr(self.app, "available_themes", {}).get(match)
                if theme_obj:
                    return f"{'Dark' if getattr(theme_obj, 'dark', True) else 'Light'} Theme"
            except Exception:
                pass
            return "Textual Theme"
        elif mode == "agent":
            if match == "team":
                return "Broadcast message to all registered agents"
            elif match == "room":
                return "Broadcast message to agents active in this session"
            try:
                agent_view = self.app.query_one("AIAgentView")
                agent = agent_view.agent_manager.get_agent(match)
                if agent:
                    backstory = agent.backstory
                    return backstory[:60] + "..." if len(backstory) > 60 else backstory
            except: pass
            return "AI Agent Persona"
        
        elif mode == "file":
            try:
                if os.path.isdir(match):
                    return "Directory"
                ext = os.path.splitext(match)[1].lower()
                desc = f"{ext.upper()[1:]} File" if ext else "File"
                try:
                    size_kb = os.path.getsize(match) / 1024
                    desc += f" ({size_kb:.1f} KB)"
                except: pass
                return desc
            except: pass
            return "File Path"
        return ""

    def update_suggestions_ui(self) -> None:
        try:
            preview = self.app.query_one("#ai_suggestions_preview")
            if self._suggestion_matches:
                preview.styles.display = "block"
                
                total = len(self._suggestion_matches)
                curr = self._suggestion_index
                if total <= 4:
                    start = 0
                    end = total
                else:
                    start = max(0, curr - 1)
                    end = start + 4
                    if end > total:
                        end = total
                        start = end - 4
                
                lines = []
                for i in range(start, end):
                    match = self._suggestion_matches[i]
                    desc = self._get_suggestion_desc(match, self._mode)
                    match_disp = os.path.basename(match) if self._mode == "file" else match
                    
                    if i == curr:
                        lines.append(f"[bold reverse]   {match_disp:<24} │ {desc:<50} [/bold reverse]")
                    else:
                        lines.append(f"   [dim]{match_disp:<24}[/dim] │ [dim]{desc:<50}[/dim]")
                
                preview.update("\n".join(lines))
            else:
                preview.styles.display = "none"
        except Exception:
            pass

    def cycle_suggestions(self, direction: int):
        if not self._suggestion_matches:
            return
        self._suggestion_index = (self._suggestion_index + direction) % len(self._suggestion_matches)
        self.update_suggestions_ui()

    def commit_suggestion(self) -> None:
        if not self._suggestion_matches:
            return
            
        match = self._suggestion_matches[self._suggestion_index]
        
        if self._mode == "file":
            try:
                app = self.app
                base_dir = str(app.query_one("#dir_tree").path) if app else os.getcwd()
            except:
                base_dir = os.getcwd()
                
            rel_path = os.path.relpath(match, base_dir).replace("\\", "/")
            if os.path.isdir(match):
                rel_path += "/"
            rel_path = rel_path.replace(" ", r"\ ")
            suggestion = rel_path
        else:
            suggestion = match
        
        with self.prevent(TextArea.Changed):
            self.text = self._base_val + suggestion
            lines = self.text.split("\n")
            self.cursor_location = (len(lines)-1, len(lines[-1]))
            
        self._suggestion_matches = []
        self.update_suggestions_ui()


_BANNER_STATS = None
_STATS_LOADING = False
_WELCOME_STATS_LOCK = threading.Lock()

def invalidate_stats_cache():
    global _BANNER_STATS
    with _WELCOME_STATS_LOCK:
        _BANNER_STATS = None

def calculate_stats_raw(agent_view) -> dict:
    from datetime import datetime, timedelta
    import glob
    import os
    import json

    def estimate_tokens(text: str) -> int:
        cached_enc = getattr(agent_view, "_tiktoken_encoding", None)
        if cached_enc is not None:
            try:
                return len(cached_enc.encode(text))
            except Exception:
                pass
        return len(text) // 4

    now = datetime.now()
    first_day_current_month = now.replace(day=1)
    last_day_last_month = first_day_current_month - timedelta(days=1)
    target_year = last_day_last_month.year
    target_month = last_day_last_month.month
    
    sessions_dir = toolbox.get_storage_path("sessions")
    if not os.path.exists(sessions_dir):
        os.makedirs(sessions_dir, exist_ok=True)
        
    session_files = glob.glob(os.path.join(sessions_dir, "*.json"))
    
    agent_input_tokens = {}
    agent_output_tokens = {}
    agent_total_tools = {}
    agent_successful_tools = {}
    agent_collabs = {}
    total_conversations_all_time = set()
    total_conversations_target_month = set()
    
    valid_agents = set(agent_view.agent_manager.agents.keys())
    
    for filepath in session_files:
        base = os.path.basename(filepath).replace(".json", "")
        parts = base.split("_")
        if len(parts) < 3:
            continue
            
        session_id = f"{parts[0]}_{parts[1]}"
        total_conversations_all_time.add(session_id)
        
        agent_name = "_".join(parts[2:])
        matched_agent = None
        for a in valid_agents:
            if a.replace(" ", "_") == agent_name or a.lower() == agent_name.lower():
                matched_agent = a
                break
        if not matched_agent:
            matched_agent = agent_name.replace("_", " ").title()
            
        try:
            ts = int(parts[1])
            file_dt = datetime.fromtimestamp(ts)
        except (ValueError, IndexError):
            try:
                mtime = os.path.getmtime(filepath)
                file_dt = datetime.fromtimestamp(mtime)
            except Exception:
                continue
                
        is_target_month = (file_dt.year == target_year and file_dt.month == target_month)
        
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            continue
            
        for msg in history:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content") or ""
            
            msg_tokens = estimate_tokens(content)
            
            if is_target_month:
                total_conversations_target_month.add(session_id)
                t_calls = msg.get("tool_calls") or []
                t_outs = msg.get("tool_outputs") or []
                
                agent_total_tools[matched_agent] = agent_total_tools.get(matched_agent, 0) + len(t_calls)
                for out in t_outs:
                    if isinstance(out, dict):
                        content_lower = str(out.get("content", "")).lower()
                        if not any(term in content_lower for term in ["error:", "exception:", "failed:", "failed to"]):
                            agent_successful_tools[matched_agent] = agent_successful_tools.get(matched_agent, 0) + 1
                
                if role == "human" or role == "system":
                    agent_input_tokens[matched_agent] = agent_input_tokens.get(matched_agent, 0) + msg_tokens
                    for out in t_outs:
                        if isinstance(out, dict):
                            agent_input_tokens[matched_agent] = agent_input_tokens.get(matched_agent, 0) + estimate_tokens(out.get("content", ""))
                elif role == "ai":
                    agent_output_tokens[matched_agent] = agent_output_tokens.get(matched_agent, 0) + msg_tokens
                    
                if role == "ai" and content:
                    mentions = agent_view.agent_manager.get_mentions(content)
                    if mentions:
                        if matched_agent not in agent_collabs:
                            agent_collabs[matched_agent] = set()
                        for m in mentions:
                            if m != matched_agent:
                                agent_collabs[matched_agent].add(m)

    used_month_name = last_day_last_month.strftime("%B")
    if not agent_input_tokens and not agent_output_tokens:
        target_year = now.year
        target_month = now.month
        used_month_name = now.strftime("%B")
        
        for filepath in session_files:
            base = os.path.basename(filepath).replace(".json", "")
            parts = base.split("_")
            if len(parts) < 3:
                continue
            session_id = f"{parts[0]}_{parts[1]}"
            agent_name = "_".join(parts[2:])
            matched_agent = None
            for a in valid_agents:
                if a.replace(" ", "_") == agent_name or a.lower() == agent_name.lower():
                    matched_agent = a
                    break
            if not matched_agent:
                matched_agent = agent_name.replace("_", " ").title()
                
            try:
                ts = int(parts[1])
                file_dt = datetime.fromtimestamp(ts)
            except (ValueError, IndexError):
                try:
                    mtime = os.path.getmtime(filepath)
                    file_dt = datetime.fromtimestamp(mtime)
                except Exception:
                    continue
                    
            is_target_month = (file_dt.year == target_year and file_dt.month == target_month)
            if not is_target_month:
                continue
                
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    history = json.load(f)
                if not isinstance(history, list):
                    history = []
            except Exception:
                continue
                
            for msg in history:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                content = msg.get("content") or ""
                
                msg_tokens = estimate_tokens(content)
                t_calls = msg.get("tool_calls") or []
                t_outs = msg.get("tool_outputs") or []
                
                agent_total_tools[matched_agent] = agent_total_tools.get(matched_agent, 0) + len(t_calls)
                for out in t_outs:
                    if isinstance(out, dict):
                        content_lower = str(out.get("content", "")).lower()
                        if not any(term in content_lower for term in ["error:", "exception:", "failed:", "failed to"]):
                            agent_successful_tools[matched_agent] = agent_successful_tools.get(matched_agent, 0) + 1
                
                if role == "human" or role == "system":
                    agent_input_tokens[matched_agent] = agent_input_tokens.get(matched_agent, 0) + msg_tokens
                    for out in t_outs:
                        if isinstance(out, dict):
                            agent_input_tokens[matched_agent] = agent_input_tokens.get(matched_agent, 0) + estimate_tokens(out.get("content", ""))
                elif role == "ai":
                    agent_output_tokens[matched_agent] = agent_output_tokens.get(matched_agent, 0) + msg_tokens
                
                if role == "ai" and content:
                    mentions = agent_view.agent_manager.get_mentions(content)
                    if mentions:
                        if matched_agent not in agent_collabs:
                            agent_collabs[matched_agent] = set()
                        for m in mentions:
                            if m != matched_agent:
                                    agent_collabs[matched_agent].add(m)

    research_input_tokens = 0
    research_output_tokens = 0
    try:
        workspace_dir = agent_view.query_one("#dir_tree").path if agent_view else os.getcwd()
        research_dir = os.path.join(workspace_dir, "research")
        if os.path.exists(research_dir):
            for root, dirs, files in os.walk(research_dir):
                log_path = os.path.join(root, "research_status.log")
                if os.path.exists(log_path):
                    try:
                        with open(log_path, "r", encoding="utf-8", errors="ignore") as lf:
                            for line in lf:
                                if "[FETCH]" in line:
                                    research_input_tokens += 6000
                    except Exception:
                        pass
                
                for file in files:
                    if file.endswith(".md") and file != "research_status.log":
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, "r", encoding="utf-8", errors="ignore") as mf:
                                md_content = mf.read()
                                research_output_tokens += estimate_tokens(md_content)
                        except Exception:
                            pass
    except Exception:
        pass

    return {
        "agent_input_tokens": agent_input_tokens,
        "agent_output_tokens": agent_output_tokens,
        "agent_total_tools": agent_total_tools,
        "agent_successful_tools": agent_successful_tools,
        "agent_collabs": agent_collabs,
        "total_conversations_all_time": total_conversations_all_time,
        "total_conversations_target_month": total_conversations_target_month,
        "research_input_tokens": research_input_tokens,
        "research_output_tokens": research_output_tokens,
        "used_month_name": used_month_name,
        "session_files": session_files
    }

def trigger_stats_loading(agent_view):
    global _BANNER_STATS, _STATS_LOADING
    with _WELCOME_STATS_LOCK:
        if _STATS_LOADING:
            return
        _STATS_LOADING = True
        
    def run():
        global _BANNER_STATS, _STATS_LOADING
        try:
            stats = calculate_stats_raw(agent_view)
            _BANNER_STATS = stats
            
            def update_ui():
                try:
                    banners = agent_view.query(".welcome_banner_box")
                    if banners:
                        for banner in banners:
                            active_agent_name = getattr(agent_view, "active_agent", None)
                            spec_name = active_agent_name.name if active_agent_name else None
                            new_renderable = get_welcome_banner(agent_view, specific_agent=spec_name, return_renderable=True)
                            banner.update(new_renderable)
                except Exception:
                    pass
            agent_view.app.call_from_thread(update_ui)
        except Exception:
            pass
        finally:
            with _WELCOME_STATS_LOCK:
                _STATS_LOADING = False
                
    threading.Thread(target=run, daemon=True).start()

def get_welcome_banner(agent_view, specific_agent: str = None, return_renderable: bool = False):
    global _BANNER_STATS, _STATS_LOADING
    
    if _BANNER_STATS is None:
        if not _STATS_LOADING:
            trigger_stats_loading(agent_view)
            
        from rich.console import Group
        from rich.text import Text
        from rich.table import Table
        from rich.rule import Rule
        
        title_text = Text.from_markup("[bold #f2a813]\u276f [/bold #f2a813][bold #da6057]FEDERaiDE[/bold #da6057]\n\u00a9 Rock Lab Private Limited", justify="center")
        divider = Rule(style="#f2a813")
        
        loading_table = Table(show_header=False, expand=True, box=None, padding=(0, 2))
        loading_table.add_column(ratio=1, justify="center")
        loading_table.add_row(Text.from_markup("[dim] Processing agent telemetry & stats...[/dim]"))
        
        tips_text = Text.from_markup(
            "  [bold #f2a813]Tips for getting started:[/bold #f2a813]\n"
            "  1. Ask questions, edit files, or run commands.\n"
            "  2. Use & to inject files. Use @ to invoke particular agents.\n"
            "  3. Press F4 to configure the active agent.\n"
            "  4. Press Ctrl+K to start a fresh conversation."
        )
        
        renderable_group = Group(
            title_text,
            divider,
            loading_table,
            divider,
            tips_text
        )
        if return_renderable:
            return renderable_group
        return Static(renderable_group, classes="welcome_banner_box")

    stats = _BANNER_STATS
    agent_input_tokens = stats["agent_input_tokens"]
    agent_output_tokens = stats["agent_output_tokens"]
    agent_total_tools = stats["agent_total_tools"]
    agent_successful_tools = stats["agent_successful_tools"]
    agent_collabs = stats["agent_collabs"]
    total_conversations_all_time = stats["total_conversations_all_time"]
    total_conversations_target_month = stats["total_conversations_target_month"]
    research_input_tokens = stats["research_input_tokens"]
    research_output_tokens = stats["research_output_tokens"]
    used_month_name = stats["used_month_name"]
    session_files = stats["session_files"]

    agent_total_tokens = {}
    for agent in set(list(agent_input_tokens.keys()) + list(agent_output_tokens.keys())):
        agent_total_tokens[agent] = agent_input_tokens.get(agent, 0) + agent_output_tokens.get(agent, 0)

    if specific_agent:
        best_agent = specific_agent
        inp_tokens = agent_input_tokens.get(best_agent, 0)
        out_tokens = agent_output_tokens.get(best_agent, 0)
        collabs = list(agent_collabs.get(best_agent, []))
        total_convs = sum(1 for f in session_files if best_agent.replace(" ", "_").lower() in f.lower())
    elif agent_total_tokens:
        best_agent = max(agent_total_tokens, key=agent_total_tokens.get)
        inp_tokens = agent_input_tokens.get(best_agent, 0) + research_input_tokens
        out_tokens = agent_output_tokens.get(best_agent, 0) + research_output_tokens
        collabs = list(agent_collabs.get(best_agent, []))
        total_convs = len(total_conversations_all_time)
    else:
        best_agent = agent_view.active_agent.name if getattr(agent_view, "active_agent", None) else "Rita"
        inp_tokens = research_input_tokens
        out_tokens = research_output_tokens
        collabs = []
        total_convs = len(total_conversations_all_time)

    def get_agent_color(name: str) -> str:
        agent = agent_view.agent_manager.get_agent(name)
        return agent.color if agent else "#00FFFF"

    best_agent_color = get_agent_color(best_agent)
    
    colored_collabs = []
    for c in collabs:
        c_color = get_agent_color(c)
        colored_collabs.append(f"[bold {c_color}]{c}[/]")
    collaborations = ", ".join(colored_collabs) if colored_collabs else "None"

    safe_name = best_agent.replace(" ", "_")
    skills_dir = toolbox.get_storage_path("agents", "skills", safe_name)
    passive_count = 0
    active_count = 0
    if os.path.exists(skills_dir):
        try:
            active_tools_dir = os.path.join(skills_dir, "active_tools")
            active_list = []
            if os.path.exists(active_tools_dir):
                active_list = [d for d in os.listdir(active_tools_dir) if os.path.isdir(os.path.join(active_tools_dir, d))]
            active_count = len(active_list)
            
            all_mds = [f.replace(".md", "") for f in os.listdir(skills_dir) if f.endswith(".md")]
            passive_count = sum(1 for m in all_mds if m not in active_list)
        except Exception:
            pass

    total_tools_run = agent_total_tools.get(best_agent, 0)
    successful_tools_run = agent_successful_tools.get(best_agent, 0)
    
    t_pct = int((successful_tools_run / total_tools_run) * 100) if total_tools_run > 0 else 0

    from rich.console import Group
    from rich.text import Text
    from rich.table import Table
    from rich.rule import Rule

    title_text = Text.from_markup(f"[bold #f2a813]\u276f [/bold #f2a813][bold #da6057]FEDERaiDE[/bold #da6057] [dim]v{get_installed_version()}[/dim]\n\u00a9 Rock Lab Private Limited", justify="center")
    divider = Rule(style="#f2a813")

    table = Table(
        show_header=False,
        expand=True,
        box=None,
        padding=(0, 2),
        border_style="#f2a813"
    )
    table.add_column(ratio=1, justify="left")
    table.add_column(ratio=1, justify="left")

    title_prefix = "Active Agent Stats:" if specific_agent else "Agent of the Month:"
    month_suffix = " [dim][/]" if specific_agent else f" [dim]({used_month_name})[/]"
    
    table.add_row(
        Text.from_markup(f"[bold #e98435]{title_prefix}[/bold #e98435]"),
        Text.from_markup(f"[bold {best_agent_color}]{best_agent}[/]{month_suffix}")
    )
    table.add_row(
        Text.from_markup("[bold #e07246]Processed Tokens:[/bold #e07246]"),
        Text.from_markup(f"[green]{inp_tokens:,}[/] / [blue]{out_tokens:,}[/]")
    )
    table.add_row(
        Text.from_markup("[bold #da6057]Total Conversations:[/bold #da6057]"),
        Text.from_markup(f"{total_convs:,}")
    )
    table.add_row(
        Text.from_markup("[bold #d44e68]Tool Calls:[/bold #d44e68]"),
        Text.from_markup(f"{successful_tools_run:,} / {total_tools_run:,} [dim]({t_pct}%)[/dim]")
    )
    table.add_row(
        Text.from_markup("[bold #ce3c79]Skills:[/bold #ce3c79]"),
        Text.from_markup(f"[blue]{passive_count}[/] / [red]{active_count}[/]")
    )
    table.add_row(
        Text.from_markup("[bold #c82a8a]Collaborators:[/bold #c82a8a]"),
        Text.from_markup(collaborations)
    )

    tips_text = Text.from_markup(
        "  [bold #f2a813]Tips for getting started:[/bold #f2a813]\n"
        "  1. Ask questions, edit files, or run commands.\n"
        "  2. Use & to inject files. Use @ to invoke particular agents.\n"
        "  3. Press F4 to configure the active agent.\n"
        "  4. Press Ctrl+K to start a fresh conversation."
    )

    renderable_group = Group(
        title_text,
        divider,
        table,
        divider,
        tips_text
    )

    if return_renderable:
        return renderable_group
    return Static(renderable_group, classes="welcome_banner_box")

def render_latex_to_unicode(text: str) -> str:
    if "$" not in text:
        return text
    try:
        from pylatexenc.latex2text import LatexNodes2Text
        converter = LatexNodes2Text()
        
        def replace_block(match):
            try: return "\n" + converter.latex_to_text(match.group(1).strip()) + "\n"
            except: return match.group(0)
        text = re.sub(r'\$\$(.*?)\$\$', replace_block, text, flags=re.DOTALL)
        
        def replace_inline(match):
            try: return converter.latex_to_text(match.group(1).strip())
            except: return match.group(0)
        text = re.sub(r'(?<![\w\\])\$([^$\n]+?)\$(?!\w)', replace_inline, text)
        
        return text
    except ImportError:
        return text

class AIAgentView(Vertical):
    BINDINGS =[
        Binding("f2", "open_chat_manager", "Sessions", priority=True),
        Binding("ctrl+k", "clear_all_contexts", "New Chat", priority=True),
        Binding("f4", "open_active_config", "Manage Agents", priority=True),
        Binding("f5", "switch_agent", "Switch Agent", priority=True),
        Binding("ctrl+t", "cycle_arm_mode", "Cycle Mode", priority=True),
        Binding("ctrl+g", "cycle_agents", "Cycle Agents", priority=True),
        Binding("ctrl+a", "abort", "Abort", priority=True),
        Binding("f3", "open_global_settings", "Settings", priority=True),
    ]

    DEFAULT_CSS = """
    AIAgentView { width: 100%; height: 100%; background: $background; padding: 0 1; }
    
    #ai_chat_scroll { height: 1fr; width: 100%; overflow-y: scroll; scrollbar-gutter: stable; }
    #chat_messages { height: auto; width: 100%; }
    .chat_msg { margin-bottom: 1; height: auto; width: 100%; }
    #ai_thinking_spinner { height: 1; display: none; color: $warning; margin: 0; padding-left: 1; }
    
    #progress_container { 
        display: none; 
        height: 12;
        border: solid $primary; 
        background: $surface;
        margin: 1 0;
        layout: horizontal;
    }
    #progress_left {
        width: 50%;      
        height: 100%;
        padding: 1;
        overflow-y: auto;
    }
    #progress_right {
        width: 50%;
        height: 100%;
        border-left: solid $primary;
        background: $boost;
    }
    .task_row { height: 1; margin-bottom: 1; layout: horizontal; }
    .task_spinner { width: 3; color: $warning; text-style: bold; }
    ProgressBar { width: 1fr; }
    
    #input_container { 
        height: auto; 
        min-height: 3; 
        max-height: 7; 
        width: 100%; 
        border-top: solid #5f9ea0; 
        border-bottom: solid #5f9ea0; 
        layout: horizontal; 
    }
    #prompt_label { 
        color: #dda0dd; 
        text-style: bold; 
    }
    #ai_chat_input { 
        width: 1fr; 
        height: auto; 
        min-height: 1; 
        max-height: 5; 
        border: none; 
        background: transparent; 
        padding: 0; 
    }
    #ai_suggestions_preview {
        display: none;
        background: $boost;
        border: round $primary;
        height: auto;
        max-height: 6; 
        padding: 0 1;
        margin-top: 0;
    }
    #status_bar { 
        height: auto; 
        min-height: 1; 
        width: 100%; 
        layout: grid; 
        grid-size: 3;
        grid-columns: 1fr 1fr 1fr;
    }
    .status_left { 
        width: 100%;
        color: #87cefa; 
        text-align: left;
        content-align: left top;
    }
    .status_center { 
        width: 100%;
        color: $text;
        content-align: center top; 
        text-align: center;
    }
    .status_right { 
        width: 100%;
        color: #dda0dd; 
        text-align: right;
        content-align: right top;
    }
    .message_block {
        height: auto;
        width: 100%;
        margin-bottom: 0;
    }
    .tool_result_box {
        border: round $accent;
        background: $boost;
        padding: 0 1;
        margin: 1 0;
        color: #808080;
    }
    .welcome_banner_box {
        border: round #f2a813;
        background: $boost;
        padding: 0 0;
        margin: 0 0 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="ai_chat_scroll"):
            yield Vertical(id="chat_messages")
            with Horizontal(id="progress_container"):
                yield Vertical(id="progress_left")
                yield RichLog(id="progress_right", markup=False, wrap=True, auto_scroll=True)
                
        yield Label("Agent is working...", id="ai_thinking_spinner")
        
        with Horizontal(id="input_container"):
            yield Label(">", id="prompt_label")
            yield ChatInput(placeholder=" Type your message, &path, or @agent", id="ai_chat_input", suggester=ChatSuggester(lambda: self.app))
            
        yield Static(id="ai_suggestions_preview") 
            
        with Horizontal(id="status_bar"):
            yield Label(f"{os.getcwd()}", id="ai_cwd_label", classes="status_left")
            yield Label("", id="ai_config_label", classes="status_center")
            yield Label("", id="ai_token_label", classes="status_right")

    def on_mount(self):
        toolbox.CURRENT_APP = self.app
        toolbox.CURRENT_AGENT_VIEW = self
        toolbox.CURRENT_LOG_CB = self.log_to_ui
        self.current_tokens = 0
        
        self.agent_manager = AgentManager()
        self.session_manager = SessionManager()
        self.schedule_manager = ScheduleManager()
        self.current_batch_id = 0
        
        default_name = self.agent_manager.get_default_agent_name()
        initial_agent = self.agent_manager.get_agent(default_name) or list(self.agent_manager.agents.values())[0]
        self.select_agent(initial_agent.name)

        if toolbox.is_keyring_locked():
            def handle_initial_unlock(result):
                if result:
                    action, password = result
                    if action == "unlock" and password:
                        if toolbox.unlock_keyring(password):
                            self.notify("Keyring unlocked.", severity="information")
                            self.update_status_bar()
                            if hasattr(self, "telegram_manager"):
                                self.telegram_manager.reload_config()
                            self.check_onboarding()
                            self.check_chatgpt_oauth_status()
                        else:
                            self.notify("Unlock failed. Stored keys may be unavailable.", severity="error")
                            self.check_onboarding()
                    elif action == "reset" and password:
                        try:
                            import keyring, os
                            backend = keyring.get_keyring()
                            backends = [backend]
                            if hasattr(backend, 'backends'):
                                backends.extend(backend.backends)
                                
                            for b in backends:
                                if type(b).__name__ == "EncryptedKeyring":
                                    if hasattr(b, "file_path") and b.file_path and os.path.exists(b.file_path):
                                        os.remove(b.file_path)
                                    b.__dict__['keyring_key'] = password
                            
                            self.notify("Keyring initialized successfully. New password established.", severity="warning")
                            self.update_status_bar()
                            self.check_onboarding()
                            self.check_chatgpt_oauth_status()
                        except Exception as e:
                            self.notify(f"Reset failed: {e}", severity="error")
            
            self.app.push_screen(KeyringUnlockModal(), handle_initial_unlock)
        else:
            self.check_onboarding()

        
        self.agent_executors = {}
        self.shell_mode = False
        self.agent_mode = "PLAN"
        self._running_task_count = 0
        self._running_agents = set()
        
        self.tts_enabled = False
        self.stt_enabled = False
        self.tts_manager = TTSManager()
        self.stt_append_history =[] 
        self.stt_manager = STTManager(
            callback=self.handle_stt_input, 
            log_callback=self.log_to_ui,
            tts_manager=self.tts_manager
        )
        
        self.telegram_manager = TelegramManager(
            callback=self.handle_telegram_input,
            log_callback=self.log_to_ui
        )

        self.turn_queue = []
        self.paused_queue = []
        self.turn_lock = threading.Lock()

        self._write_log(get_welcome_banner(self))
        self.update_tokens()
        resume_offset = parse_resume_index()
        if resume_offset is not None:
            self.call_after_refresh(lambda: self.action_resume_last(offset=resume_offset))
        self.query_one("#ai_chat_input").focus()
        
        self.spinner_chars =["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.spinner_idx = 0
        self.set_interval(0.1, self.tick_spinners)
        self.set_interval(60.0, self.tick_scheduler)
        if toolbox.load_global_settings().get("autoupdate_on_launch", False):
            self.check_for_updates_bg(manual=False)

    def check_chatgpt_oauth_status(self, agent=None):
        if getattr(self, "_chatgpt_auth_modal_open", False):
            return
        if toolbox.is_keyring_locked():
            return
        target_agent = agent or getattr(self, "active_agent", None)
        if target_agent and is_chatgpt_oauth_agent(target_agent) and not has_valid_chatgpt_token():
            self._chatgpt_auth_modal_open = True
            def on_auth_done(success: bool):
                self._chatgpt_auth_modal_open = False
                if success:
                    self.notify(f"ChatGPT Subscription authorized for {target_agent.name}!", severity="information")
                    self.update_status_bar()
                else:
                    self.notify(f"ChatGPT OAuth authentication required for {target_agent.name}.", severity="warning")
            self.app.push_screen(ChatGPTAuthModal(), on_auth_done)

    def ensure_chatgpt_auth_for_agent(self, agent) -> bool:
        if not is_chatgpt_oauth_agent(agent):
            return True
        if has_valid_chatgpt_token():
            return True
        
        event = threading.Event()
        result_box = [False]
        
        def on_done(ok: bool):
            result_box[0] = ok
            event.set()
            
        def push_modal():
            if getattr(self, "_chatgpt_auth_modal_open", False):
                return
            self._chatgpt_auth_modal_open = True
            self.notify(f"ChatGPT Subscription authentication required for {agent.name}.", severity="warning")
            def on_modal_done(ok: bool):
                self._chatgpt_auth_modal_open = False
                on_done(ok)
            self.app.push_screen(ChatGPTAuthModal(), on_modal_done)
            
        self.app.call_from_thread(push_modal)
        
        while not event.is_set():
            if toolbox.ABORT_EVENT.is_set():
                return False
            event.wait(0.1)
            
        return result_box[0]

    def select_agent(self, name: str) -> bool:
        agent = self.agent_manager.get_agent(name)
        if agent:
            self.active_agent = agent
            
            try:
                input_container = self.query_one("#input_container")
                prompt_label = self.query_one("#prompt_label")
                
                input_container.styles.border_top = ("solid", agent.color)
                input_container.styles.border_bottom = ("solid", agent.color)
                prompt_label.styles.color = agent.color
            except Exception:
                pass
            
            self.update_tokens()
            self.check_chatgpt_oauth_status(agent)
            return True
        return False
    
    def confirm_tool_execution(self, tool_name: str, arguments: dict, agent_name: str = "Agent") -> bool:
        result_event = threading.Event()
        final_result = [False]

        def handle_result(res: Any):
            if res == "stop":
                self.action_abort()
                final_result[0] = False
            else:
                final_result[0] = (res == "approve")
            result_event.set()

        def push_modal():
            modal = ToolConfirmationModal(tool_name, arguments, agent_name=agent_name)
            self.app.push_screen(modal, handle_result)

        self.app.call_from_thread(push_modal)
        
        while not result_event.is_set():
            if toolbox.ABORT_EVENT.is_set():
                return False
            result_event.wait(0.1) 
            
        return final_result[0]
    
    def action_clear_all_contexts(self):
        self.action_abort() 
        self.session_manager.clear_all_contexts()
        self.agent_executors = {} 
        self.agent_mode = "PLAN"
        self.clear_chat_ui()
        invalidate_stats_cache() 
        self._write_log(Rule(title="[bold yellow]ALL CONTEXTS CLEARED", style="dim"))
        self._write_log(get_welcome_banner(self))
        self.update_tokens()
    
    def action_resume_last(self, offset: int = 1):
        if offset <= 0:
            return  

        all_files = glob.glob(toolbox.get_storage_path("sessions", "*.json"))
        curr_sess_id = getattr(self.session_manager, "current_session_id", "")
        
        sessions_map = {}
        for f in all_files:
            base = os.path.basename(f).replace(".json", "")
            parts = base.split("_")
            if len(parts) >= 2:
                sess_id = f"{parts[0]}_{parts[1]}"
                if sess_id == curr_sess_id:
                    continue
                if sess_id not in sessions_map:
                    sessions_map[sess_id] = []
                sessions_map[sess_id].append(f)
                
        if not sessions_map:
            self.log_to_ui("[bold red]No past chat sessions found to resume.[/bold red]")
            return

        sorted_sessions = sorted(
            sessions_map.items(),
            key=lambda item: max(os.path.getmtime(f) for f in item[1]),
            reverse=True
        )

        idx = offset - 1
        if 0 <= idx < len(sorted_sessions):
            target_files = sorted_sessions[idx][1]
            target_file = target_files[0]
            active = getattr(self, "active_agent", None)
            for f in target_files:
                if active and active.name.lower() in os.path.basename(f).lower():
                    target_file = f
                    break
            self.load_chat_file(target_file)
        else:
            self.log_to_ui(f"[bold red]Cannot resume session -r {offset}: only {len(sorted_sessions)} past session(s) found.[/bold red]")
        
    def action_abort(self):
        toolbox.ABORT_EVENT.set()
        
        if hasattr(self, "current_batch_id"):
            self.session_manager.abort_batch(self.current_batch_id)

        toolbox.nuke_all_threads()

        self._running_agents.clear()
        if self.workers:
            self.workers.cancel_all()
            self._toggle_spinner(False)
            
            try:
                self.query_one("#progress_container").styles.display = "none"
            except: pass

            self.log_to_ui("[bold red] Operation Aborted by User.[/bold red]")
            self.query_one("#ai_chat_input").focus()
        
    def action_open_chat_manager(self):
        def handle_chat_mgr(action):
            if action == "new_session":
                self.action_clear_all_contexts()
            elif action == "load_chat":
                self.app.push_screen(ChatLoadModal(), self.load_chat_file)
        self.app.push_screen(ChatManagerModal(), handle_chat_mgr)

    def action_switch_agent(self):
        names = list(self.agent_manager.agents.keys())
        default = self.agent_manager.get_default_agent_name()
        def handle_switch(res):
            if res:
                self.select_agent(res["name"])
                if res["default"]:
                    self.agent_manager.set_default_agent_name(res["name"])
                    self.log_to_ui(f"[bold #f2a813]Default agent set to: {res['name']}[/bold #f2a813]")
                else:
                    self.log_to_ui(f"[bold green]Switched active agent to: {res['name']}[/bold green]")
                new_renderable = get_welcome_banner(self, specific_agent=res["name"], return_renderable=True)
                banners = self.query(".welcome_banner_box")
                if banners:
                    for banner in banners:
                        banner.update(new_renderable)
                else:
                    from textual.widgets import Static
                    self._write_log(Static(new_renderable, classes="welcome_banner_box"))
        self.app.push_screen(SwitchAgentModal(names, default), handle_switch)

    
    def load_chat_file(self, filepath: str):
        if not filepath or not os.path.exists(filepath): return
        try:
            base = os.path.basename(filepath).replace(".json", "")
            parts = base.split("_")
            if len(parts) < 2: return
            sess_id = f"{parts[0]}_{parts[1]}"
            owner_raw = "_".join(parts[2:]) if len(parts) >= 3 else parts[-1]
            
            matched_owner = self.agent_manager.get_agent(owner_raw) or self.agent_manager.get_agent(owner_raw.replace("_", " "))
            owner_name = matched_owner.name if matched_owner else owner_raw.replace("_", " ")

            # 1. Abort background jobs and cleanly purge the previous session memory
            self.action_abort()
            self.session_manager.active_sessions.clear()
            self.agent_executors.clear()
            with self.turn_lock:
                self.turn_queue.clear()
                self.paused_queue.clear()

            self.session_manager.current_session_id = sess_id

            # 2. Discover and load ONLY the agents that participated in this saved session on disk
            matching_files = glob.glob(os.path.join(self.session_manager.sessions_dir, f"{sess_id}_*.json"))
            if not matching_files:
                matching_files = [filepath]

            for sf in matching_files:
                sf_base = os.path.basename(sf).replace(".json", "")
                sf_parts = sf_base.split("_")
                sf_owner_raw = "_".join(sf_parts[2:]) if len(sf_parts) >= 3 else sf_parts[-1]
                sf_matched = self.agent_manager.get_agent(sf_owner_raw) or self.agent_manager.get_agent(sf_owner_raw.replace("_", " "))
                sf_agent_name = sf_matched.name if sf_matched else sf_owner_raw.replace("_", " ")

                try:
                    with open(sf, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    valid_msgs = []
                    for m in data:
                        if isinstance(m, dict):
                            clean_m = {k: v for k, v in m.items() if k in HistoryMessage.__dataclass_fields__}
                            valid_msgs.append(HistoryMessage(**clean_m))
                    self.session_manager.active_sessions[sf_agent_name] = valid_msgs
                except Exception:
                    pass

            # 3. Ensure the primary agent of the loaded file is selected
            if owner_name not in self.session_manager.active_sessions and self.session_manager.active_sessions:
                owner_name = list(self.session_manager.active_sessions.keys())[0]

            self.select_agent(owner_name)
            owner_history = self.session_manager.active_sessions.get(owner_name, [])
            self.replay_chat(owner_history, owner_name)
            self.log_to_ui(f"[bold green]Restored Session: {sess_id} (agent: {owner_name})[/bold green]")
            self.update_status_bar()
        except Exception as e:
            self.log_to_ui(f"[bold red]Load Error:[/bold red] {e}")

    def replay_chat(self, history: List[HistoryMessage], owner_name: str):
        self.query_one("#chat_messages").query("*").remove()
        self._write_log(Rule(title=f"[bold #f2a813]SESSION RESTORED: {owner_name.upper()}", style="dim"))

        for hm in history:
            if hm.role == "system": continue

            content = hm.content
            label = "User"
            color = "blue"

            if hm.role == "ai":
                agent = self.agent_manager.get_agent(owner_name)
                color = agent.color if agent else "magenta"

                if content and content.strip():
                    self._write_message_block(f"[bold {color}]{owner_name}:[/bold {color}]", content, color, is_markdown=True)

                if hm.tool_calls:
                    for tc in hm.tool_calls:
                        tc_name = tc.get("name", "tool")
                        tc_args = str(tc.get("args", {}))
                        call_text = f"[#808080]Calling Tool: {escape(tc_name)} with args: {escape(tc_args)}[/#808080]"
                        self._write_message_block(f"[bold {color}]{owner_name} (Tool Call):[/bold {color}]", call_text, color, is_markdown=False)

                if hm.tool_outputs:
                    for out in hm.tool_outputs:
                        t_name = out.get("name", "tool")
                        t_content = str(out.get("content", ""))
                        if t_name in ["search_web", "SearchWeb"]:
                            summary = "[Search results successfully parsed and delivered to active agent context]"
                        else:
                            summary_clean = re.sub(r'\[ImageBase64:\s*[^\]]+\]', '[ImageBase64: <data_transmitted>]', t_content)
                            summary_clean = re.sub(r'data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=\s]{20,}', '<base64_data_omitted>', summary_clean)
                            summary = (summary_clean + '...') if len(summary_clean) > 200 else summary_clean

                        box_content = f"[bold]Tool Result ({owner_name}):[/bold]\n{escape(summary)}"
                        box_widget = Static(Text.from_markup(box_content), classes="tool_result_box", markup=False)
                        box_widget.styles.border = ("round", color)
                        self._write_log(box_widget)
            else: 
                intercom_match = re.search(r'<AGENT_INTERCOM sender="([^"]+)">([\s\S]*?)</AGENT_INTERCOM>', content)
                tool_match = re.search(r'<AGENT_INTERCOM_TOOL_RESPONSE agent="([^"]+)" tool="([^"]+)"[^>]*>([\s\S]*?)</AGENT_INTERCOM_TOOL_RESPONSE>', content)

                if intercom_match:
                    label = intercom_match.group(1) 
                    synced_agent = self.agent_manager.get_agent(label)
                    color = synced_agent.color if synced_agent else "cyan"
                    content = intercom_match.group(2).strip()
                elif tool_match:
                    label = f"{tool_match.group(1)} (Tool: {tool_match.group(2)})"
                    synced_agent = self.agent_manager.get_agent(tool_match.group(1))
                    color = synced_agent.color if synced_agent else "bright_black"
                    content = tool_match.group(3).strip()
                else:
                    user_cfg = toolbox.load_global_settings()
                    label = user_cfg.get("user_name", "User")
                    color = user_cfg.get("user_color", "#dda0dd")
                
                content = re.sub(r'^\s*(?:\[(?:Time|Today\'s date)[^\]]*\]\s*)+', '', content, flags=re.IGNORECASE).strip()
                self._write_message_block(f"[bold {color}]{label}:[/bold {color}]", content, color, is_markdown=True)
    
    def action_open_active_config(self, config_override: AgentConfig = None, old_name_override: str = None):
        agent_to_edit = config_override or self.active_agent

        def handle_modal_result(result_tuple):
            if not result_tuple:
                return
                
            action = result_tuple[0]
            
            if action == "delete":
                agent_to_delete = result_tuple[1]
                if len(self.agent_manager.agents) <= 1:
                    self.notify("Error: You cannot delete your last remaining agent.", severity="error")
                    return
                
                self.agent_manager.delete_agent(agent_to_delete.name)
                self.agent_executors.pop(agent_to_delete.name, None)
                
                try:
                    import keyring
                    keyring.delete_password("Federate", f"agent_key_{agent_to_delete.name.lower().replace(' ', '_')}")
                    keyring.delete_password("Federate", f"agent_backup_key_{agent_to_delete.name.lower().replace(' ', '_')}")
                except Exception:
                    pass
                
                next_agent_name = list(self.agent_manager.agents.keys())[0]
                self.select_agent(next_agent_name)
                
                self.agent_manager.set_default_agent_name(next_agent_name)
                
                self.log_to_ui(f"[bold red]Agent '{agent_to_delete.name}' deleted.[/bold red] Switched to '{next_agent_name}'.")
                self.update_status_bar()
                
                new_renderable = get_welcome_banner(self, specific_agent=next_agent_name, return_renderable=True)
                banners = self.query(".welcome_banner_box")
                if banners:
                    for banner in banners:
                        banner.update(new_renderable)
                return

        self.app.push_screen(ConfigModal(agent_to_edit, self.agent_manager), handle_modal_result)

    @work(thread=True)
    def process_agent_save(self, new_config: AgentConfig, is_new: bool, old_name: str, modal=None, is_onboarding: bool = False, onboarding_data: dict = None):
        translated, error_msg = agent_core.translate_backstory(new_config)

        if error_msg:
            def reset_modal():
                self.notify(f"Failed to verify agent '{new_config.name}': {error_msg}", severity="error")
                self.log_to_ui(f"[bold red]Agent save failed for '{new_config.name}':[/bold red] {error_msg}")
                if modal:
                    try:
                        for btn in modal.query("#actions_container Button"):
                            btn.disabled = False
                        modal.query_one("#ai_modal_status", Label).update(f"[bold red] {escape(error_msg)}[/bold red]")
                    except Exception:
                        pass
                elif is_onboarding:
                    self.show_onboarding_modal(onboarding_data)

            self.app.call_from_thread(reset_modal)
            return

        try:
            cache_path = toolbox.get_storage_path("agents", "translated_backstories.json")
            cache = {}
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cache = json.load(f)
                except Exception:
                    pass
            cache[new_config.name] = {
                "original": new_config.backstory,
                "translated": translated
            }
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=4)
        except Exception:
            pass

        def finalize_ui():
            self.agent_manager.save_agent(new_config)
            if not is_new and new_config.name != old_name and old_name in self.agent_manager.agents:
                self.agent_manager.delete_agent(old_name)
                self.agent_executors.pop(old_name, None)

            if is_onboarding:
                self.agent_manager.set_default_agent_name(new_config.name)

            self.select_agent(new_config.name)
            self.agent_executors.pop(new_config.name, None)

            status_msg = "configured and activated" if is_onboarding else ("created and activated" if is_new else "updated")
            self.log_to_ui(f"[bold green]Agent '{new_config.name}' {status_msg}.[/bold green]")
            self.update_status_bar()

            new_renderable = get_welcome_banner(self, specific_agent=new_config.name, return_renderable=True)
            banners = self.query(".welcome_banner_box")
            if banners:
                for banner in banners:
                    banner.update(new_renderable)

            if modal:
                try:
                    modal.dismiss()
                except Exception:
                    pass

        self.app.call_from_thread(finalize_ui)
    
    def consolidate_memories(self, manual: bool = False):
        if not manual:
            return 

        if not self.active_agent or not self.active_agent.get_api_key():
            self.log_to_ui("[bold red]Cannot consolidate memories: API Key is missing for active agent.[/bold red]")
            return

        if getattr(self, "_running_agents", None) or getattr(self, "_running_task_count", 0) > 0:
            self.log_to_ui("[bold red]Consolidation Refused: An agent task is currently running. Please wait for it to finish or press Ctrl+A.[/bold red]")
            return

        active_sessions = getattr(self.session_manager, "active_sessions", {})
        if len(active_sessions) > 1:
            agent_names = ", ".join([f"'{name}'" for name in active_sessions.keys()])
            self.log_to_ui(
                f"[bold red]Consolidation Refused:[/bold red] Multiple agents ({agent_names}) are active in this room session.\n"
                f"[dim]To prevent cross-agent intercom contamination, please press [bold]Ctrl+K[/bold] to start a fresh single-agent session before running /consolidate.[/dim]"
            )
            return

        agent = self.active_agent
        all_agents_list = list(self.agent_manager.agents.values())

        prompt = (
            "Please review your core memorylets (only MEMORY the section). \n"
            "1) Consolidate and combine related memorylets in the MEMORY section into single summarized entries. \n"
            "2) Prune/delete any out-of-date or obsolete or expired memorylets using the `update_core_memory` tool with section='MEMORY'. \n"
            "3) For example factual data that is now no longer valid should be pruned completely. \n"
            "4) Ensure no important facts are lost while removing redundancy and outdated information. \n"
            "5) Ensure that unrelated ideas are separate memorylets. If you find any memorylets clumping unrelated facts together, split them into separate memorylets IMMEDIATELY. When doing so, save the later fact of the clumped memorylet as new memorylet and edit the original to retain only the earlier fact. \n"
            "6) In case of conflicting or contradictory memories, keep the later version (greater ID) and prune/delete the older version.\n"
            "STRICT CONSTRAINTS:\n"
            "- DO NOT mention or invoke other agents (no @mentions).\n"
            "- DO NOT use edit_file or save_file on JSON files directly. Use update_core_memory ONLY.\n"
            "- When finished, provide a concise summary of changes made."
        )

        self.current_batch_id += 1
        processed_prompt = f"[Memory Consolidation Task]\n{prompt}"

        self.log_to_ui(f"[bold cyan]Starting memory consolidation for {agent.name}...[/bold cyan]")
        self.session_manager.init_agent_session(agent, all_agents_list)
        self.session_manager.broadcast_message(agent.name, processed_prompt, is_ai=False)
        self.run_agent_task(agent, processed_prompt, batch_id=self.current_batch_id)

    def action_open_global_settings(self):
        def handle_global_config(result):
            if result == "update":
                self.log_to_ui("[bold green] Global settings successfully updated.[/bold green]")
                self.update_status_bar()
                
        self.app.push_screen(GlobalSettingsModal(), handle_global_config)
    
    def action_open_config(self):
        def handle_chat_mgr(action):
            if action == "new_session":
                self.action_clear_all_contexts()
            elif action == "load_chat":
                def handle_load(filepath):
                    if filepath:
                        self.log_to_ui(f"Loading {filepath}...") 
                self.app.push_screen(ChatLoadModal(), handle_load)

        self.app.push_screen(ChatManagerModal(), handle_chat_mgr)

    def write_message_block(self, header_markup: str, content: str, color: str, is_markdown: bool = True):
        try:
            self.app.call_from_thread(self._write_message_block, header_markup, content, color, is_markdown)
        except RuntimeError:
            self._write_message_block(header_markup, content, color, is_markdown)

    def _write_message_block(self, header_markup: str, content: str, color: str, is_markdown: bool = True):
        try:
            chat_body = self.query_one("#chat_messages")
            top_rule = Static(Rule(style=color))
            header_widget = Static(Text.from_markup(header_markup), classes="chat_msg", markup=False)
            
            if is_markdown:
                msg_to_render = render_latex_to_unicode(content)
                content_widget = Static(Markdown(msg_to_render), classes="chat_msg")
            else:
                content_widget = Static(Text.from_markup(content), classes="chat_msg", markup=False)
                
            bottom_rule = Static(Rule(style=color))
            
            box = Vertical(
                top_rule,
                header_widget,
                content_widget,
                bottom_rule,
                classes="message_block"
            )
            chat_body.mount(box)
            self.app.call_after_refresh(lambda: self.query_one("#ai_chat_scroll").scroll_end(animate=False))
            self.query_one("#ai_chat_scroll").scroll_end(animate=False)
            self.update_status_bar()
        except Exception:
            pass

    def render_tool_result_box(self, owner_name, color, summary):
        try:
            box_content = f"[bold]Tool Result ({owner_name}):[/bold]\n{escape(summary)}"
            box_widget = Static(Text.from_markup(box_content), classes="tool_result_box", markup=False)
            box_widget.styles.border = ("round", color)
            self._write_log(box_widget)
        except Exception:
            pass
            
    def render_tool_error_box(self, owner_name, color, summary):
        try:
            box_content = f"[bold]Tool Result ({owner_name}):[/bold]\n{summary}"
            box_widget = Static(Text.from_markup(box_content), classes="tool_result_box", markup=False)
            box_widget.styles.border = ("round", color)
            self._write_log(box_widget)
        except Exception:
            pass

    def mount_ai_message_box(self, agent_name, agent_color):
        try:
            top_rule = Static(Rule(style=agent_color))
            header_widget = Static(Text.from_markup(f"[bold {agent_color}]{agent_name}:[/bold {agent_color}]"), classes="chat_msg", markup=False)
            self.current_ai_widget = Static(Markdown(""), classes="chat_msg")
            bottom_rule = Static(Rule(style=agent_color))
            
            ai_box = Vertical(
                top_rule,
                header_widget,
                self.current_ai_widget,
                bottom_rule,
                classes="message_block"
            )
            self.query_one("#chat_messages").mount(ai_box)
        except Exception:
            pass

    def update_ai_message(self, display_text):
        try:
            if hasattr(self, "current_ai_widget") and self.current_ai_widget:
                self.current_ai_widget.update(Markdown(display_text))
        except Exception:
            pass

    def render_latex_to_unicode_ext(self, text):
        return render_latex_to_unicode(text)

    def log_to_ui(self, msg: Any, is_markdown: bool = False):
        try:
            self.app.call_from_thread(self._write_log, msg, is_markdown)
        except RuntimeError:
            self._write_log(msg, is_markdown)
    
    def _write_log(self, msg: Any, is_markdown: bool = False):
        try:
            chat_body = self.query_one("#chat_messages")
            
            if isinstance(msg, str) and not is_markdown:
                match = re.search(r'\[bold ([^\]]+)\]([^:]+):\[/bold(?:\s+[^\]]+)?\]', msg)
                if match:
                    label_text = match.group(2).strip()
                    agent = self.agent_manager.get_agent(label_text)
                    if agent:
                        old_color = match.group(1)
                        msg = msg.replace(f"[bold {old_color}]", f"[bold {agent.color}]")
                        msg = msg.replace(f"[/bold {old_color}]", f"[/bold {agent.color}]")

            if isinstance(msg, str):
                if is_markdown:
                    msg_to_render = render_latex_to_unicode(msg)
                    widget = Static(Markdown(msg_to_render), classes="chat_msg")
                else:
                    try:
                        parsed_text = Text.from_markup(msg)
                        
                        if not hasattr(self, "_dummy_console"):
                            from rich.console import Console
                            self._dummy_console = Console()
                            
                        for span in parsed_text.spans:
                            if isinstance(span.style, str):
                                self._dummy_console.get_style(span.style)
                                
                        widget = Static(parsed_text, classes="chat_msg", markup=False)
                    except Exception:
                        widget = Static(Text(msg), classes="chat_msg", markup=False)
            elif isinstance(msg, (Rule, Text, Markdown)):
                widget = Static(msg, classes="chat_msg", markup=False)
            else:
                widget = msg
            chat_body.mount(widget)
            self.app.call_after_refresh(lambda: self.query_one("#ai_chat_scroll").scroll_end(animate=False))
            self.query_one("#ai_chat_scroll").scroll_end(animate=False)
            self.update_status_bar()
        except Exception: pass

    def update_status_bar(self):
        try:
            mode = getattr(self, "agent_mode", "PLAN")
            if mode == "PLAN":
                mode_str = "[bold green]SAFE[/bold green]"
            elif mode == "INTERMEDIATE":
                mode_str = "[bold tomato]SEMI-AUTO[/bold tomato]"
            else:
                mode_str = "[bold red]FULL-AUTO[/bold red]"

            user_cfg = toolbox.load_global_settings()
            raw_model_color = user_cfg.get("model_color", "#ffd700") or "#ffd700"

            # Safe validation fallback for model color
            from rich.style import Style
            try:
                Style.parse(raw_model_color)
                model_color = raw_model_color
            except Exception:
                model_color = "#ffd700"

            # Collect all active agents participating in the current session
            active_names = list(self.session_manager.active_sessions.keys()) if hasattr(self, "session_manager") else []
            if not active_names and hasattr(self, "active_agent") and self.active_agent:
                active_names = [self.active_agent.name]
            elif hasattr(self, "active_agent") and self.active_agent and self.active_agent.name not in active_names:
                active_names.insert(0, self.active_agent.name)

            agent_lines = []
            for a_name in active_names:
                a_cfg = self.agent_manager.get_agent(a_name)
                if a_cfg:
                    raw_color = a_cfg.color or "#00FFFF"
                    try:
                        Style.parse(raw_color)
                        color = raw_color
                    except Exception:
                        color = "#00FFFF"

                    model = (a_cfg.backup_model if (a_cfg.use_backup and a_cfg.backup_model) else a_cfg.model) or "unknown"
                    agent_lines.append(
                        f"[bold {color}]{a_cfg.name}[/bold {color}] "
                        f"[{color}]([/][{model_color}]{model}[/{model_color}][{color}])[/]"
                    )
                else:
                    agent_lines.append(f"[bold cyan]{a_name}[/bold cyan]")

            agent_info = "\n".join(agent_lines)
            
            try:
                app = self.app
                base_dir = str(app.query_one("#dir_tree").path) if app else os.getcwd()
            except Exception:
                base_dir = os.getcwd()
            self.query_one("#ai_cwd_label", Label).update(Text(base_dir))
            self.query_one("#ai_config_label", Label).update(f"[F3] {mode_str}")
            self.query_one("#ai_token_label", Label).update(agent_info)
        except Exception: pass
        self.update_prompt_label()

    def update_prompt_label(self):
        try:
            label = self.query_one("#prompt_label", Label)
            if self.shell_mode:
                folder = os.path.basename(os.getcwd()) or os.getcwd()
                label.update(Text.from_markup(f"[bold red]shell@{escape(folder)} %[/bold red]"))
            else:
                label.update(f"[bold {self.active_agent.color}]{self.active_agent.name}>[/bold {self.active_agent.color}]")
        except Exception: pass

    def action_cycle_arm_mode(self):
        ARM_MODES = ["PLAN", "INTERMEDIATE", "EXECUTE"]
        current_idx = ARM_MODES.index(self.agent_mode) if self.agent_mode in ARM_MODES else 0
        next_idx = (current_idx + 1) % len(ARM_MODES)
        self.agent_mode = ARM_MODES[next_idx]
        self.agent_executors = {}
        self.log_to_ui(f"System operating mode cycled to: {self.agent_mode}")
        self.update_status_bar()

    def action_cycle_agents(self):
        names = list(self.agent_manager.agents.keys())
        if not names:
            return
            
        try:
            current_idx = names.index(self.active_agent.name)
        except ValueError:
            current_idx = 0
            
        next_idx = (current_idx + 1) % len(names)
        next_name = names[next_idx]
        
        if self.select_agent(next_name):
            self.log_to_ui(f"[bold green]Cycled active agent to: {next_name}[/bold green]")
            new_renderable = get_welcome_banner(self, specific_agent=next_name, return_renderable=True)
            banners = self.query(".welcome_banner_box")
            if banners:
                for banner in banners:
                    banner.update(new_renderable)
            else:
                from textual.widgets import Static
                self._write_log(Static(new_renderable, classes="welcome_banner_box"))

    def toggle_plan_mode(self):
        self.action_cycle_arm_mode()

    def clear_chat_ui(self):
        try:
            self.query_one("#chat_messages").query("*").remove()
            self.query_one("#progress_left").query("*").remove()
            self.query_one("#progress_right", RichLog).clear()
            self.query_one("#progress_container").styles.display = "none"
        except Exception: pass

    def get_executor(self, agent_config: AgentConfig):
        return agent_core.get_executor_core(self, agent_config)
    
    @work(thread=True)
    def force_update_all_backstories(self):
        agent_core.force_update_all_backstories_core(self)

    def translate_team_backstories(self, host_agent: AgentConfig, all_agents: List[AgentConfig]):
        agent_core.translate_team_backstories_core(self, host_agent, all_agents)
    
    @on(ChatInput.AbortRequest)
    def on_abort_request(self, event: ChatInput.AbortRequest):
        self.action_abort()

    @on(Input.Submitted, "#ai_chat_input")
    @on(ChatInput.Submitted)
    def on_input_submitted(self, event: ChatInput.Submitted):
        prompt = event.value
        if not prompt.strip(): return
        event.input.text = "" 
        
        if prompt.strip() == "!":
            self.shell_mode = not self.shell_mode
            self.update_prompt_label()
            return

        if self.shell_mode or prompt.startswith("!"):
            cmd = prompt[1:].strip() if prompt.startswith("!") else prompt.strip()
            self._write_log(Rule(style="dim"))
            self._write_log(f"[bold red]Shell:[/bold red] {escape(cmd)}")
            out = process_shell_command(cmd, self)
            self._write_log(escape(out))
            return

        if prompt.startswith("/"):
            self._write_log(Rule(style="dim"))
            process_slash_command(prompt, self)
            return
        
        acting_agent = self.active_agent
        clean_prompt = prompt
        is_team = False
        
        is_interrupt = False
        if self._running_agents:
            self.action_abort()
            is_interrupt = True
        
        self.current_batch_id += 1
        batch_id = self.current_batch_id

        if prompt.strip().lower() != "@resume" and hasattr(self, "paused_queue") and self.paused_queue:
            self.paused_queue = []

        if prompt.strip().lower() == "@resume":
            if hasattr(self, "paused_queue") and self.paused_queue:
                self.log_to_ui("[bold green]Resuming paused agent queue...[/bold green]")
                with self.turn_lock:
                    self.turn_queue = self.paused_queue
                    self.paused_queue = []
                    first_resumed = self.turn_queue.pop(0)
                acting_agents = [first_resumed]
                self.session_manager.join_conversation(self.active_agent.name, first_resumed, list(self.agent_manager.agents.values()))
                clean_prompt = "[Queue Resumed by User]"
            else:
                self.log_to_ui("[bold red]No paused queue to resume.[/bold red]")
                return
        elif prompt.strip().lower().startswith("@team") or prompt.strip().lower().startswith("@room"):
            is_team = prompt.strip().lower().startswith("@team")
            is_room = not is_team
            prefix_len = 5 if is_team else 5
            clean_prompt = prompt.strip()[prefix_len:].strip()
            
            if is_interrupt:
                clean_prompt = f"the User interrupted to say this: {clean_prompt}"

            if not clean_prompt:
                self.log_to_ui(f"[bold red]Usage: @{'team' if is_team else 'room'} <message>[/bold red]")
                return
            
            if is_team:
                all_agents_list = list(self.agent_manager.agents.values())
                for agent in all_agents_list:
                    self.session_manager.init_agent_session(agent, all_agents_list)
                    self.session_manager.join_conversation(self.active_agent.name, agent, all_agents_list)
                acting_agents = list(self.agent_manager.agents.values())
            else:
                active_names = list(self.session_manager.active_sessions.keys())
                acting_agents = []
                all_agents_list = list(self.agent_manager.agents.values())
                for name in active_names:
                    agent = self.agent_manager.get_agent(name)
                    if agent:
                        self.session_manager.join_conversation(self.active_agent.name, agent, all_agents_list)
                        acting_agents.append(agent)
            
            with self.turn_lock:
                self.turn_queue = []

        else:
            seq_mentions = self.agent_manager.get_mentions(prompt)
            par_mentions = self.agent_manager.get_parallel_mentions(prompt)
            clean_prompt = prompt
            if is_interrupt:
                clean_prompt = f"the User interrupted to say this: {clean_prompt}"

            acting_agents = []

            if seq_mentions:
                target_agents = []
                for name in seq_mentions:
                    agent = self.agent_manager.get_agent(name)
                    if agent:
                        target_agents.append(agent)
                
                if target_agents:
                    if par_mentions:
                        with self.turn_lock:
                            self.turn_queue = target_agents
                    else:
                        first_agent = target_agents[0]
                        with self.turn_lock:
                            self.turn_queue = target_agents[1:]
                        acting_agents.append(first_agent)
                        self.session_manager.join_conversation(self.active_agent.name, first_agent, list(self.agent_manager.agents.values()))
                elif not par_mentions:
                    acting_agents.append(self.active_agent)
                    self.session_manager.init_agent_session(self.active_agent, list(self.agent_manager.agents.values()))
                    with self.turn_lock:
                        self.turn_queue = []
            elif not par_mentions:
                acting_agents.append(self.active_agent)
                self.session_manager.init_agent_session(self.active_agent, list(self.agent_manager.agents.values()))
                with self.turn_lock:
                    self.turn_queue = []

            for name in par_mentions:
                p_agent = self.agent_manager.get_agent(name)
                if p_agent and p_agent not in acting_agents:
                    acting_agents.append(p_agent)
                    self.session_manager.join_conversation(self.active_agent.name, p_agent, list(self.agent_manager.agents.values()))

        user_cfg = toolbox.load_global_settings()
        u_name = user_cfg.get("user_name", "User")
        u_color = user_cfg.get("user_color", "#dda0dd")

        if is_team:
            hdr = f"[bold {u_color}]{u_name} (to Team):[/bold {u_color}]"
        elif 'is_room' in locals() and is_room:
            hdr = f"[bold {u_color}]{u_name} (to Room):[/bold {u_color}]"
        elif len(acting_agents) == 1:
            hdr = f"[bold {u_color}]{u_name} (to {acting_agents[0].name}):[/bold {u_color}]"
        else:
            hdr = f"[bold {u_color}]{u_name}:[/bold {u_color}]"

        self._write_message_block(hdr, clean_prompt, u_color, is_markdown=True)
        
        time_stamp = f"[Time: {datetime.now().strftime('%H:%M')}]\n"
        processed_prompt = time_stamp + handle_ampersand_commands(clean_prompt, self)
        
        self.session_manager.broadcast_message(u_name, processed_prompt, is_ai=False)
        self.update_tokens()
        toolbox.ABORT_EVENT.clear()
        
        for agent in acting_agents:
            self.run_agent_task(agent, processed_prompt, batch_id=batch_id)

    @work(thread=True)
    def run_agent_task(self, agent: AgentConfig, prompt: str, override_thread_id: str = None, batch_id: int = 0):
        agent_core.run_agent_task_core(self, agent, prompt, override_thread_id, batch_id)

    def _toggle_spinner(self, show: bool, agent_name: str = "Agent", agent_color: str = "#00FFFF"):
        if show:
            self._running_task_count += 1
        else:
            self._running_task_count = max(0, self._running_task_count - 1)
            
        try:
            self.query_one("#ai_thinking_spinner").display = (self._running_task_count > 0)
        except Exception:
            pass

    def tick_spinners(self):
        try:
            spinner_label = self.query_one("#ai_thinking_spinner")
            spinner_visible = spinner_label.display
        except Exception:
            spinner_visible = False

        try:
            container = self.query_one("#progress_container")
            progress_visible = (container.styles.display != "none")
        except Exception:
            progress_visible = False

        if not spinner_visible and not progress_visible:
            return

        try:
            self.spinner_idx = (self.spinner_idx + 1) % len(self.spinner_chars)
            char = self.spinner_chars[self.spinner_idx]

            if progress_visible:
                try:
                    for row in self.query(".task_row"):
                        try:
                            prog = row.query_one(ProgressBar)
                            spin_label = row.query_one(".task_spinner")
                            if prog.progress < prog.total:
                                spin_label.update(char)
                            else:
                                spin_label.update("")
                        except Exception: continue
                except Exception: pass
            
            if spinner_visible:
                try:
                    if self._running_agents:
                        agents_list = sorted(list(self._running_agents))
                        if len(agents_list) == 1:
                            a_name = agents_list[0]
                            a_cfg = self.agent_manager.get_agent(a_name)
                            a_color = a_cfg.color if a_cfg else "white"
                            spinner_label.update(Text.from_markup(f"[bold {a_color}]{char} {a_name} is working...[/]"))
                        elif 1 < len(agents_list) <= 5:
                            parts = []
                            for i, name in enumerate(agents_list):
                                cfg = self.agent_manager.get_agent(name)
                                color = cfg.color if cfg else "white"
                                parts.append(f"[bold {color}]{name}[/]")
                            
                            if len(parts) == 2:
                                names_str = f"{parts[0]} and {parts[1]}"
                            else:
                                names_str = ", ".join(parts[:-1]) + f", and {parts[-1]}"
                            spinner_label.update(Text.from_markup(f"{char} {names_str} are working..."))
                        else:
                            spinner_label.update(Text.from_markup(f"{char} [bold #dda0dd]{len(agents_list)} agents[/] are working..."))
                    else:
                        spinner_label.update(f"{char} Agent is working...")
                except Exception: pass

        except Exception:
            pass
    
    def _get_last_scheduled(self, task, now):
        from datetime import datetime, timedelta
        import calendar
        try:
            task_date = datetime.strptime(getattr(task, "date_str", ""), "%Y-%m-%d") if getattr(task, "date_str", "") else now
            th, tm = map(int, task.time_str.split(":"))
            anchor = datetime(task_date.year, task_date.month, task_date.day, th, tm)
        except Exception:
            return None

        if anchor > now:
            return None

        repeat_mode = getattr(task, "repeat", "daily")
        candidate = anchor
        while True:
            if repeat_mode == "daily":
                nxt = candidate + timedelta(days=1)
            elif repeat_mode == "weekly":
                nxt = candidate + timedelta(weeks=1)
            elif repeat_mode == "monthly":
                month = candidate.month
                year = candidate.year + (month // 12)
                month = (month % 12) + 1
                max_day = calendar.monthrange(year, month)[1]
                nxt = datetime(year, month, min(task_date.day, max_day), th, tm)
            elif repeat_mode == "annually":
                nxt = datetime(candidate.year + 1, task_date.month, task_date.day, th, tm)
            else:
                nxt = candidate + timedelta(days=1)

            if nxt > now:
                break
            candidate = nxt
        return candidate

    def tick_scheduler(self):
        if getattr(self, "shell_mode", False) or not hasattr(self, "schedule_manager") or getattr(self, "_scheduler_prompt_active", False):
            return
            
        now = datetime.now()
        
        for task in self.schedule_manager.tasks:
            if not task.is_active: continue
            if getattr(task, "snooze_until", 0.0) > time.time(): continue
            
            run_today = False
            last_scheduled = self._get_last_scheduled(task, now)
            if last_scheduled:
                sched_str = last_scheduled.strftime("%Y-%m-%d %H:%M")
                if getattr(task, "last_run_date", "") != sched_str:
                    run_today = True

            if run_today:
                if self._running_agents:
                    continue 

                self._scheduler_prompt_active = True

                def handle_action(action: str):
                    self._scheduler_prompt_active = False
                    if action == "defer":
                        task.snooze_until = time.time() + 900.0
                        self.notify(f"Scheduled task '{task.prompt[:30]}...' deferred for 15 minutes.", severity="warning")
                    elif action == "skip":
                        task.last_run_date = sched_str
                        self.schedule_manager.save()
                        self.notify("Scheduled task skipped.", severity="information")
                    elif action == "run_now":
                        task.last_run_date = sched_str
                        self.schedule_manager.save()
                        self.action_clear_all_contexts()
                        self.log_to_ui(Rule(title="[bold yellow] INITIATING AUTOMATED SCHEDULED TASK", style="dim"))
                        
                        full_prompt = f"@{task.agent_name} [Automated Scheduled Task]:\n{task.prompt}"
                        
                        from textual.widgets import TextArea
                        chat_input = self.query_one("#ai_chat_input", ChatInput)
                        
                        event = ChatInput.Submitted(chat_input, full_prompt)
                        self.on_input_submitted(event)

                agent = self.agent_manager.get_agent(task.agent_name)
                a_color = agent.color if agent else "#00FFFF"
                self.app.push_screen(ScheduledTaskPromptModal(task, agent_color=a_color), handle_action)
                break
    
    def update_tokens(self):
        try:
            self.update_status_bar()
        except Exception:
            pass

    @work(thread=True)
    def check_for_updates_bg(self, manual: bool = False):
        import urllib.request, json
        
        installed_ver = get_installed_version()
        latest_ver = None
        release_notes = ""
        
        try:
            url = "https://pypi.org/pypi/federaide/json"
            req = urllib.request.Request(url, headers={'User-Agent': 'Federate-App'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                pypi_data = json.loads(resp.read().decode('utf-8'))
                latest_ver = pypi_data.get("info", {}).get("version")
        except Exception as e:
            if manual:
                self.log_to_ui(f"[bold red]Failed to check for updates:[/bold red] {e}")
            return

        if not latest_ver:
            if manual:
                self.log_to_ui("[bold red]Could not resolve latest version from PyPI.[/bold red]")
            return

        def ver_tuple(v):
            return tuple(int(x) for x in re.sub(r'[^0-9.]', '', v).split('.') if x.isdigit())

        is_newer = ver_tuple(latest_ver) > ver_tuple(installed_ver)

        if not is_newer:
            if manual:
                self.log_to_ui(f"[bold green]Federate is up-to-date![/bold green] Installed version: v{installed_ver}")
            return

        settings = toolbox.load_global_settings()
        skipped = settings.get("skipped_version", "")
        if not manual and skipped == latest_ver:
            return

        headers = {'User-Agent': 'Mozilla/5.0 (compatible; FederateApp/1.0)'}
        
        try:
            gh_list_url = "https://api.github.com/repos/ROCK-LAB-PRIVATE-LIMITED/federate.ai/releases?per_page=100"
            req_gh_list = urllib.request.Request(gh_list_url, headers=headers)
            with urllib.request.urlopen(req_gh_list, timeout=8) as resp_gh_list:
                releases_data = json.loads(resp_gh_list.read().decode('utf-8'))
                
            cumulative_notes = []
            if isinstance(releases_data, list):
                for rel in releases_data:
                    tag = rel.get("tag_name", "")
                    tag_ver = ver_tuple(tag)
                    if tag_ver > ver_tuple(installed_ver) and tag_ver <= ver_tuple(latest_ver):
                        body = rel.get("body", "").strip()
                        if body:
                            rel_title = rel.get("name") or f"Release {tag}"
                            cumulative_notes.append(f"## {rel_title}\n\n{body}")
            
            if cumulative_notes:
                release_notes = "\n\n---\n\n".join(cumulative_notes)
        except Exception:
            pass

        if not release_notes:
            for endpoint in [
                f"https://api.github.com/repos/ROCK-LAB-PRIVATE-LIMITED/federate.ai/releases/tags/v{latest_ver}",
                f"https://api.github.com/repos/ROCK-LAB-PRIVATE-LIMITED/federate.ai/releases/tags/{latest_ver}",
                "https://api.github.com/repos/ROCK-LAB-PRIVATE-LIMITED/federate.ai/releases/latest"
            ]:
                try:
                    req_gh = urllib.request.Request(endpoint, headers=headers)
                    with urllib.request.urlopen(req_gh, timeout=8) as resp_gh:
                        gh_data = json.loads(resp_gh.read().decode('utf-8'))
                        body = gh_data.get("body", "")
                        if body and body.strip():
                            release_notes = body
                            break
                except Exception:
                    continue

        if not release_notes or not release_notes.strip():
            release_notes = f"## Release v{latest_ver}\n\nA new update for FEDERaiDE (**v{latest_ver}**) is available with bug fixes and performance improvements."

        def show_modal():
            def handle_update_result(action: str):
                if action == "skip":
                    st = toolbox.load_global_settings()
                    st["skipped_version"] = latest_ver
                    toolbox.save_global_settings(st)
                    self.log_to_ui(f"[dim]Skipped update v{latest_ver}. Type /update anytime to update.[/dim]")

            self.app.push_screen(UpdateModal(installed_ver, latest_ver, release_notes), handle_update_result)

        self.app.call_from_thread(show_modal)

    def request_clarification(self, options: Optional[List[str]] = None, agent_name: str = "Agent") -> str:
        result_event = threading.Event()
        final_result = [""]

        def handle_result(res: str):
            final_result[0] = res or ""
            result_event.set()

        self.app.call_from_thread(self.app.push_screen, ClarificationModal(options, agent_name=agent_name), handle_result)
        
        while not result_event.is_set():
            if toolbox.ABORT_EVENT.is_set():
                return ""
            time.sleep(0.1)
            
        return final_result[0]

    def handle_telegram_input(self, chat_id: int, text: str):
        if text.strip().startswith("/") or text.strip().startswith("&"):
            self.telegram_manager.send_message(
                chat_id, 
                "ℹ️ Slash and Attachment commands are disabled on Telegram."
            )
            return
        def _process():
            try:
                self.current_telegram_chat_id = chat_id
                
                user_cfg = toolbox.load_global_settings()
                u_name = user_cfg.get("user_name", "User")
                u_color = user_cfg.get("user_color", "#dda0dd")

                self._write_message_block(f"[bold {u_color}]Telegram {u_name} ({chat_id}):[/bold {u_color}]", text, u_color, is_markdown=True)
                
                is_interrupt = False
                if self._running_agents:
                    self.action_abort()
                    is_interrupt = True
                
                self.current_batch_id += 1
                batch_id = self.current_batch_id
                
                if text.strip().lower() != "@resume" and hasattr(self, "paused_queue") and self.paused_queue:
                    self.paused_queue = []

                acting_agent = self.active_agent
                clean_prompt = text
                acting_agents = []
                is_team = False
                is_room = False

                if text.strip().lower() == "@resume":
                    if hasattr(self, "paused_queue") and self.paused_queue:
                        self.log_to_ui("[bold green]Resuming paused agent queue...[/bold green]")
                        with self.turn_lock:
                            self.turn_queue = self.paused_queue
                            self.paused_queue = []
                            first_resumed = self.turn_queue.pop(0)
                        acting_agents = [first_resumed]
                        self.session_manager.join_conversation(self.active_agent.name, first_resumed, list(self.agent_manager.agents.values()))
                        clean_prompt = "[Queue Resumed by User]"
                    else:
                        self.log_to_ui("[bold red]No paused queue to resume.[/bold red]")
                        return
                elif text.strip().lower().startswith("@team") or text.strip().lower().startswith("@room"):
                    is_team = text.strip().lower().startswith("@team")
                    is_room = not is_team
                    prefix_len = 5
                    clean_prompt = text.strip()[prefix_len:].strip()
                    
                    if is_interrupt:
                        clean_prompt = f"the User interrupted to say this: {clean_prompt}"
                    
                    if is_team:
                        all_agents_list = list(self.agent_manager.agents.values())
                        for agent in all_agents_list:
                            self.session_manager.init_agent_session(agent, all_agents_list)
                            self.session_manager.join_conversation(self.active_agent.name, agent, all_agents_list)
                        acting_agents = all_agents_list
                    else:
                        active_names = list(self.session_manager.active_sessions.keys())
                        all_agents_list = list(self.agent_manager.agents.values())
                        for name in active_names:
                            agent = self.agent_manager.get_agent(name)
                            if agent:
                                self.session_manager.join_conversation(self.active_agent.name, agent, all_agents_list)
                                acting_agents.append(agent)
                else:
                    seq_mentions = self.agent_manager.get_mentions(text)
                    par_mentions = self.agent_manager.get_parallel_mentions(text)
                    clean_prompt = text
                    if is_interrupt:
                        clean_prompt = f"the User interrupted to say this: {clean_prompt}"

                    acting_agents = []

                    if seq_mentions:
                        target_agents = []
                        for name in seq_mentions:
                            agent = self.agent_manager.get_agent(name)
                            if agent:
                                target_agents.append(agent)
                        
                        if target_agents:
                            if par_mentions:
                                with self.turn_lock:
                                    self.turn_queue = target_agents
                            else:
                                first_agent = target_agents[0]
                                with self.turn_lock:
                                    self.turn_queue = target_agents[1:]
                                acting_agents.append(first_agent)
                                self.session_manager.join_conversation(self.active_agent.name, first_agent, list(self.agent_manager.agents.values()))
                        elif not par_mentions:
                            acting_agents.append(self.active_agent)
                            self.session_manager.init_agent_session(self.active_agent, list(self.agent_manager.agents.values()))
                            with self.turn_lock:
                                self.turn_queue = []
                    elif not par_mentions:
                        acting_agents.append(self.active_agent)
                        self.session_manager.init_agent_session(self.active_agent, list(self.agent_manager.agents.values()))
                        with self.turn_lock:
                            self.turn_queue = []

                    for name in par_mentions:
                        p_agent = self.agent_manager.get_agent(name)
                        if p_agent and p_agent not in acting_agents:
                            acting_agents.append(p_agent)
                            self.session_manager.join_conversation(self.active_agent.name, p_agent, list(self.agent_manager.agents.values()))

                if not clean_prompt and (is_team or is_room):
                    return

                time_stamp = f"[Today's date is {datetime.now().strftime('%A, %B %d, %Y')} and the time now is {datetime.now().strftime('%H:%M')}]\n"
                processed_prompt = time_stamp + clean_prompt
                
                self.session_manager.broadcast_message(f"Telegram {u_name} ({chat_id})", processed_prompt, is_ai=False)
                self.update_tokens()
                self.telegram_manager.start_chat_action(chat_id, "typing")
                
                toolbox.ABORT_EVENT.clear()
                for agent in acting_agents:
                    self.run_agent_task(agent, processed_prompt, batch_id=batch_id)

            except Exception as e:
                self.log_to_ui(f"[bold red]Telegram Input Error:[/bold red] {e}")
                
        self.app.call_from_thread(_process)

    def handle_stt_input(self, text: str, action: str = "append"):
        def _process():
            try:
                chat_input = self.query_one("#ai_chat_input", ChatInput)
                current_val = chat_input.text.strip() 

                if action == "append":
                    if text:
                        new_val = (current_val + " " + text).strip()
                        chat_input.text = new_val 
                        self.stt_append_history.append(text)
                
                elif action == "delete":
                    if self.stt_append_history:
                        last_text = self.stt_append_history.pop()
                        if chat_input.text.endswith(last_text): 
                            chat_input.text = chat_input.text[:-len(last_text)].strip() 
                        else:
                            chat_input.text = chat_input.text.replace(last_text, "").strip() 

                elif action == "submit":
                    if text:
                        chat_input.text = (current_val + " " + text).strip() 
                    
                    self.stt_append_history = [] 
                    
                    if chat_input.text.strip(): 
                        self.on_input_submitted(ChatInput.Submitted(chat_input, chat_input.text)) 

                chat_input.focus()
                
                lines = chat_input.text.split("\n")
                chat_input.cursor_location = (len(lines)-1, len(lines[-1])) 
                
            except Exception as e:
                self.log_to_ui(f"[bold red]STT UI Binding Error:[/bold red] {e}")
                
        self.app.call_from_thread(_process)

    def mount_progress(self, tasks: list[str]):
        def _mount():
            try:
                self._write_log(Rule(title="[bold]Deep Research Modules Dispatched[/]", style="dim"))

                container = self.query_one("#progress_container")
                container.styles.display = "block"

                left_pane = self.query_one("#progress_left")
                right_log = self.query_one("#progress_right", RichLog)

                left_pane.query("*").remove()
                right_log.clear()

                from textual.containers import Horizontal
                for t in tasks:
                    safe_id = "task_" + "".join(c if c.isalnum() else "_" for c in t)

                    bar = ProgressBar(total=100, show_eta=False, id=f"prog_{safe_id}")

                    row = Horizontal(
                        Label(self.spinner_chars[0], id=f"spin_{safe_id}", classes="task_spinner"),
                        bar,
                        classes="task_row", id=f"row_{safe_id}"
                    )
                    left_pane.mount(row)

                self.app.call_after_refresh(
                    lambda: self.query_one("#ai_chat_scroll").scroll_end(animate=False)
                )
            except Exception as e:
                self.log_to_ui(f"[bold red]UI Error:[/bold red] {e}")

        self.app.call_from_thread(_mount)

    def update_progress(self, task: str, percent: float, msg: str):
        safe_id = "task_" + "".join(c if c.isalnum() else "_" for c in task)
        def _update():
            try:
                log_pane = self.query_one("#progress_right", RichLog)
                log_pane.write(msg)

                prog = self.query_one(f"#prog_{safe_id}", ProgressBar)
                spin = self.query_one(f"#spin_{safe_id}", Label)

                if percent is not None:
                    prog.update(progress=percent)
                    if percent >= 100:
                        spin.update("") 

                self.app.call_after_refresh(
                    lambda: self.query_one("#ai_chat_scroll").scroll_end(animate=False)
                )
            except Exception: pass

        self.app.call_from_thread(_update)

    def hide_progress(self):
        def _hide():
            try:
                self.query_one("#progress_container").styles.display = "none"
            except Exception:
                pass
        self.app.call_from_thread(_hide)
    
    def check_onboarding(self):
        settings_path = os.path.join(self.agent_manager.agents_dir, "settings.json")
        is_pristine = not os.path.exists(settings_path)
        if is_pristine or (len(self.agent_manager.agents) == 1 and not self.active_agent.get_api_key()):
            self.call_after_refresh(self.show_onboarding_modal)

    def show_onboarding_modal(self, initial_data: dict = None):
        def handle_onboarding(result):
            if result:
                old_name = self.active_agent.name
                
                new_config = AgentConfig(
                    name=result["name"],
                    backstory=result["backstory"],
                    model=result["model"],
                    base_url=result["base_url"],
                    color="#00FFFF",
                    enabled_tools=["read_file", "fetch_url", "save_file", "edit_file", "dispatch_coding_subagent", "run_terminal_command"],
                    reasoning_effort="none",
                    temperature=1.0
                )
                
                self._update_agent_keys(new_config.name, result["api_key"])
                self.process_agent_save(new_config, is_new=False, old_name=old_name, is_onboarding=True, onboarding_data=result)

        self.app.push_screen(OnboardingModal(initial_data), handle_onboarding)

    def _update_agent_keys(self, agent_name: str, primary_key: str):
        import keyring
        primary_user = f"agent_key_{agent_name.lower().replace(' ', '_')}"
        try:
            if primary_key:
                keyring.set_password("Federate", primary_user, primary_key)
                os.environ[f"AGENT_KEY_{agent_name.upper().replace(' ', '_')}"] = primary_key
        except Exception as e:
            self.notify(f"Keychain Access Failed: Could not save credentials to OS Keyring.\nDetail: {e}", severity="error")