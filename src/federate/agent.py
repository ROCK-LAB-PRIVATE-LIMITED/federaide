import os
import sys
import glob
import threading
import subprocess
import requests
import fnmatch
import re
from markdownify import markdownify as md

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.binding import Binding
from textual.widgets import RichLog, Input, Label, Button, Select, Static, ProgressBar, Checkbox, ListView, ListItem, TextArea, Switch
from textual.screen import ModalScreen
from textual import work, on
from textual.message import Message
from textual import events

from commands import ChatSuggester, process_shell_command, process_slash_command, handle_ampersand_commands

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, AIMessageChunk, ToolMessage, messages_to_dict, messages_from_dict
# --- GEMINI THOUGHT SIGNATURE MONKEY-PATCH FOR OPENAI COMPATIBILITY ---
try:
    import langchain_openai.chat_models.base as langchain_openai_base
    
    _orig_convert_message_to_dict = langchain_openai_base._convert_message_to_dict

    def _patched_convert_message_to_dict(message, *args, **kwargs):
        msg_dict = _orig_convert_message_to_dict(message, *args, **kwargs)
        
        # 1. Sanitize Tool Response Messages (function_response name cannot be empty)
        if msg_dict.get("role") == "tool" and not msg_dict.get("name"):
            msg_dict["name"] = "unknown_tool"
            
        # 2. Sanitize Assistant Tool Call Messages (function_call name cannot be empty)
        elif msg_dict.get("role") == "assistant" and msg_dict.get("tool_calls"):
            for tc in msg_dict["tool_calls"]:
                func = tc.get("function")
                if func and not func.get("name"):
                    func["name"] = "unknown_tool"

        if isinstance(message, AIMessage) or msg_dict.get("role") == "assistant":
            tool_calls = msg_dict.get("tool_calls")
            if tool_calls:
                sig_map = {}
                raw_tool_calls = message.additional_kwargs.get("tool_calls", [])
                for rtc in raw_tool_calls:
                    rtc_id = rtc.get("id")
                    extra = rtc.get("extra_content") or {}
                    google = extra.get("google") or {}
                    sig = google.get("thought_signature")
                    if sig and rtc_id:
                        sig_map[rtc_id] = sig
                
                for tc in tool_calls:
                    tc_id = tc.get("id")
                    sig = sig_map.get(tc_id) or "skip_thought_signature_validator"
                    tc["extra_content"] = {
                        "google": {
                            "thought_signature": sig
                        }
                    }
        return msg_dict

    langchain_openai_base._convert_message_to_dict = _patched_convert_message_to_dict
except Exception:
    pass
# ----------------------------------------------------------------------------
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from rich.markup import escape
from rich.markdown import Markdown
from rich.text import Text
from rich.rule import Rule
from rich.spinner import Spinner

from typing import Any, List, Dict, Optional

from pathlib import Path

import json
import glob
from datetime import datetime

try:
    import tiktoken
    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False

from toolbox import (
    get_storage_path,
    read_file, 
    save_file, 
    edit_file, 
    list_files, 
    run_terminal_command, 
    curl_url, 
    search_web,
    perform_research,
    manage_agenda,
    get_user_clarification,
    search_episodic_memory,
    retrieve_episodic_memory,
    load_dynamic_tools,
    prepare_active_skill,
    finalize_active_skill,
    manage_active_skill,
    fix_active_skill,
    take_screenshot,
    click_at_current_location,
    move_cursor_absolute,
    move_cursor_relative,
    send_scroll,
    inject_keyboard_input
)
from toolbox import shared_memory, update_core_memory, save_skill, read_skill, distill_journey, mark_quagmire, delete_passive_skill, list_skills, is_keyring_locked, unlock_keyring, get_locked_keyring
import time
import toolbox
from subagents import dispatch_subagent

from audio_handler import TTSManager, STTManager, AudioConfigModal

_BACKSTORY_LOCK = threading.Lock()
from telegram_handler import TelegramManager
from orchestration import AgentManager, SessionManager, AgentConfig, HistoryMessage, ScheduleManager

def get_session_name_map() -> dict:
    path = os.path.join(toolbox.FEDERATE_DIR, "session_names.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_session_name_map(m: dict):
    path = os.path.join(toolbox.FEDERATE_DIR, "session_names.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=4)
    except Exception:
        pass

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
        files = sorted(glob.glob(get_storage_path("sessions", "*.json")), key=os.path.getmtime, reverse=True)
        name_map = get_session_name_map()
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
        elif btn_id == "btn_restart_now":
            try: self.dismiss("restart")
            except Exception: self.app.pop_screen()

    def start_update_process(self):
        if self.is_updating:
            return
        self.is_updating = True
        
        status_lbl = self.query_one("#update_status", Label)
        status_lbl.update("[bold yellow]⏳ Updating Federate in background... Please wait.[/bold yellow]")
        
        try:
            self.query_one("#btn_update_now", Button).disabled = True
            self.query_one("#btn_skip_ver", Button).disabled = True
            self.query_one("#btn_defer_update", Button).disabled = True
        except Exception:
            pass
            
        self.perform_update_worker()

    @work(thread=True)
    def perform_update_worker(self):
        success, output = execute_system_update()
        self.app.call_from_thread(self.on_update_finished, success, output)

    def on_update_finished(self, success: bool, output: str):
        self.is_updating = False
        status_lbl = self.query_one("#update_status", Label)
        
        if success:
            status_lbl.update("[bold green]🎉 Update installed successfully! Restart required.[/bold green]")
            try:
                btn_update = self.query_one("#btn_update_now", Button)
                btn_update.label = "Restart Now"
                btn_update.id = "btn_restart_now"
                btn_update.variant = "success"
                btn_update.disabled = False
                btn_update.focus()
            except Exception:
                pass
        else:
            clean_err = escape(str(output)[:150]) if output else "Unknown error"
            status_lbl.update(f"[bold red]❌ Update failed:[/bold red] {clean_err}")
            try:
                btn_update = self.query_one("#btn_update_now", Button)
                btn_update.label = "Retry Update"
                btn_update.disabled = False
                
                self.query_one("#btn_close_update", Button).disabled = False
                self.query_one("#btn_close_update", Button).focus()
            except Exception:
                pass

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

                yield Label("Context Compression", classes="section_label")
                with Vertical(classes="field_container"):
                    yield Label("Verbatim Messages to Keep", classes="field_label")
                    yield Label("Number of recent dialogue messages kept verbatim at the end of the context (Minimum: 1).", classes="field_help")
                    yield Input(id="keep_verbatim_count", placeholder="e.g. 2")
                    
            with Horizontal(id="global_actions"):
                yield Button("Save Changes", id="save_global_btn", variant="success")
                yield Button("Cancel", id="cancel_global_btn", variant="error")

    def on_mount(self):
        from toolbox import load_global_settings
        config = load_global_settings()
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
        
        self.query_one("#research_images_max", Input).value = str(config.get("research_images_max", 10))
        self.query_one("#research_image_retries", Input).value = str(config.get("research_image_retries", 1))
        with self.prevent(Checkbox.Changed):
            self.query_one("#research_image_system_enabled", Checkbox).value = config.get("research_image_system_enabled", False)
            self.query_one("#research_images_as_links", Checkbox).value = not config.get("research_images_as_links", False)

    @on(Checkbox.Changed, "#research_images_as_links")
    def on_as_links_changed(self, event: Checkbox.Changed):
        if event.value:  # If checked (meaning Embed images directly is enabled)
            self.check_and_trigger_warning("research_images_as_links")

    @on(Checkbox.Changed, "#research_image_system_enabled")
    def on_system_enabled_changed(self, event: Checkbox.Changed):
        if event.value:  # If checked (meaning Image subsystem master switch is enabled)
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
        from toolbox import save_global_settings
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

        # Enforce that at least 1 message is kept verbatim
        if keep_verbatim < 1:
            self.notify("Invalid: Verbatim Messages to Keep must be at least 1.", severity="error")
            return
            
        try: img_max = int(self.query_one("#research_images_max", Input).value.strip())
        except ValueError: img_max = 10
        
        try: img_retries = int(self.query_one("#research_image_retries", Input).value.strip())
        except ValueError: img_retries = 1
        
        img_system_enabled = self.query_one("#research_image_system_enabled", Checkbox).value
        images_as_links = not self.query_one("#research_images_as_links", Checkbox).value

        config = {
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
        save_global_settings(config)
        
        # Dynamically sync the baseline variables in toolbox.py and the active view
        import toolbox
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
    Input:focus, Select:focus, SelectCurrent:focus { background: black !important; color: white !important; border: round $accent; }
    #onboard_actions { height: 4; align: center middle; border-top: solid $primary; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="onboard_dialog"):
            yield Label(" Welcome to Federate Multiagent Harness", classes="pane_title")
            with VerticalScroll(id="onboard_scroll"):
                with Vertical(classes="details_box"):
                    yield Label("Please configure your first agent to get started:", classes="section_label")
                    yield Label("Agent Name:")
                    yield Input("Rita", id="onboard_name")
                    yield Label("Agent Backstory:")
                    yield Input("You are Rita, a general purpose senior developer.", id="onboard_backstory")
                    yield Label("Model:")
                    yield Input("gemini-3.1-flash-lite", id="onboard_model")
                    yield Label("Base URL Preset:")
                    yield Select(BASE_URL_PRESETS, value="https://generativelanguage.googleapis.com/v1beta/openai/", id="onboard_base_url_preset", allow_blank=False)
                    yield Label("Base URL:")
                    yield Input("https://generativelanguage.googleapis.com/v1beta/openai/", id="onboard_base_url")
                    yield Label("API Key:")
                    yield Input(placeholder="Enter your API Key...", id="onboard_api_key", password=True)
            with Horizontal(id="onboard_actions"):
                yield Button("Get Started", id="onboard_submit_btn", variant="success")

    @on(Select.Changed, "#onboard_base_url_preset")
    def on_preset_changed(self, event: Select.Changed):
        if event.value != "custom" and event.value != Select.BLANK:
            self.query_one("#onboard_base_url", Input).value = str(event.value)

    @on(Input.Changed, "#onboard_base_url")
    def on_base_url_changed(self, event: Input.Changed):
        input_val = event.value.strip()
        matched = "custom"
        for label, val in BASE_URL_PRESETS:
            if val == input_val:
                matched = val
                break
        select_widget = self.query_one("#onboard_base_url_preset", Select)
        if select_widget.value != matched:
            select_widget.value = matched

    @on(Button.Pressed, "#onboard_submit_btn")
    def submit(self):
        name = self.query_one("#onboard_name", Input).value.strip()
        if not name:
            self.notify("Agent name cannot be empty.", severity="error")
            return
        self.dismiss({
            "name": name,
            "backstory": self.query_one("#onboard_backstory", Input).value.strip(),
            "model": self.query_one("#onboard_model", Input).value.strip(),
            "base_url": self.query_one("#onboard_base_url", Input).value.strip(),
            "api_key": self.query_one("#onboard_api_key", Input).value.strip(),
        })

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
        self.all_manageable_tools = [
            "list_files", "search_web", "perform_research", "manage_agenda",
            "read_file", "curl_url", "save_file", "edit_file", 
            "dispatch_subagent", "run_terminal_command", "visual_computer_operation"
        ]

    def compose(self) -> ComposeResult:
        with Vertical(id="config_dialog"):
            yield Label(f"Agent Editor: {self.agent_config.name}", classes="pane_title")
            with VerticalScroll(id="config_scroll"):
                with Vertical(classes="details_box"):
                    yield Label("Primary Settings", classes="section_label")
                    yield Label("Agent Name:")
                    yield Input(value=self.agent_config.name, id="ai_name")
                    yield Label("Backstory:")
                    yield Input(value=self.agent_config.backstory, id="ai_backstory")
                    yield Label("Model:")
                    yield Input(value=self.agent_config.model, id="ai_model")
                    yield Label("Reasoning Effort (o1, o3, gemini-thinking models):")
                    yield Select([("Low", "low"), ("Medium", "medium"), ("High", "high")], value=getattr(self.agent_config, "reasoning_effort", "high"), id="ai_reasoning_effort", allow_blank=False)
                    yield Label("Temperature:")
                    yield Input(value=str(getattr(self.agent_config, "temperature", 1.0)), id="ai_temperature")
                    yield Label("Base URL Preset:")
                    current_url = self.agent_config.base_url or "https://openrouter.ai/api/v1"
                    matched_preset = "custom"
                    for label, val in BASE_URL_PRESETS:
                        if val == current_url:
                            matched_preset = val
                            break
                    yield Select(BASE_URL_PRESETS, value=matched_preset, id="ai_base_url_preset", allow_blank=False)
                    
                    yield Label("Base URL:")
                    yield Input(value=current_url, id="ai_base_url")
                    yield Label("API Key:")
                    yield Input(value=self.agent_config.get_api_key(), id="ai_api_key", password=True)
                    yield Label("Agent Color (Hex):")
                    yield Input(value=self.agent_config.color, id="ai_color")
                    yield Label("TTS Voice:") # <-- NEW
                    
                    # Prepare list options dynamically based on the current assigned voice
                    current_voice = self.agent_config.tts_voice or "af_sarah"
                    voice_options = VOICE_OPTIONS.copy()
                    if not any(opt[1] == current_voice for opt in voice_options):
                        voice_options.insert(0, (f"{current_voice} (Custom)", current_voice))
                        
                    yield Select(voice_options, value=current_voice, id="ai_tts_voice", allow_blank=False) # <-- NEW
                    yield Label("Pronouns:") # <-- NEW
                    yield Select([("He/Him", "he/him"), ("She/Her", "she/her"), ("Neither", "neither")], value=self.agent_config.pronouns or "neither", id="ai_pronouns", allow_blank=False) # <-- NEW
                    with Horizontal(classes="config_row"):
                        yield Checkbox("Vision Capable", id="ai_vision_capable", value=self.agent_config.is_capable_vision)
                        yield Checkbox("Disable All Tools", id="ai_disable_all_tools", value=self.agent_config.disable_all_tools) # <-- NEW

                    yield Label("Agent Abilities (Enable in SAFE mode / Disable tool)", classes="section_label")
                    with VerticalScroll(id="abilities_container"):
                        disabled_list = getattr(self.agent_config, "disabled_tools", ["visual_computer_operation"])
                        for tool_name in self.all_manageable_tools:
                            is_enabled = tool_name in self.agent_config.enabled_tools
                            is_disabled = tool_name in disabled_list
                            label = "Autonomous Visual Computer Operation" if tool_name == "visual_computer_operation" else tool_name.replace("_", " ").title()
                            with Horizontal(classes="ability_row"):
                                yield Checkbox(f"Enable (Safe): {label}", id=f"ability_{tool_name}", value=is_enabled, classes="enable_cb")
                                yield Checkbox("Force Disable", id=f"disable_{tool_name}", value=is_disabled, classes="disable_cb")


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
            self.query_one("#ai_base_url", Input).value = str(event.value)

    @on(Input.Changed, "#ai_base_url")
    def on_base_url_input_changed(self, event: Input.Changed):
        input_val = event.value.strip()
        matched = "custom"
        for label, val in BASE_URL_PRESETS:
            if val == input_val:
                matched = val
                break
        
        select_widget = self.query_one("#ai_base_url_preset", Select)
        if select_widget.value != matched:
            select_widget.value = matched
    
    def _get_current_fields(self):
        enabled_tools = []
        disabled_tools = []
        for tool_name in self.all_manageable_tools:
            if self.query_one(f"#ability_{tool_name}", Checkbox).value:
                enabled_tools.append(tool_name)
            if self.query_one(f"#disable_{tool_name}", Checkbox).value:
                disabled_tools.append(tool_name)

        try:
            temp = float(self.query_one("#ai_temperature", Input).value.strip())
        except ValueError:
            temp = 1.0

        return {
            "name": self.query_one("#ai_name", Input).value.strip(),
            "backstory": self.query_one("#ai_backstory", Input).value.strip(),
            "model": self.query_one("#ai_model", Input).value.strip(),
            "base_url": self.query_one("#ai_base_url", Input).value.strip(),
            "is_capable_vision": self.query_one("#ai_vision_capable", Checkbox).value,
            "disable_all_tools": self.query_one("#ai_disable_all_tools", Checkbox).value,
            "api_key": self.query_one("#ai_api_key", Input).value.strip(),
            "color": self.query_one("#ai_color", Input).value.strip() or "#00FFFF",
            "backup_model": self.query_one("#ai_backup_model", Input).value.strip(),
            "backup_base_url": self.query_one("#ai_backup_base_url", Input).value.strip(),
            "backup_api_key": self.query_one("#ai_backup_api_key", Input).value.strip(),
            "use_backup": self.query_one("#ai_use_backup", Checkbox).value,
            "enabled_tools": enabled_tools,
            "disabled_tools": disabled_tools,
            "tts_voice": (self.query_one("#ai_tts_voice", Select).value if self.query_one("#ai_tts_voice", Select).value != Select.BLANK else None) or "af_sarah",
            "pronouns": (self.query_one("#ai_pronouns", Select).value if self.query_one("#ai_pronouns", Select).value != Select.BLANK else None) or "she/her",
            "reasoning_effort": (self.query_one("#ai_reasoning_effort", Select).value if self.query_one("#ai_reasoning_effort", Select).value != Select.BLANK else None) or "high",
            "temperature": temp
        }

    def _apply_save(self, is_new: bool):
        fields = self._get_current_fields()
        if not fields["name"]:
            return

        # Define the callback at the function level. It still safely closes over `is_new`.
        def handle_unlock(result):
            if not result:
                return
            action, password = result
            if action == "unlock" and password:
                if unlock_keyring(password):
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

        locked_keyring = get_locked_keyring()
        if locked_keyring:
            self.app.push_screen(KeyringUnlockModal(), handle_unlock)
            return

        new_config = AgentConfig(
            name=fields["name"],
            backstory=fields["backstory"],
            model=fields["model"],
            base_url=fields["base_url"],
            is_capable_vision=fields["is_capable_vision"],
            color=fields["color"],
            backup_model=fields["backup_model"],
            backup_base_url=fields["backup_base_url"],
            use_backup=fields["use_backup"],
            enabled_tools=fields["enabled_tools"],
            disabled_tools=fields["disabled_tools"],
            tts_voice=fields["tts_voice"], # <-- NEW
            pronouns=fields["pronouns"], # <-- NEW
            disable_all_tools=fields["disable_all_tools"], # <-- NEW
            reasoning_effort=fields["reasoning_effort"],
            temperature=fields["temperature"]
        )


        # Save keys to .env
        self._update_env(new_config.name, fields["api_key"], fields["backup_api_key"])

        self.dismiss(("save", new_config, is_new))

    def _update_env(self, agent_name: str, primary_key: str, backup_key: str):
        # We keep the function name '_update_env' to avoid changing other callers
        import keyring
        primary_user = f"agent_key_{agent_name.lower().replace(' ', '_')}"
        backup_user = f"agent_backup_key_{agent_name.lower().replace(' ', '_')}"

        try:
            # 1. Update Primary Credential
            if primary_key:
                keyring.set_password("Federate", primary_user, primary_key)
                os.environ[f"AGENT_KEY_{agent_name.upper().replace(' ', '_')}"] = primary_key
            else:
                try: keyring.delete_password("Federate", primary_user)
                except Exception: pass

            # 2. Update Backup Credential
            if backup_key:
                keyring.set_password("Federate", backup_user, backup_key)
                os.environ[f"AGENT_BACKUP_KEY_{agent_name.upper().replace(' ', '_')}"] = backup_key
            else:
                try: keyring.delete_password("Federate", backup_user)
                except Exception: pass

        except Exception as e:
            # Gracefully fail and natively notify the user that storage was rejected
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

# --- TEXTUAL AI UI WIDGET ---

# Mapping of slash command descriptions for the table
def get_installed_version() -> str:
    try:
        import importlib.metadata
        return importlib.metadata.version("federate")
    except Exception:
        try:
            import federate
            return getattr(federate, "__version__", "0.9.27")
        except Exception:
            return "0.9.27"

def execute_system_update() -> tuple[bool, str]:
    import subprocess, sys, os, shutil
    
    # 1. WINDOWS: Stream update script directly from GitHub
    if os.name == "nt" or sys.platform == "win32":
        for script in ["update.ps1", "install.ps1"]:
            ps_cmd = f"irm https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/Federate/main/{script} | iex"
            try:
                cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if res.returncode == 0:
                    return True, res.stdout
            except Exception:
                pass

    # 2. UNIX (macOS / Linux / Termux): Stream update script directly from GitHub
    else:
        for script in ["update.sh", "install.sh"]:
            sh_cmd = f"curl -LsSf https://raw.githubusercontent.com/ROCK-LAB-PRIVATE-LIMITED/Federate/main/{script} | bash"
            try:
                cmd = ["bash", "-c", sh_cmd]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if res.returncode == 0:
                    return True, res.stdout
            except Exception:
                pass

    # 3. Direct uv tool fallback
    uv_path = shutil.which("uv")
    if not uv_path:
        user_bin_uv = os.path.join(os.path.expanduser("~"), ".local", "bin", "uv" + (".exe" if os.name == "nt" else ""))
        if os.path.exists(user_bin_uv):
            uv_path = user_bin_uv

    if uv_path:
        try:
            cmd = [uv_path, "tool", "install", "--upgrade", "--refresh", "federate[all]"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if res.returncode == 0:
                return True, res.stdout
        except Exception:
            pass

    # 4. Direct pip fallback
    try:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "federate[all]"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.returncode == 0:
            return True, res.stdout
        return False, res.stderr or res.stdout
    except Exception as e:
        return False, str(e)

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
    "/tts": "Toggle Text-to-Speech (TTS) voice output",
    "/stt": "Toggle Speech-to-Text (STT) hotword listening",
    "/readback": "Read back the last AI response with TTS",
    "/speech": "Open Audio/Voice configuration modal",
    "/telegram": "Configure Telegram Bot integration",
    "/select_agent": "Switch the active host agent",
    "/clear_all": "Wipe memory and history of all agents",
    "/skills": "List all passive and active skills for the active agent",
    "/settings": "Open global harness settings modal",
    "/help": "Show this detailed help menu"
}

class ScheduleModal(ModalScreen[None]):
    DEFAULT_CSS = """
    ScheduleModal { align: center middle; background: $background 60%; }
    #sched_dialog { width: 75; height: 85%; border: thick $primary; background: $surface; padding: 1 2; }
    #task_list { height: 1fr; border: round $accent; overflow-y: auto; margin-bottom: 1; background: $boost; }
    .task_item { layout: horizontal; height: auto; padding: 1; border-bottom: solid $primary 50%; }
    .task_info { width: 1fr; }
    .task_del_btn { width: 10; margin-left: 1; margin-top: 1; }
    .task_edit_btn { width: 10; margin-left: 1; margin-top: 1; }
    #add_form { border-top: solid $primary; padding-top: 1; height: auto; }
    .form_row { layout: horizontal; height: auto; margin-bottom: 1; }
    #new_agent { width: 40%; }
    #new_time { width: 60%; margin-left: 1; }
    #new_date { width: 50%; }
    #new_repeat { width: 50%; margin-left: 1; }
    """
    def __init__(self, agent_view):
        super().__init__()
        self.agent_view = agent_view
        
    def _get_next_run(self, task):
        from datetime import datetime, timedelta
        import calendar
        now = datetime.now()
        try:
            task_date = datetime.strptime(getattr(task, "date_str", ""), "%Y-%m-%d") if getattr(task, "date_str", "") else now
            th, tm = map(int, task.time_str.split(":"))
            candidate = datetime(task_date.year, task_date.month, task_date.day, th, tm)
        except Exception:
            return None

        repeat_mode = getattr(task, "repeat", "daily")
        while candidate <= now or (task.last_run_date == now.strftime("%Y-%m-%d") and candidate.date() <= now.date()):
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
            yield VerticalScroll(id="task_list")
            
            with Vertical(id="add_form"):
                yield Label("Add New Scheduled Task:")
                with Horizontal(classes="form_row"):
                    agents = [(a, a) for a in self.agent_view.agent_manager.agents.keys()]
                    yield Select(agents, id="new_agent", prompt="Select Agent")
                    yield Input(placeholder="HH:MM (24h format, e.g., 14:30)", id="new_time")
                with Horizontal(classes="form_row"):
                    yield Input(placeholder="YYYY-MM-DD (e.g., 2026-07-16, optional)", id="new_date")
                    repeats = [("Daily", "daily"), ("Weekly", "weekly"), ("Monthly", "monthly"), ("Annually", "annually")]
                    yield Select(repeats, value="daily", id="new_repeat", allow_blank=False)
                yield Input(placeholder="Task Prompt...", id="new_prompt", classes="form_row")
                with Horizontal(classes="form_row"):
                    yield Button("Add Task", id="add_task_btn", variant="success")
                    yield Button("Close", id="close_btn", variant="error")

    def on_mount(self):
        self.refresh_list()
        
    def refresh_list(self):
        container = self.query_one("#task_list")
        container.query("*").remove()
        for t in self.agent_view.schedule_manager.tasks:
            next_run = self._get_next_run(t)
            next_run_str = f"\nNext run: {next_run.strftime('%Y-%m-%d @ %H:%M')}" if next_run else f"Time: {t.time_str}"
            repeat_part = f" [{getattr(t, 'repeat', 'daily').title()}]"
            info = f"[bold cyan]{t.agent_name}[/bold cyan]  [bold yellow]{next_run_str}[/bold yellow]{repeat_part}\n[dim]{t.prompt}[/dim]"
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
            prompt = self.query_one("#new_prompt", Input).value.strip()
            date_str = self.query_one("#new_date", Input).value.strip()
            repeat_val = self.query_one("#new_repeat", Select).value
            repeat = str(repeat_val) if repeat_val != Select.BLANK else "daily"
            
            if agent and Select.BLANK != agent and time_str and prompt:
                # Strict regex validation for 24-hour HH:MM format (00:00 to 23:59)
                import re
                if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", time_str):
                    self.notify("Time must be a valid 24-hour time in HH:MM format (e.g., 14:30)", severity="error")
                    return
                
                # Strict calendar validation for YYYY-MM-DD if provided
                if date_str:
                    try:
                        from datetime import datetime
                        datetime.strptime(date_str, "%Y-%m-%d")
                    except ValueError:
                        self.notify("Date must be a valid calendar date in YYYY-MM-DD format (e.g., 2026-07-16)", severity="error")
                        return
                
                # Sanitize the prompt input by stripping leading/trailing whitespace
                prompt = prompt.strip()
                
                self.agent_view.schedule_manager.add_task(agent, time_str, prompt, date_str=date_str, repeat=repeat)
                self.query_one("#new_time", Input).value = ""
                self.query_one("#new_date", Input).value = ""
                self.query_one("#new_prompt", Input).value = ""
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
                # 1. Populate the input widgets with the existing properties
                self.query_one("#new_agent", Select).value = task.agent_name
                self.query_one("#new_time", Input).value = task.time_str
                self.query_one("#new_prompt", Input).value = task.prompt
                
                # 2. Delete the old task and update the list
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

    def __init__(self, task, **kwargs):
        super().__init__(**kwargs)
        self.sched_task = task
        self.time_left = 60
        self.timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="sched_prompt_dialog"):
            yield Label(f" Scheduled Task Due: [bold cyan]{self.sched_task.agent_name}[/bold cyan]", classes="pane_title")
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
    """Custom Multiline TextArea that supports cycling suggestions and Enter-to-submit."""
    
    BINDINGS = [
        Binding("ctrl+a", "abort", "Abort", show=True),
    ]

    class AbortRequest(Message):
        pass

    class Submitted(Message):
        """Internal message to trigger chat submission."""
        def __init__(self, input_widget, value: str):
            super().__init__()
            self.input = input_widget
            self.value = value

    def action_abort(self) -> None:
        self.post_message(self.AbortRequest())

    def __init__(self, *args, **kwargs):
        # TextArea doesn't use the suggester or placeholder kwargs, so we pop them
        kwargs.pop("suggester", None)
        kwargs.pop("placeholder", None)
        super().__init__(*args, **kwargs)
        self.show_line_numbers = False
        self._suggestion_matches = []
        self._suggestion_index = 0
        self._base_val = ""
        self._mode = "file"

    async def _on_message(self, message: Message) -> None:
        """Low-level message listener to reliably capture changes on self."""
        await super()._on_message(message)
        if message.__class__.__name__ == "Changed":
            self.handle_text_changed()

    def on_key(self, event: events.Key) -> None:
        """Raw keyboard intercept to override Textual's stubborn TextArea defaults."""
        
        # 1. If we have active suggestions, hijack Up/Down/Right arrows completely
        if self._suggestion_matches:
            if event.key == "up":
                event.prevent_default()
                event.stop() # Stops the cursor_up action from firing
                self.cycle_suggestions(-1)
                return
            elif event.key == "down":
                event.prevent_default()
                event.stop() # Stops the cursor_down action from firing
                self.cycle_suggestions(1)
                return
            elif event.key == "right":
                event.prevent_default()
                event.stop() # Stops cursor movement after inserting
                self.commit_suggestion()
                return
        
        # 2. Check for Newline combos FIRST
        if event.key in ("shift+enter", "alt+enter", "ctrl+j"):
            event.prevent_default()
            self.insert("\n")
            
        # 3. Check for standard Enter LAST to send the message
        elif event.key == "enter":
            event.prevent_default()
            val = self.text.strip()
            self.post_message(self.Submitted(self, val))

    def handle_text_changed(self) -> None:
        """Processes text changes to find autocompletion matches."""
        val = self.text
        
        # Handle / for slash commands
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

        # Handle & for files
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

        # Handle @ for agents
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
        """Retrieves helpful contextual info for each completion choice."""
        if mode == "command":
            return SLASH_COMMAND_DESCS.get(match, "Slash Command")
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
        """Visual table displaying scrollable suggestions below the input widget."""
        try:
            preview = self.app.query_one("#ai_suggestions_preview")
            if self._suggestion_matches:
                preview.styles.display = "block"
                
                # Sliding 4-item scroll window centered around selection
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
        """Commits the highlighted suggestion when pressing the Right Arrow."""
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
            # Move cursor to end of the newly completed text
            lines = self.text.split("\n")
            self.cursor_location = (len(lines)-1, len(lines[-1]))
            
        # Wipe selection window on insert
        self._suggestion_matches = []
        self.update_suggestions_ui()

_BANNER_STATS = None
_STATS_LOADING = False
_WELCOME_STATS_LOCK = threading.Lock()

def invalidate_stats_cache():
    """Wipes the cached banner statistics to force a background recalculation next time."""
    global _BANNER_STATS
    with _WELCOME_STATS_LOCK:
        _BANNER_STATS = None

def calculate_stats_raw(agent_view) -> dict:
    """Internal helper that performs the heavy session analysis and token estimation."""
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

    # 1. Determine last calendar month
    now = datetime.now()
    first_day_current_month = now.replace(day=1)
    last_day_last_month = first_day_current_month - timedelta(days=1)
    target_year = last_day_last_month.year
    target_month = last_day_last_month.month
    
    # 2. Scan sessions directory
    sessions_dir = get_storage_path("sessions")
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
            
            # Count tools called and inputs/outputs in target month
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

    # Fallback logic if last calendar month had no activity
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
    """Triggers background stats collection thread to avoid freezing the TUI."""
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
            
            # Post-completion UI update callback on TUI main event loop
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
    
    # 1. Return a non-blocking loading placeholder if statistical data is not yet available
    if _BANNER_STATS is None:
        if not _STATS_LOADING:
            trigger_stats_loading(agent_view)
            
        from rich.console import Group
        from rich.text import Text
        from rich.table import Table
        from rich.rule import Rule
        
        title_text = Text.from_markup("[bold #f2a813]\u276f [/bold #f2a813][bold #da6057]FEDERATE[/bold #da6057]\n\u00a9 Rock Lab Private Limited", justify="center")
        divider = Rule(style="#f2a813")
        
        loading_table = Table(show_header=False, expand=True, box=None, padding=(0, 2))
        loading_table.add_column(ratio=1, justify="center")
        loading_table.add_row(Text.from_markup("[dim] Processing agent telemetry & stats...[/dim]"))
        
        tips_text = Text.from_markup(
            "  [bold #f2a813]Tips for getting started:[/bold #f2a813]\n"
            "  1. Ask questions, edit files, or run commands.\n"
            "  2. Use & to inject files. Use @ to invoke particular agents.\n"
            "  3. Press F2 to configure the active agent.\n"
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

    # 2. Construct the layout instantly using cached background stats
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
    skills_dir = get_storage_path("agents", "skills", safe_name)
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

    # FIXED: Compute tool stats bound directly to the shown best_agent rather than global sums
    total_tools_run = agent_total_tools.get(best_agent, 0)
    successful_tools_run = agent_successful_tools.get(best_agent, 0)
    
    t_pct = int((successful_tools_run / total_tools_run) * 100) if total_tools_run > 0 else 0

    from rich.console import Group
    from rich.text import Text
    from rich.table import Table
    from rich.rule import Rule

    title_text = Text.from_markup(f"[bold #f2a813]\u276f [/bold #f2a813][bold #da6057]FEDERATE[/bold #da6057] [dim]v{get_installed_version()}[/dim]\n\u00a9 Rock Lab Private Limited", justify="center")
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
        "  3. Press F2 to configure the active agent.\n"
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
    """Parses LaTeX math blocks into Unicode for terminal rendering."""
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
    """Full-screen Chat Agent interface with Multi-Agent support."""
    
    BINDINGS =[
        Binding("f2", "open_chat_manager", "Sessions"),
        Binding("ctrl+k", "clear_all_contexts", "New Chat", priority=True),
        Binding("f4", "open_active_config", "Manage Agents"),
        Binding("f5", "switch_agent", "Switch Agent"),
        Binding("ctrl+t", "cycle_arm_mode", "Cycle Mode"),
        Binding("ctrl+g", "cycle_agents", "Cycle Agents"),
        Binding("ctrl+a", "abort", "Abort"),
        Binding("f3", "open_global_settings", "Settings"),
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
        max-height: 6; /* Fits up to 4 lines of suggestions + borders cleanly */
        padding: 0 1;
        margin-top: 0;
    }
    #status_bar { height: auto; min-height: 1; width: 100%; layout: grid; grid-size: 3; }
    .status_left { color: #87cefa; }
    .status_center { 
        width: 100%;
        color: $text;
        content-align: center middle; 
        text-align: center;
    }
    .status_right { color: #dda0dd; text-align: right; }
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
            
        yield Static(id="ai_suggestions_preview") # Yield the preview directly below the input
            
        with Horizontal(id="status_bar"):
            yield Label(f"{os.getcwd()}", id="ai_cwd_label", classes="status_left")
            yield Label("", id="ai_config_label", classes="status_center")
            yield Label("", id="ai_token_label", classes="status_right")

    def on_mount(self):
        toolbox.CURRENT_APP = self.app
        toolbox.CURRENT_AGENT_VIEW = self
        toolbox.CURRENT_LOG_CB = self.log_to_ui
        self.current_tokens = 0
        
        # Initialize Orchestration
        self.agent_manager = AgentManager()
        self.session_manager = SessionManager()
        self.schedule_manager = ScheduleManager()
        self.current_batch_id = 0
        
        # Load the persisted default
        default_name = self.agent_manager.get_default_agent_name()
        initial_agent = self.agent_manager.get_agent(default_name) or list(self.agent_manager.agents.values())[0]
        self.select_agent(initial_agent.name)

        # STARTUP: Handle Termux/EncryptedKeyring blocking
        from toolbox import is_keyring_locked, unlock_keyring
        if is_keyring_locked():
            def handle_initial_unlock(result):
                if result:
                    action, password = result
                    if action == "unlock" and password:
                        if unlock_keyring(password):
                            self.notify("Keyring unlocked.", severity="information")
                            self.select_agent(self.active_agent.name)
                            self.update_status_bar()
                            self.check_onboarding()
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
                                    # Dynamically resolve and delete the file if it exists
                                    if hasattr(b, "file_path") and b.file_path and os.path.exists(b.file_path):
                                        os.remove(b.file_path)
                                    # Inject the new password into memory
                                    b.__dict__['keyring_key'] = password
                            
                            self.notify("Keyring initialized successfully. New password established.", severity="warning")
                            self.select_agent(self.active_agent.name)
                            self.update_status_bar()
                            self.check_onboarding()
                        except Exception as e:
                            self.notify(f"Reset failed: {e}", severity="error")
            
            self.app.push_screen(KeyringUnlockModal(), handle_initial_unlock)
        else:
            self.check_onboarding()
        
        self.agent_executors = {} # Cache for react agents
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
        self.turn_lock = threading.Lock()

        self._write_log(get_welcome_banner(self))
        self.update_tokens()
        if "-r" in sys.argv:
            # We delay the call until the UI is fully painted and stable
            self.call_after_refresh(self.action_resume_last)
        self.query_one("#ai_chat_input").focus()
        
        self.spinner_chars =["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.spinner_idx = 0
        self.set_interval(0.1, self.tick_spinners)
        self.set_interval(60.0, self.tick_scheduler)
        self.check_for_updates_bg(manual=False)

    def select_agent(self, name: str) -> bool:
        agent = self.agent_manager.get_agent(name)
        if agent:
            self.active_agent = agent
            #self.session_manager.init_agent_session(agent, list(self.agent_manager.agents.values()))
            
            # Apply agent-specific color to the UI programmatically
            try:
                input_container = self.query_one("#input_container")
                prompt_label = self.query_one("#prompt_label")
                
                # Update borders and label color
                input_container.styles.border_top = ("solid", agent.color)
                input_container.styles.border_bottom = ("solid", agent.color)
                prompt_label.styles.color = agent.color
            except Exception:
                pass
            
            self.update_tokens()
            return True
        return False
    
    def confirm_tool_execution(self, tool_name: str, arguments: dict, agent_name: str = "Agent") -> bool:
        """Pushes the ToolConfirmationModal and blocks the background worker thread until approved/rejected."""
        result_event = threading.Event()
        final_result = [False]

        def handle_result(res: Any):
            if res == "stop":
                self.action_abort()
                final_result[0] = False
            else:
                final_result[0] = (res == "approve")
            result_event.set()

        # Thread-safe instantiation and push of the ModalScreen on the main event loop thread
        def push_modal():
            modal = ToolConfirmationModal(tool_name, arguments, agent_name=agent_name)
            self.app.push_screen(modal, handle_result)

        self.app.call_from_thread(push_modal)
        
        while not result_event.is_set():
            if toolbox.ABORT_EVENT.is_set():
                return False
            result_event.wait(0.1) # Efficient wake-up with timeout (releases GIL)
            
        return final_result[0]
    
    def action_clear_all_contexts(self):
        self.action_abort() 
        self.session_manager.clear_all_contexts()
        self.agent_executors = {} # Clear cached executors so updated schemas load fresh
        self.agent_mode = "PLAN"
        self.clear_chat_ui()
        invalidate_stats_cache() # Evicts old stats cache prior to rebuild
        self._write_log(Rule(title="[bold yellow]ALL CONTEXTS CLEARED", style="dim"))
        self._write_log(get_welcome_banner(self))
        self.update_tokens()
    
    def action_resume_last(self):
        """Finds the last modified session and uses the existing load logic."""
        files = sorted(glob.glob(get_storage_path("sessions", "*.json")), key=os.path.getmtime)
        if len(files) > 1:
            self.load_chat_file(files[-2])
        
    def action_abort(self):
        """Interrupts current agent tasks, stops the spinner, and returns focus to input."""
        toolbox.ABORT_EVENT.set()
        
        # Persistent Abort: Mark the current batch as dead
        if hasattr(self, "current_batch_id"):
            self.session_manager.abort_batch(self.current_batch_id)

        # DROP THE HAMMER: Instantly kill all executing background agent threads
        toolbox.nuke_all_threads()

        self._running_agents.clear()
        if self.workers:
            self.workers.cancel_all()
            self._toggle_spinner(False)
            
            # Ensure Research Progress UI dies
            try:
                self.query_one("#progress_container").styles.display = "none"
            except: pass

            self.log_to_ui("[bold red] Operation Aborted by User.[/bold red]")
            self.query_one("#ai_chat_input").focus()
        
    
    def action_open_chat_manager(self):
        """F2 triggers the Session menu."""
        def handle_chat_mgr(action):
            if action == "new_session":
                self.action_clear_all_contexts()
            elif action == "load_chat":
                self.app.push_screen(ChatLoadModal(), self.load_chat_file)
        self.app.push_screen(ChatManagerModal(), handle_chat_mgr)

    def action_switch_agent(self):
        """Ctrl+M shows scrollable buttons + Default checkbox."""
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
        """Loads a multi-agent session file and reconstructs the UI accurately."""
        if not filepath: return
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Reconstruct HistoryMessage objects
            messages = [HistoryMessage(**m) for m in data]
            
            # Extract agent name from filename (Standard: sess_TIMESTAMP_AgentName.json)
            base = os.path.basename(filepath).replace(".json", "")
            agent_name = base.split("_")[-1]
            sess_id = "_".join(base.split("_")[:-1])

            # Update the global session state
            self.session_manager.current_session_id = sess_id
            self.session_manager.active_sessions[agent_name] = messages
            
            # Switch the UI to the agent whose file we just loaded
            if self.select_agent(agent_name):
                self.replay_chat(messages, agent_name)
                self.log_to_ui(f"[bold green]Restored Session: {sess_id} (agent: {agent_name})[/bold green]")
        except Exception as e:
            self.log_to_ui(f"[bold red]Load Error:[/bold red] {e}")

    def replay_chat(self, history: List[HistoryMessage], owner_name: str):
        """Processes tags to rename labels and clean content during UI reconstruction."""
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
                    self._write_log(Rule(style="dim"))
                    self._write_log(f"[bold {color}]{owner_name}:[/bold {color}]", is_markdown=False)
                    self._write_log(content, is_markdown=True)

                if hm.tool_calls:
                    for tc in hm.tool_calls:
                        tc_name = tc.get("name", "tool")
                        tc_args = str(tc.get("args", {}))
                        self._write_log(Rule(style="dim"))
                        self._write_log(f"[#808080]Calling Tool: {escape(tc_name)} with args: {escape(tc_args)}[/#808080]")

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
                        self._write_log(box_widget)
            else: # role == "human"
                # Check for synced intercom tags from other agents
                intercom_match = re.search(r'<AGENT_INTERCOM sender="([^"]+)">([\s\S]*?)</AGENT_INTERCOM>', content)
                tool_match = re.search(r'<AGENT_INTERCOM_TOOL_RESPONSE agent="([^"]+)" tool="([^"]+)">([\s\S]*?)</AGENT_INTERCOM_TOOL_RESPONSE>', content)

                if intercom_match:
                    label = intercom_match.group(1) # The synced agent's name
                    content = intercom_match.group(2).strip()
                    color = "cyan"
                elif tool_match:
                    label = f"{tool_match.group(1)} (Tool: {tool_match.group(2)})"
                    content = tool_match.group(3).strip()
                    color = "bright_black" # Dim/Gray for tool output
                else:
                    # Genuine user message
                    label = "User"
                    color = "blue"

                self._write_log(Rule(style="dim"))
                self._write_log(f"[bold {color}]{label}:[/bold {color}]", is_markdown=False)
                self._write_log(content, is_markdown=True)
    
    def action_open_active_config(self):
        """F4: Opens the editor. Distinguishes between Renaming and Cloning."""
        def handle_modal_result(result_tuple):
            if not result_tuple:
                return
                
            action = result_tuple[0]
            
            if action == "delete":
                agent_to_delete = result_tuple[1]
                if len(self.agent_manager.agents) <= 1:
                    self.notify("Error: You cannot delete your last remaining agent.", severity="error")
                    return
                
                # Delete agent config and clear from executors
                self.agent_manager.delete_agent(agent_to_delete.name)
                self.agent_executors.pop(agent_to_delete.name, None)
                
                # Wipe the API keys from the OS Keyring for security
                try:
                    import keyring
                    keyring.delete_password("Federate", f"agent_key_{agent_to_delete.name.lower().replace(' ', '_')}")
                    keyring.delete_password("Federate", f"agent_backup_key_{agent_to_delete.name.lower().replace(' ', '_')}")
                except Exception:
                    pass
                
                # Switch to the next available agent
                next_agent_name = list(self.agent_manager.agents.keys())[0]
                self.select_agent(next_agent_name)
                
                # Update default agent just in case the deleted one was the default
                self.agent_manager.set_default_agent_name(next_agent_name)
                
                # Update UI
                self.log_to_ui(f"[bold red]Agent '{agent_to_delete.name}' deleted.[/bold red] Switched to '{next_agent_name}'.")
                self.update_status_bar()
                
                # Sync welcome banner
                new_renderable = get_welcome_banner(self, specific_agent=next_agent_name, return_renderable=True)
                banners = self.query(".welcome_banner_box")
                if banners:
                    for banner in banners:
                        banner.update(new_renderable)
                return

            if action != "save":
                return
            
            # Extract the new config and the 'is_new' flag
            _, new_config, is_new = result_tuple
            old_name = self.active_agent.name
            
            # 1. Always save the new name
            self.agent_manager.save_agent(new_config)
            
            # 2. Only delete the old one if it's an UPDATE (Rename), NOT if it's SAVE AS NEW (Clone)
            if not is_new and new_config.name != old_name:
                self.agent_manager.delete_agent(old_name)
                self.agent_executors.pop(old_name, None)
            
            # 3. Switch to the agent (whether it's the renamed one or the brand new clone)
            self.select_agent(new_config.name)
            self.agent_executors.pop(new_config.name, None) 
            
            status_msg = "created and activated" if is_new else "updated"
            self.log_to_ui(f"[bold green]Agent '{new_config.name}' {status_msg}.[/bold green]")
            self.update_status_bar()

        self.app.push_screen(ConfigModal(self.active_agent, self.agent_manager), handle_modal_result)
    
    def action_open_global_settings(self):
        """F3: Opens the global harness settings."""
        def handle_global_config(result):
            if result == "update":
                self.log_to_ui("[bold green] Global settings successfully updated.[/bold green]")
                
        self.app.push_screen(GlobalSettingsModal(), handle_global_config)
    
    def action_open_config(self):
        """Now handles F2 Chat Management."""
        def handle_chat_mgr(action):
            if action == "new_session":
                self.action_clear_all_contexts()
            elif action == "load_chat":
                # Re-use your existing ChatLoadModal logic
                def handle_load(filepath):
                    if filepath:
                        # You need to implement load_chat_file logic or 
                        # adapt it to multi-agent sessions here
                        self.log_to_ui(f"Loading {filepath}...") 
                self.app.push_screen(ChatLoadModal(), handle_load)

        self.app.push_screen(ChatManagerModal(), handle_chat_mgr)

    def log_to_ui(self, msg: Any, is_markdown: bool = False):
        try:
            self.app.call_from_thread(self._write_log, msg, is_markdown)
        except RuntimeError:
            self._write_log(msg, is_markdown)
    
    def _write_log(self, msg: Any, is_markdown: bool = False):
        try:
            chat_body = self.query_one("#chat_messages")
            
            # If msg is markup like "[bold blue]User:[/bold blue]", extract label to try and find agent color
            if isinstance(msg, str) and not is_markdown:
                # Regex matches both opening style tags and optional closing style suffixes
                match = re.search(r'\[bold ([^\]]+)\]([^:]+):\[/bold(?:\s+[^\]]+)?\]', msg)
                if match:
                    label_text = match.group(2).strip()
                    # Try to find agent by name
                    agent = self.agent_manager.get_agent(label_text)
                    if agent:
                        # Override both opening and closing tag colors to prevent mismatches
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
                        
                        # Pre-validate all styles immediately to catch MissingStyle before the async rendering phase
                        if not hasattr(self, "_dummy_console"):
                            from rich.console import Console
                            self._dummy_console = Console()
                            
                        for span in parsed_text.spans:
                            if isinstance(span.style, str):
                                self._dummy_console.get_style(span.style)
                                
                        widget = Static(parsed_text, classes="chat_msg", markup=False)
                    except Exception:
                        # Universal fallback to raw string if the text contains invalid bracket markup (e.g. code arrays)
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
                
            backup_str = " [bold yellow][B][/]" if self.active_agent.use_backup else ""
            agent_info = f"[bold {self.active_agent.color}]{self.active_agent.name}{backup_str}[/] ({self.active_agent.model}) [bold magenta]({self.current_tokens})[/bold magenta]"
            
            try:
                app = self.app
                base_dir = str(app.query_one("#dir_tree").path) if app else os.getcwd()
            except Exception:
                base_dir = os.getcwd()
            # Use Text() to entirely disable markup parsing on raw paths
            self.query_one("#ai_cwd_label", Label).update(Text(base_dir))
            self.query_one("#ai_config_label", Label).update(f"[F3] {mode_str}")
            self.query_one("#ai_token_label", Label).update(agent_info)
        except Exception: pass
        self.update_prompt_label()

    def update_prompt_label(self):
        try:
            label = self.query_one("#prompt_label", Label)
            backup_str = " [B]" if self.active_agent.use_backup else ""
            if self.shell_mode:
                folder = os.path.basename(os.getcwd()) or os.getcwd()
                label.update(Text.from_markup(f"[bold red]shell@{escape(folder)} %[/bold red]"))
            else:
                label.update(f"[bold {self.active_agent.color}]{self.active_agent.name}{backup_str}>[/bold {self.active_agent.color}]")
        except Exception: pass

    def action_cycle_arm_mode(self):
        """Cycles through safety modes: PLAN -> INTERMEDIATE -> EXECUTE."""
        ARM_MODES = ["PLAN", "INTERMEDIATE", "EXECUTE"]
        current_idx = ARM_MODES.index(self.agent_mode) if self.agent_mode in ARM_MODES else 0
        next_idx = (current_idx + 1) % len(ARM_MODES)
        self.agent_mode = ARM_MODES[next_idx]
        self.agent_executors = {} # Force re-init of all executors
        self.log_to_ui(f"System operating mode cycled to: {self.agent_mode}")
        self.update_status_bar()

    def action_cycle_agents(self):
        """Cycles the active host agent sequentially through all configured personas."""
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
        """Maintains backwards compatibility for any slash command hooks using the old toggle."""
        self.action_cycle_arm_mode()

    def clear_chat_ui(self):
        try:
            self.query_one("#chat_messages").query("*").remove()
            self.query_one("#progress_left").query("*").remove()
            self.query_one("#progress_right", RichLog).clear()
            self.query_one("#progress_container").styles.display = "none"
        except Exception: pass

    def get_executor(self, agent_config: AgentConfig):
        if agent_config.name in self.agent_executors:
            return self.agent_executors[agent_config.name]
        
        if agent_config.use_backup and agent_config.backup_model:
            model = agent_config.backup_model
            base_url = agent_config.backup_base_url or agent_config.base_url
            api_key = agent_config.get_backup_api_key()
        else:
            model = agent_config.model
            base_url = agent_config.base_url
            api_key = agent_config.get_api_key()

        if not api_key:
            return None
            
        llm = ChatOpenAI(
            model=model, 
            temperature=getattr(agent_config, "temperature", 1.0),
            api_key=api_key,
            base_url=base_url,
            max_retries=5,
            timeout=120,
            model_kwargs={"reasoning_effort": getattr(agent_config, "reasoning_effort", "high")}
        )

        # Unified message pre-processor to intercept tool outputs and extract image payloads.
        # It packages image data strictly inside companion HumanMessages immediately 
        # following the ToolMessage, enabling real-time visual analysis while remaining 
        # compliant with all LLM provider APIs (Gemini, Claude, OpenAI).
        def preprocess_messages(messages):
            if not isinstance(messages, list):
                return messages
            processed = []
            for msg in messages:
                processed.append(msg)
                
                # Check for image triggers within plain-text ToolMessage responses
                if msg.__class__.__name__ == "ToolMessage" and isinstance(msg.content, str):
                    # Guardrail: Do not extract images if viewing source code, file structures, 
                    # or raw search results to prevent matching dummy tags or literal string variables.
                    tool_name = getattr(msg, "name", None)
                    if tool_name in {"edit_file", "save_file", "list_files", "search_web", "perform_research", "manage_agenda"}:
                        continue
                    
                    # Case A: Local file paths - Support multiple images
                    if "[Attached Image:" in msg.content:
                        matches = re.finditer(r'\[Attached Image:\s*(.*?)\]', msg.content)
                        for match in matches:
                            filepath = match.group(1).strip()
                            try:
                                from toolbox import get_safe_path
                                import base64, mimetypes
                                resolved_path, _ = get_safe_path(filepath)
                                if os.path.exists(resolved_path):
                                    mime = mimetypes.guess_type(resolved_path)[0] or "image/png"
                                    with open(resolved_path, "rb") as f:
                                        b64 = base64.b64encode(f.read()).decode('utf-8')
                                    
                                    companion = HumanMessage(content=[
                                        {"type": "text", "text": f"[Attached Image: {filepath}]"},
                                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                                    ])
                                    processed.append(companion)
                            except Exception:
                                pass
                                
                    # Case B: Legacy direct Base64 output streams - Support multiple images
                    if "[ImageBase64:" in msg.content:
                        matches = re.finditer(r'\[ImageBase64:\s*(data:image/[a-zA-Z]+;base64,[^\]]+)\]', msg.content)
                        for match in matches:
                            url = match.group(1).strip().replace("\n", "").replace("\r", "").replace(" ", "")
                            # Guardrail: Ignore variables, template markers, or unfinished streams
                            if any(marker in url for marker in ["{", "}", "<", ">", "b64_str", "base64data"]):
                                continue
                            # Omit the giant Base64 text wall from the ToolMessage inline to save input tokens
                            msg.content = msg.content.replace(match.group(0), "[ImageBase64: <data_transmitted>]")
                            companion = HumanMessage(content=[
                                {"type": "text", "text": "[Dynamic Visual Output]:"},
                                {"type": "image_url", "image_url": {"url": url}}
                            ])
                            processed.append(companion)
            return processed

        # Intercept LLM execution at the lowest level of BaseChatModel
        orig_generate = llm._generate
        def patched_generate(messages, stop=None, run_manager=None, **kwargs):
            processed_messages = preprocess_messages(messages)
            return orig_generate(processed_messages, stop=stop, run_manager=run_manager, **kwargs)
        llm._generate = patched_generate

        orig_stream = llm._stream
        def patched_stream(messages, stop=None, run_manager=None, **kwargs):
            processed_messages = preprocess_messages(messages)
            return orig_stream(processed_messages, stop=stop, run_manager=run_manager, **kwargs)
        llm._stream = patched_stream
        
        # --- NEW: ENFORCE DYNAMIC RESTRICTIONS ---
        if agent_config.disable_all_tools:
            # Only passive text-only skills, memories, and clarification remain active
            tools = [
                update_core_memory, save_skill, read_skill, list_skills, 
                distill_journey, delete_passive_skill, mark_quagmire, 
                get_user_clarification, search_episodic_memory, retrieve_episodic_memory
            ]
            allowed_names = {getattr(t, "name", t) for t in tools}
            
            # Map out all executable, terminal, research, active skill development, and high-privilege tools
            other_tool_names = [
                "list_files", "search_web", "perform_research", "manage_agenda",
                "read_file", "curl_url", "save_file", "edit_file", 
                "dispatch_subagent", "run_terminal_command", "take_screenshot",        
                "click_at_current_location", "move_cursor_absolute", 
                "move_cursor_relative", "send_scroll", "inject_keyboard_input",
                "prepare_active_skill", "finalize_active_skill", "manage_active_skill", "fix_active_skill"
            ]
            try:
                dynamic_tools = load_dynamic_tools(agent_config.name)
                for dt in dynamic_tools:
                    if dt.name not in other_tool_names:
                        other_tool_names.append(dt.name)
            except Exception:
                pass
                
            # Create safe, dummy unauthorized placeholders for all executable tools
            from langchain_core.tools import StructuredTool
            dummy_tools = []
            for name in other_tool_names:
                dummy_tools.append(StructuredTool.from_function(
                    func=lambda *args, n=name, **kwargs: f"Error: Tool '{n}' is unauthorized. All tools are disabled for this agent.",
                    name=name,
                    description=f"Unauthorized placeholder."
                ))
                
            tools.extend(dummy_tools)
            final_tools = tools
            
            # Intercept the tool-binding interface to hide restricted tools from the LLM's system prompt
            class RestrictedModelWrapper:
                def __init__(self, model, allowed_names):
                    self.model = model
                    self.allowed_names = allowed_names
                def bind_tools(self, tools, **kwargs):
                    allowed_bind_tools = [t for t in tools if getattr(t, "name", t) in self.allowed_names]
                    return self.model.bind_tools(allowed_bind_tools, **kwargs)
                def __getattr__(self, name):
                    return getattr(self.model, name)
                    
            llm = RestrictedModelWrapper(llm, allowed_names)
            
        else:
            disabled_tools = getattr(agent_config, "disabled_tools", ["visual_computer_operation"])
            def is_tool_disabled(t_name: str) -> bool:
                if t_name in disabled_tools:
                    return True
                computer_tools = {"take_screenshot", "click_at_current_location", "move_cursor_absolute", "move_cursor_relative", "send_scroll", "inject_keyboard_input"}
                if t_name in computer_tools and "visual_computer_operation" in disabled_tools:
                    return True
                return False

            raw_tools = [list_files, search_web, perform_research, manage_agenda, update_core_memory, save_skill, read_skill, distill_journey, delete_passive_skill, list_skills, mark_quagmire, get_user_clarification, search_episodic_memory, retrieve_episodic_memory, prepare_active_skill, finalize_active_skill, manage_active_skill, fix_active_skill]
            raw_tools.extend(load_dynamic_tools(agent_config.name))

            high_priv_map = {
                "read_file": read_file,
                "curl_url": curl_url,
                "save_file": save_file,
                "edit_file": edit_file,
                "dispatch_subagent": dispatch_subagent,
                "run_terminal_command": run_terminal_command,
                "take_screenshot": take_screenshot,        
                "click_at_current_location": click_at_current_location,
                "move_cursor_absolute": move_cursor_absolute, 
                "move_cursor_relative": move_cursor_relative,       
                "send_scroll": send_scroll,                    
                "inject_keyboard_input": inject_keyboard_input
            }
            
            from langchain_core.tools import StructuredTool

            def make_wrapped_tool(t_obj):
                def wrapped_func(*args, **kwargs):
                    agent_name = agent_config.name
                    confirmed = self.confirm_tool_execution(t_obj.name, kwargs, agent_name=agent_name)
                    if not confirmed:
                        return f"Error: Tool execution of '{t_obj.name}' was rejected by the user."
                    return t_obj.func(*args, **kwargs)
                return StructuredTool(
                    name=t_obj.name,
                    description=t_obj.description,
                    args_schema=t_obj.args_schema,
                    func=wrapped_func
                )

            if self.agent_mode == "EXECUTE":
                for tname, tool_obj in high_priv_map.items():
                    raw_tools.append(tool_obj)
            elif self.agent_mode == "INTERMEDIATE":
                for tname, tool_obj in high_priv_map.items():
                    raw_tools.append(make_wrapped_tool(tool_obj))
            else: # PLAN (SAFE) Mode
                for tname in agent_config.enabled_tools:
                    if tname == "visual_computer_operation":
                        for ct in ["take_screenshot", "click_at_current_location", "move_cursor_absolute", "move_cursor_relative", "send_scroll", "inject_keyboard_input"]:
                            raw_tools.append(make_wrapped_tool(high_priv_map[ct]))
                    elif tname in high_priv_map:
                        raw_tools.append(make_wrapped_tool(high_priv_map[tname]))
            
            # Enforce hard disablers
            final_tools = []
            allowed_names = set()
            for t_obj in raw_tools:
                if is_tool_disabled(t_obj.name):
                    dummy = StructuredTool.from_function(
                        func=lambda *args, name=t_obj.name, **kwargs: f"Error: Tool '{name}' is UNAUTHORIZED for this agent. You are forbidden from using it.",
                        name=t_obj.name,
                        description="Unauthorized placeholder."
                    )
                    final_tools.append(dummy)
                else:
                    final_tools.append(t_obj)
                    allowed_names.add(t_obj.name)

            class RestrictedModelWrapper:
                def __init__(self, model, allowed_names):
                    self.model = model
                    self.allowed_names = allowed_names
                def bind_tools(self, tools, **kwargs):
                    allowed_bind_tools = [t for t in tools if getattr(t, "name", t) in self.allowed_names]
                    return self.model.bind_tools(allowed_bind_tools, **kwargs)
                def __getattr__(self, name):
                    return getattr(self.model, name)
                    
            llm = RestrictedModelWrapper(llm, allowed_names)

        executor = create_react_agent(llm, final_tools, checkpointer=shared_memory)
        self.agent_executors[agent_config.name] = executor
        return executor
    
    def translate_team_backstories(self, host_agent: AgentConfig, all_agents: List[AgentConfig]):
        from toolbox import get_storage_path
        import json
        
        # Enforce strict sequential execution during team/room parallel task dispatches
        with _BACKSTORY_LOCK:
            cache_path = get_storage_path("agents", "translated_backstories.json")
            cache = {}
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cache = json.load(f)
                except Exception:
                    pass

            updated = False
            for a in all_agents:
                if a.name == host_agent.name:
                    continue
                
                cached_data = cache.get(a.name, {})
                if cached_data.get("original") == a.backstory and cached_data.get("translated"):
                    continue

                try:
                    from langchain_openai import ChatOpenAI
                    from langchain_core.messages import HumanMessage
                    from toolbox import resilient_invoke
                    
                    api_key = host_agent.get_api_key()
                    if api_key:
                        self.log_to_ui(f"[dim]Translating backstory for {a.name} to 3rd person...[/dim]")
                        llm = ChatOpenAI(
                            model=host_agent.model,
                            api_key=api_key,
                            base_url=host_agent.base_url,
                            temperature=0,
                            max_retries=1,
                            model_kwargs={"reasoning_effort": getattr(host_agent, "reasoning_effort", "high")}
                        )
                        
                        pronoun_val = getattr(a, "pronouns", "neither")
                        pronoun_instruction = f" Use '{pronoun_val}' pronouns when referring to this agent." if pronoun_val != "neither" else " Use gender-neutral pronouns (they/them) when referring to this agent."
                        prompt = f"Convert the following AI agent backstory from 1st/2nd person to 3rd person. Start with '{a.name} is...'.{pronoun_instruction} Only return the converted backstory, nothing else.\n\nOriginal: {a.backstory}"
                        res = resilient_invoke(llm, [HumanMessage(content=prompt)])
                        translated = res.content.strip()
                        if translated:
                            cache[a.name] = {
                                "original": a.backstory,
                                "translated": translated
                            }
                            updated = True
                except Exception as e:
                    self.log_to_ui(f"[dim red]Translation failed for {a.name}: {e}[/dim red]")

            if updated:
                try:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(cache, f, indent=4)
                except Exception:
                    pass
    
    @on(ChatInput.AbortRequest)
    def on_abort_request(self, event: ChatInput.AbortRequest):
        self.action_abort()

    @on(Input.Submitted, "#ai_chat_input")
    @on(ChatInput.Submitted)
    def on_input_submitted(self, event: ChatInput.Submitted):
        prompt = event.value
        if not prompt.strip(): return
        event.input.text = "" # Clears the TextArea
        
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

        # Multi-Agent Routing
        acting_agent = self.active_agent
        clean_prompt = prompt
        is_team = False
        
        # --- INTERRUPT SYSTEM ---
        is_interrupt = False
        if self._running_agents:
            self.action_abort()
            is_interrupt = True
        
        self.current_batch_id += 1
        batch_id = self.current_batch_id
        # ------------------------

        # Handle @team or @room command
        if prompt.strip().lower().startswith("@team") or prompt.strip().lower().startswith("@room"):
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
                # Ensure everyone is initialized and synced BEFORE broadcasting
                all_agents_list = list(self.agent_manager.agents.values())
                for agent in all_agents_list:
                    self.session_manager.init_agent_session(agent, all_agents_list)
                    self.session_manager.join_conversation(self.active_agent.name, agent, all_agents_list)
                acting_agents = list(self.agent_manager.agents.values())
            else:
                # @room: Only agents already in the active_sessions
                active_names = list(self.session_manager.active_sessions.keys())
                acting_agents = []
                all_agents_list = list(self.agent_manager.agents.values())
                for name in active_names:
                    agent = self.agent_manager.get_agent(name)
                    if agent:
                        # Standard sync for consistency
                        self.session_manager.join_conversation(self.active_agent.name, agent, all_agents_list)
                        acting_agents.append(agent)
            
            # Reset turn queue for team/room parallel turns
            with self.turn_lock:
                self.turn_queue = []

        else:
            # Handle sequential @ mentions
            mentions = self.agent_manager.get_mentions(prompt)
            clean_prompt = prompt
            if is_interrupt:
                clean_prompt = f"the User interrupted to say this: {clean_prompt}"

            if mentions:
                target_agents = []
                for name in mentions:
                    agent = self.agent_manager.get_agent(name)
                    if agent:
                        target_agents.append(agent)
                
                if target_agents:
                    first_agent = target_agents[0]
                    with self.turn_lock:
                        self.turn_queue = target_agents[1:]
                    
                    acting_agents = [first_agent]
                    # Ensure first agent session is ready
                    self.session_manager.join_conversation(self.active_agent.name, first_agent, list(self.agent_manager.agents.values()))
                else:
                    # Fallback to active agent if mentions failed
                    acting_agents = [self.active_agent]
                    self.session_manager.init_agent_session(self.active_agent, list(self.agent_manager.agents.values()))
            else:
                # Default active agent
                acting_agents = [self.active_agent]
                self.session_manager.init_agent_session(self.active_agent, list(self.agent_manager.agents.values()))
                with self.turn_lock:
                    self.turn_queue = []

        self._write_log(Rule(style="dim"))
        if is_team:
            self._write_log("[bold blue]User (to Team):[/bold blue]", is_markdown=False)
        elif 'is_room' in locals() and is_room:
            self._write_log("[bold blue]User (to Room):[/bold blue]", is_markdown=False)
        elif len(acting_agents) == 1:
            self._write_log(f"[bold blue]User (to {acting_agents[0].name}):[/bold blue]", is_markdown=False)
        else:
            self._write_log("[bold blue]User:[/bold blue]", is_markdown=False)
        
        self._write_log(clean_prompt, is_markdown=True)
        
        # Process & context injection
        time_stamp = f"[Time: {datetime.now().strftime('%H:%M')}]\n"
        processed_prompt = time_stamp + handle_ampersand_commands(clean_prompt, self)
        
        # Broadcast user message to all active agents (now they all definitely have sessions)
        self.session_manager.broadcast_message("User", processed_prompt, is_ai=False)
        self.update_tokens()
        # Reset the global abort flag before starting tasks
        toolbox.ABORT_EVENT.clear()
        
        for agent in acting_agents:
            self.run_agent_task(agent, processed_prompt, batch_id=batch_id)


    @work(thread=True)
    def run_agent_task(self, agent: AgentConfig, prompt: str, override_thread_id: str = None, batch_id: int = 0):
        toolbox.register_thread()
        if agent.name in self._running_agents:
            self.log_to_ui(f"[dim yellow]Agent {agent.name} is already working on a task.[/dim yellow]")
            toolbox.unregister_thread()
            return

        self._running_agents.add(agent.name)
        try:
            # Synchronously translate team backstories on this background worker thread
            try:
                self.translate_team_backstories(agent, list(self.agent_manager.agents.values()))
            except Exception:
                pass

            # Update the system prompt in active session history with the latest translated backstories
            try:
                history = self.session_manager.active_sessions.get(agent.name, [])
                if history and history[0].role == "system":
                    history[0].content = agent.get_full_system_prompt(list(self.agent_manager.agents.values()))
            except Exception:
                pass

            executor = self.get_executor(agent)
            if not executor:
                self.log_to_ui(f"[bold red]Agent {agent.name} not configured (Key missing).[/bold red]")
                return
            
            toolbox.thread_context.agent_name = agent.name    
            toolbox.thread_context.batch_id = batch_id
            self.app.call_from_thread(self._toggle_spinner, True, agent.name, agent.color)
            # --- NEW: Prepare the TTS voice stream for this specific agent ---
            if getattr(self, "tts_enabled", False):
                self.tts_manager.start_stream(voice=agent.tts_voice, agent_name=agent.name)
            # Use a unique thread_id per agent/session
            thread_id = override_thread_id or f"{self.session_manager.current_session_id}_{agent.name}"
            run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 5000}

            current_ai_text = ""
            full_ai_response = ""
            current_ai_widget = None
            tool_outputs = []
            tool_calls = []

            try:
                # Check if we have an existing state for this thread in the checkpointer
                state = executor.get_state(run_config)

                import base64
                import mimetypes

                def _format_vision_content(text: str, is_vision: bool):
                    if isinstance(text, list):
                        return text
                    if not isinstance(text, str):
                        return text
                    if not is_vision or "[Attached Image:" not in text:
                        return text
                    
                    parts = re.split(r'\[Attached Image: (.*?)\]', text)
                    if len(parts) == 1: return text
                    
                    content_list = []
                    for i, part in enumerate(parts):
                        if i % 2 == 0:
                            if part.strip(): content_list.append({"type": "text", "text": part.strip()})
                        else:
                            file_path = part.strip()
                            try:
                                mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
                                if file_path.lower().endswith(".pdf"):
                                    try:
                                        import pypdfium2 as pdfium
                                        import io
                                        
                                        doc = pdfium.PdfDocument(file_path)
                                        dpi_val = getattr(self, "pdf_dpi", None) or 150
                                        # Convert DPI to scale value relative to 72 points per inch standard
                                        scale_val = dpi_val / 72.0
                                        
                                        for page in doc:
                                            bitmap = page.render(scale=scale_val)
                                            pil_img = bitmap.to_pil()
                                            
                                            buffered = io.BytesIO()
                                            pil_img.save(buffered, format="PNG")
                                            b64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                                            content_list.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                                    except Exception as pdf_e:
                                        # --- SEAMLESS FALLBACK: Try pure-python text extraction via pypdf ---
                                        try:
                                            from pypdf import PdfReader
                                            reader = PdfReader(file_path)
                                            text_accum = []
                                            for idx_p, page in enumerate(reader.pages):
                                                page_text = page.extract_text() or ""
                                                text_accum.append(f"--- PDF Page {idx_p+1} ---\n{page_text}")
                                            full_text = "\n\n".join(text_accum).strip()
                                            if full_text:
                                                content_list.append({"type": "text", "text": f"[Visual conversion failed, fell back to text extraction]:\n\n{full_text}"})
                                            else:
                                                raise ValueError("No text extractable from this PDF.")
                                        except Exception as fallback_e:
                                            content_list.append({"type": "text", "text": f"[PDF processing failed: Visual engine error: {pdf_e}. Text engine error: {fallback_e}. Make sure the PDF is not corrupted.]"})
                                else:
                                    with open(file_path, "rb") as f:
                                        b64 = base64.b64encode(f.read()).decode('utf-8')
                                    content_list.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                            except Exception as e:
                                content_list.append({"type": "text", "text": f"[Failed to load attached image: {file_path} - {e}]"})
                    return content_list

                # Prepare messages from history
                history = self.session_manager.active_sessions.get(agent.name, [])
                langchain_messages = []
                for hm in history:
                    content = hm.content
                    if not agent.is_capable_vision:
                        if "data:image" in content or "data:application/pdf" in content:
                            content = re.sub(r'data:(?:image|application/pdf);base64,[A-Za-z0-9+/=]+', '[Attachment stripped: Agent not vision capable]', content)

                    if hm.role == "system": langchain_messages.append(SystemMessage(content=content))
                    elif hm.role == "human": langchain_messages.append(HumanMessage(content=_format_vision_content(content, agent.is_capable_vision)))
                    elif hm.role == "ai": 
                        langchain_messages.append(AIMessageChunk(content=content, tool_calls=hm.tool_calls or []))
                        if hm.tool_calls:
                            # Self-Heal: Ensure every tool call has a response
                            outputs_by_name = {o.get("name"): o for o in (hm.tool_outputs or [])}
                            for tc in hm.tool_calls:
                                tc_name = tc.get("name")
                                if tc_name in outputs_by_name:
                                    output = outputs_by_name[tc_name]
                                    tool_content = str(output.get("content", ""))
                                    
                                    # 1. Standard plain-text ToolMessage (Satisfies strict API schemas)
                                    langchain_messages.append(ToolMessage(
                                        content=tool_content,
                                        name=tc_name,
                                        tool_call_id=tc.get("id", "unknown")
                                    ))
                                    
                                    # 2. Global Companion Injection: 
                                    # If ANY tool response contains an image, append companion HumanMessages
                                    if "[Attached Image:" in tool_content:
                                        matches = re.finditer(r'\[Attached Image: (.*?)\]', tool_content)
                                        for match in matches:
                                            if agent.is_capable_vision:
                                                filepath = match.group(1).strip()
                                                langchain_messages.append(HumanMessage(
                                                    content=_format_vision_content(f"[Attached Image: {filepath}]", True)
                                                ))
                                else:
                                    # Placeholder for unfinished calls to satisfy LangGraph validation
                                    self.log_to_ui(f"[dim yellow] Healing interrupted tool call: {tc_name}[/dim yellow]")
                                    langchain_messages.append(ToolMessage(
                                        content="[Tool execution was interrupted or cancelled during session transition.]",
                                        name=tc_name,
                                        tool_call_id=tc.get("id", "unknown")
                                    ))
                """
                # ---------------------------------------------------------------------
                # --- OPTIMIZATION PASS: Strip Older Automated Screenshots Only ---
                # ---------------------------------------------------------------------
                # 1. Locate the index of the absolute last HumanMessage containing an automated screenshot
                last_screenshot_msg_idx = -1
                for msg_idx, msg in enumerate(langchain_messages):
                    if isinstance(msg, HumanMessage) and isinstance(msg.content, list):
                        # Determine if this message is an automated screenshot
                        is_screenshot = False
                        if msg_idx < len(history):
                            is_screenshot = "screenshots/screen_" in (history[msg_idx].content or "")
                        else:
                            is_screenshot = True  # Dynamically injected companion message is always a screenshot

                        if is_screenshot and any(block.get("type") == "image_url" for block in msg.content):
                            last_screenshot_msg_idx = msg_idx
                
                # 2. Strip only older automated screenshots, leaving your user-uploaded files fully intact
                if last_screenshot_msg_idx != -1:
                    for msg_idx, msg in enumerate(langchain_messages):
                        if msg_idx < last_screenshot_msg_idx and isinstance(msg, HumanMessage) and isinstance(msg.content, list):
                            # Verify if this older message is indeed an automated screenshot
                            is_screenshot = False
                            if msg_idx < len(history):
                                is_screenshot = "screenshots/screen_" in (history[msg_idx].content or "")
                            else:
                                is_screenshot = True

                            if is_screenshot:
                                for block in msg.content:
                                    if block.get("type") == "image_url":
                                        block.clear()
                                        block.update({
                                            "type": "text",
                                            "text": "[Historical screen state omitted to ensure focus on the latest state]"
                                        })
                # ---------------------------------------------------------------------
                """
                # If no state exists, we must provide the initial input to the graph
                if not state.values:
                    stream_input = {"messages": langchain_messages}
                else:
                    # IMPORTANT: LangGraph checkpointer is independent of SessionManager.
                    # We must sync any "intercom" messages from SessionManager history 
                    # that LangGraph hasn't seen yet.
                    existing_messages = state.values.get("messages", [])

                    # SELF-HEAL CHECKPOINTER: LangGraph's internal state might be invalid
                    # if it contains an AIMessage with tool_calls that has no ToolMessages.
                    if existing_messages:
                        # Collect all tool calls and all responses across the entire history
                        needed_responses = {} # call_id -> tool_name
                        for m in existing_messages:
                            # Register tool calls
                            tcs = getattr(m, "tool_calls", None)
                            if tcs:
                                for tc in tcs:
                                    if "id" in tc:
                                        needed_responses[tc["id"]] = tc.get("name") or "tool"

                            # Remove those that have responses (ToolMessage has tool_call_id)
                            tcid = getattr(m, "tool_call_id", None)
                            if tcid and tcid in needed_responses:
                                del needed_responses[tcid]

                        if needed_responses:
                            self.log_to_ui(f"[dim yellow] Healing {len(needed_responses)} incomplete tool calls in checkpointer...[/dim yellow]")
                            healing_messages = []
                            for tid, tname in needed_responses.items():
                                healing_messages.append(ToolMessage(
                                    content="[Tool execution was interrupted or cancelled during session transition.]",
                                    name=tname,
                                    tool_call_id=tid
                                ))

                            try:
                                # Attempt to patch the existing state
                                executor.update_state(run_config, {"messages": healing_messages})
                                # Refresh state after healing
                                state = executor.get_state(run_config)
                                existing_messages = state.values.get("messages", [])
                            except Exception as he:
                                # If we can't even patch it, we'll hit the reset logic in the main except block
                                self.log_to_ui(f"[dim red]Checkpoint patching failed: {he}[/dim red]")
                                raise he

                    existing_contents = set()
                    for m in existing_messages:
                        existing_contents.add(str(m.content) if isinstance(m.content, list) else m.content)

                    missing_messages = []
                    for m in langchain_messages:
                        if isinstance(m, SystemMessage): continue
                        m_val = str(m.content) if isinstance(m.content, list) else m.content
                        if m_val not in existing_contents:
                            missing_messages.append(m)

                    if missing_messages:
                        # Update state with missing messages first
                        executor.update_state(run_config, {"messages": missing_messages})

                    # Now provide the NEW prompt
                    stream_input = {"messages": [HumanMessage(content=_format_vision_content(prompt, agent.is_capable_vision))]}

                # Stream execution
                consecutive_fail_count = 0
                MAX_CONSECUTIVE_FAILS = 5
                
                while consecutive_fail_count < MAX_CONSECUTIVE_FAILS:
                    try:
                        for event_type, event_data in executor.stream(stream_input, config=run_config, stream_mode=["messages", "updates"]):
                            if toolbox.ABORT_EVENT.is_set() or (batch_id != 0 and (batch_id != self.current_batch_id or batch_id in self.session_manager.aborted_batch_ids)):
                                raise Exception("Operation forcefully aborted or interrupted by user.")

                            if event_type == "messages":
                                chunk, metadata = event_data
                                if metadata.get("langgraph_node") == "agent" and isinstance(chunk, AIMessageChunk) and chunk.content:
                                    text_chunk = str(chunk.content)
                                    current_ai_text += text_chunk
                                    full_ai_response += text_chunk
                                    # --- NEW: Stream text chunk to TTS engine ---
                                    if getattr(self, "tts_enabled", False):
                                        self.tts_manager.stream_text(text_chunk, agent_name=agent.name, voice=agent.tts_voice)
                                    if not current_ai_widget:
                                        self.log_to_ui(Rule(style="dim"))
                                        self.log_to_ui(f"[bold {agent.color}]{agent.name}:[/bold {agent.color}]", is_markdown=False)
                                        current_ai_widget = Static(Markdown(""), classes="chat_msg")
                                        self.app.call_from_thread(self.query_one("#chat_messages").mount, current_ai_widget)
                                        
                                    display_text = render_latex_to_unicode(current_ai_text)
                                    self.app.call_from_thread(current_ai_widget.update, Markdown(display_text))
                                    self.app.call_after_refresh(lambda: self.query_one("#ai_chat_scroll").scroll_end(animate=False))

                            elif event_type == "updates":
                                # Progress Made: Reset consecutive fail counter
                                consecutive_fail_count = 0
                                
                                for node_name, node_data in event_data.items():
                                    messages = node_data.get("messages", [])
                                    if not isinstance(messages, list):
                                        messages = [messages]

                                    if node_name == "agent":
                                        for msg in messages:
                                            if hasattr(msg, 'additional_kwargs') and 'thought' in msg.additional_kwargs:
                                                self.log_to_ui(f"[dim]Thought:[/dim] {escape(msg.additional_kwargs['thought'])}")

                                            # Telegram integration
                                            if getattr(self, "current_telegram_chat_id", None) and current_ai_text.strip():
                                                # --- NEW: Pass agent.tts_voice ---
                                                self.telegram_manager.send_message(
                                                    self.current_telegram_chat_id, 
                                                    current_ai_text.strip(), 
                                                    title=agent.name, 
                                                    voice=agent.tts_voice
                                                )

                                            # Print outgoing tool calls
                                            if hasattr(msg, "tool_calls") and msg.tool_calls:
                                                self.log_to_ui(Rule(style="dim"))
                                                for tc in msg.tool_calls:
                                                    tool_calls.append(tc)
                                                    self.log_to_ui(f"[#808080]Calling Tool: {escape(tc['name'])} with args: {escape(str(tc['args']))}[/#808080]")

                                            # Clean up streaming variables for the next turn
                                            if getattr(self, "tts_enabled", False):
                                                self.tts_manager.flush_stream(agent_name=agent.name, voice=agent.tts_voice)
                                            current_ai_widget = None
                                            current_ai_text = ""

                                    # Print incoming tool results
                                    elif node_name == "tools":
                                        for msg in messages:
                                            tool_name = getattr(msg, 'name', 'tool')
                                            
                                            content_to_save = msg.content
                                            if isinstance(msg.content, list):
                                                reconstructed = ""
                                                for block in msg.content:
                                                    if block.get("type") == "text":
                                                        reconstructed += block.get("text", "")
                                                    elif block.get("type") == "image_url":
                                                        reconstructed += "\n[ImageBase64: <data_transmitted>]\n"
                                                content_to_save = reconstructed
                                                
                                            tool_outputs.append({"name": tool_name, "content": content_to_save})
                                            
                                            if "[Attached Image:" in str(content_to_save):
                                                img_match = re.search(r'\[Attached Image: (.*?)\]', str(content_to_save))
                                                if img_match:
                                                    img_name = os.path.basename(img_match.group(1).strip())
                                                    self.log_to_ui(f"[#808080]Harness: Intercepted companion image `{img_name}` and queued for visual analysis.[/]")
                                            
                                            # Intercept and hide raw search results from the UI to comply with DuckDuckGo terms
                                            if tool_name in ["search_web", "SearchWeb"]:
                                                summary = "[Search results successfully parsed and delivered to active agent context]"
                                            else:
                                                # Clean up any embedded Base64 tags or raw data URIs from the UI printout to keep Textual responsive
                                                summary_clean = str(content_to_save)
                                                summary_clean = re.sub(r'\[ImageBase64:\s*[^\]]+\]', '[ImageBase64: <data_transmitted>]', summary_clean)
                                                summary_clean = re.sub(r'data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=\s]{20,}', '<base64_data_omitted>', summary_clean)
                                                summary = (summary_clean + '...') if len(summary_clean) > 200 else summary_clean
                                            
                                            # Build the boxed widget
                                            box_content = f"[bold]Tool Result ({agent.name}):[/bold]\n{escape(summary)}"
                                            box_widget = Static(Text.from_markup(box_content), classes="tool_result_box", markup=False)
                                            
                                            self.log_to_ui(box_widget)
                        
                        # Detect completely empty responses and inject a retry prompt natively
                        if not full_ai_response.strip() and not tool_outputs and not tool_calls:
                            consecutive_fail_count += 1
                            if consecutive_fail_count < MAX_CONSECUTIVE_FAILS:
                                self.log_to_ui(f"[bold yellow] Agent returned an empty response. Prompting LLM to retry ({consecutive_fail_count}/{MAX_CONSECUTIVE_FAILS})...[/bold yellow]")
                                stream_input = {"messages": [HumanMessage(content="System Guardrail: Your previous response was completely empty. You must provide a text response or call a tool.")]}
                                current_ai_text = ""
                                full_ai_response = ""
                                current_ai_widget = None
                                tool_outputs = []
                                tool_calls = []
                                continue
                            else:
                                raise ValueError("Empty response received from API after multiple retries")

                        # Success: exit retry loop
                        break

                    except Exception as stream_e:
                        err_msg = str(stream_e).lower()
                        consecutive_fail_count += 1
                        
                        if ("connection" in err_msg or "reset" in err_msg or "timeout" in err_msg or "429" in err_msg) and consecutive_fail_count < MAX_CONSECUTIVE_FAILS:
                            self.log_to_ui(f"[yellow] Stream interrupted ({escape(str(stream_e))}). Retrying {consecutive_fail_count}/{MAX_CONSECUTIVE_FAILS}...[/yellow]")
                            time.sleep(3)
                            stream_input = None # LangGraph resumes from checkpoint
                            continue
                        raise stream_e

                # Broadcast AI response and tool outputs to others
                if full_ai_response.strip() or tool_outputs or tool_calls:
                    if batch_id != 0 and (batch_id != self.current_batch_id or batch_id in self.session_manager.aborted_batch_ids):
                        return
                    ai_response = full_ai_response.strip()
                    # 1. Save to session manager so others can see it
                    self.session_manager.broadcast_message(agent.name, ai_response, is_ai=True, tool_outputs=tool_outputs, tool_calls=tool_calls)
                    self.app.call_from_thread(self.update_tokens)
                    
                    # Silent Background Session Naming
                    self.trigger_background_naming(prompt, ai_response)
                    
                    # 2. Check for new mentions in AI response and add to queue
                    new_mentions = self.agent_manager.get_mentions(ai_response)
                    with self.turn_lock:
                        for m_name in new_mentions:
                            m_agent = self.agent_manager.get_agent(m_name)
                            # Avoid duplicates in queue and don't re-queue self immediately
                            if m_agent and m_agent.name != agent.name and m_agent not in self.turn_queue:
                                self.turn_queue.append(m_agent)

                # 3. Check queue for the next agent to respond
                next_agent = None
                with self.turn_lock:
                    if self.turn_queue:
                        next_agent = self.turn_queue.pop(0)

                if next_agent:
                    self.log_to_ui(f"[bold cyan]>> Sequential hand-off to {next_agent.name}...[/bold cyan]")
                    self.session_manager.join_conversation(agent.name, next_agent, list(self.agent_manager.agents.values()))
                    self.run_agent_task(next_agent, prompt, batch_id=batch_id)

            except Exception as e:
                error_str = str(e)

                # Silence abort/interrupt errors
                if any(term in error_str.lower() for term in ["aborted", "interrupted"]):
                    return

                # Broad, guaranteed mitigation check for any API validation, schema, or Bad Request (400) errors
                is_schema_or_api_error = any(term in error_str.lower() or term in repr(e).lower() for term in ["400", "invalid", "empty", "badrequest", "toolmessage", "tool_calls", "validation", "argument"])

                if is_schema_or_api_error and "_rst_" not in thread_id:
                    self.log_to_ui("[bold yellow] State Corruption or API Error Detected. Performing Automated Recovery...[/bold yellow]")
                    # Timestamped reset suffix to bypass the broken SQLite thread
                    new_thread_id = f"{thread_id}_rst_{int(time.time())}"
                    # Remove from running agents so it can re-trigger
                    self._running_agents.discard(agent.name)
                    return self.run_agent_task(agent, prompt, override_thread_id=new_thread_id, batch_id=batch_id)

                self.log_to_ui(f"[bold red]Execution Error ({agent.name}):[/bold red] {e}")
        finally:
            self._running_agents.discard(agent.name)
            self.app.call_from_thread(self._toggle_spinner, False, agent.name, agent.color) 
            toolbox.unregister_thread()
               
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
        """Animates all active spinners (research tasks and active agents)."""
        try:
            self.spinner_idx = (self.spinner_idx + 1) % len(self.spinner_chars)
            char = self.spinner_chars[self.spinner_idx]

            # 1. Animate Research Tasks (if container is visible)
            try:
                container = self.query_one("#progress_container")
                if container.styles.display != "none":
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
            
            # 2. Animate Main Thinking Indicator
            try:
                spinner_label = self.query_one("#ai_thinking_spinner")
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
        """Checks the clock and fires off scheduled tasks natively as a Ghost User with catch-up logic."""
        if getattr(self, "shell_mode", False) or not hasattr(self, "schedule_manager") or getattr(self, "_scheduler_prompt_active", False):
            return
            
        now = datetime.now()
        current_date = now.strftime("%Y-%m-%d")
        
        for task in self.schedule_manager.tasks:
            if not task.is_active: continue
            if getattr(task, "snooze_until", 0.0) > time.time(): continue
            
            run_today = False
            last_scheduled = self._get_last_scheduled(task, now)
            if last_scheduled:
                try:
                    last_run_dt = datetime.strptime(task.last_run_date, "%Y-%m-%d") if task.last_run_date else None
                except Exception:
                    last_run_dt = None
                
                if not last_run_dt or last_run_dt.date() < last_scheduled.date():
                    run_today = True

            if run_today:
                if self._running_agents:
                    continue 

                self._scheduler_prompt_active = True

                def handle_action(action: str):
                    self._scheduler_prompt_active = False
                    if action == "defer":
                        task.snooze_until = time.time() + 900.0  # Defer 15 minutes
                        self.notify(f"Scheduled task '{task.prompt[:30]}...' deferred for 15 minutes.", severity="warning")
                    elif action == "skip":
                        task.last_run_date = current_date
                        self.schedule_manager.save()
                        self.notify("Scheduled task skipped.", severity="information")
                    elif action == "run_now":
                        task.last_run_date = current_date
                        self.schedule_manager.save()
                        self.action_clear_all_contexts()
                        self.log_to_ui(Rule(title="[bold yellow] INITIATING AUTOMATED SCHEDULED TASK", style="dim"))
                        
                        full_prompt = f"@{task.agent_name} [Automated Scheduled Task]:\n{task.prompt}"
                        
                        from agent import ChatInput
                        chat_input = self.query_one("#ai_chat_input", ChatInput)
                        
                        event = ChatInput.Submitted(chat_input, full_prompt)
                        self.on_input_submitted(event)

                self.app.push_screen(ScheduledTaskPromptModal(task), handle_action)
                break
    
    def update_tokens(self):
        try:
            # Class-level attributes to prevent main-thread visual blocking
            if not hasattr(self, "_tiktoken_encoding"):
                self._tiktoken_encoding = None
            if not hasattr(self, "_tiktoken_loading"):
                self._tiktoken_loading = False

            history = self.session_manager.active_sessions.get(self.active_agent.name, [])
            text = "".join((m.content or "") + "".join(str(out.get("content", "")) for out in (m.tool_outputs or [])) for m in history)
            count = 0
            
            if HAS_TIKTOKEN:
                if self._tiktoken_encoding is not None:
                    try:
                        count = len(self._tiktoken_encoding.encode(text))
                    except Exception:
                        count = len(text) // 4
                else:
                    # Provide an instant character-approximation fallback for boot sequence
                    count = len(text) // 4
                    if not self._tiktoken_loading:
                        self._tiktoken_loading = True
                        
                        # Load compiled Rust modules in background to prevent I/O or network hangs on UI
                        def bg_load_tiktoken():
                            try:
                                import tiktoken
                                self._tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
                            except Exception:
                                pass
                            finally:
                                self._tiktoken_loading = False
                                # Silently refresh status display upon load completion
                                try:
                                    self.app.call_from_thread(self.update_tokens)
                                except Exception:
                                    pass
                                    
                        import threading
                        threading.Thread(target=bg_load_tiktoken, daemon=True).start()
            else:
                count = len(text) // 4
                
            self.current_tokens = count
            self.update_status_bar()
        except Exception:
            pass

    @work(thread=True)
    def check_for_updates_bg(self, manual: bool = False):
        import urllib.request, json
        from toolbox import load_global_settings, save_global_settings
        
        installed_ver = get_installed_version()
        latest_ver = None
        release_notes = ""
        
        try:
            url = "https://pypi.org/pypi/federate/json"
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

        settings = load_global_settings()
        skipped = settings.get("skipped_version", "")
        if not manual and skipped == latest_ver:
            return

        headers = {'User-Agent': 'Mozilla/5.0 (compatible; FederateApp/1.0)'}
        
        # 1. Fetch release list to build cumulative update notes across all intermediate versions
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

        # 2. Single-tag fallback if full release list lookup fails
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
            release_notes = f"## Release v{latest_ver}\n\nA new update for Federate (**v{latest_ver}**) is available with bug fixes and performance improvements."

        def show_modal():
            def handle_update_result(action: str):
                if action == "restart":
                    self.restart_application()
                elif action == "skip":
                    st = load_global_settings()
                    st["skipped_version"] = latest_ver
                    save_global_settings(st)
                    self.log_to_ui(f"[dim]Skipped update v{latest_ver}. Type /update anytime to update.[/dim]")

            self.app.push_screen(UpdateModal(installed_ver, latest_ver, release_notes), handle_update_result)

        self.app.call_from_thread(show_modal)

    def restart_application(self):
        import os, sys
        try:
            import toolbox
            toolbox.ABORT_EVENT.set()
            toolbox.nuke_all_threads()
        except Exception:
            pass
        print('\033[?25h', end='', flush=True)
        try:
            os.system('cls' if os.name == 'nt' else 'clear')
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def request_clarification(self, options: Optional[List[str]] = None, agent_name: str = "Agent") -> str:
        """Pushes the ClarificationModal and waits for the result."""
        result_event = threading.Event()
        final_result = [""]

        def handle_result(res: str):
            final_result[0] = res or ""
            result_event.set()

        self.app.call_from_thread(self.app.push_screen, ClarificationModal(options, agent_name=agent_name), handle_result)
        
        # Wait for the user to submit
        while not result_event.is_set():
            if toolbox.ABORT_EVENT.is_set():
                return ""
            time.sleep(0.1)
            
        return final_result[0]

    def handle_telegram_input(self, chat_id: int, text: str):
        """Processes incoming Telegram messages exactly like UI chat."""
        def _process():
            try:
                self.current_telegram_chat_id = chat_id
                
                self._write_log(Rule(style="dim"))
                self._write_log(f"[bold blue]Telegram User ({chat_id}):[/bold blue]", is_markdown=False)
                self._write_log(text, is_markdown=True)
                
                # --- INTERRUPT SYSTEM ---
                is_interrupt = False
                if self._running_agents:
                    self.action_abort()
                    is_interrupt = True
                
                self.current_batch_id += 1
                batch_id = self.current_batch_id
                # ------------------------

                # Determine routing and clean prompt
                acting_agent = self.active_agent
                clean_prompt = text
                acting_agents = []
                is_team = False
                is_room = False

                if text.strip().lower().startswith("@team") or text.strip().lower().startswith("@room"):
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
                    match = re.match(r'^@([^\s/]+)\s+(.*)', text)
                    if match:
                        # Strip trailing punctuation so Telegram users can type '@Ron,'
                        target_name = match.group(1).rstrip(",.:!?()[]{}")
                        target_agent = self.agent_manager.get_agent(target_name)
                        if target_agent:
                            acting_agent = target_agent
                            clean_prompt = match.group(2)
                            if is_interrupt:
                                clean_prompt = f"the User interrupted to say this: {clean_prompt}"
                            self.session_manager.join_conversation(self.active_agent.name, acting_agent, list(self.agent_manager.agents.values()))
                        else:
                            if is_interrupt:
                                clean_prompt = f"the User interrupted to say this: {clean_prompt}"
                    else:
                        if is_interrupt:
                            clean_prompt = f"the User interrupted to say this: {clean_prompt}"
                        self.session_manager.init_agent_session(acting_agent, list(self.agent_manager.agents.values()))
                    
                    acting_agents = [acting_agent]

                if not clean_prompt and (is_team or is_room):
                    return

                time_stamp = f"[Today's date is {datetime.now().strftime('%A, %B %d, %Y')} and the time now is {datetime.now().strftime('%H:%M')}]\n"
                processed_prompt = time_stamp + handle_ampersand_commands(clean_prompt, self)
                
                # Broadcast user message
                self.session_manager.broadcast_message(f"Telegram User ({chat_id})", processed_prompt, is_ai=False)
                self.update_tokens()
                # --- START THE TYPING LOOP ---
                self.telegram_manager.start_chat_action(chat_id, "typing")
                
                toolbox.ABORT_EVENT.clear()
                for agent in acting_agents:
                    self.run_agent_task(agent, processed_prompt, batch_id=batch_id)

            except Exception as e:
                self.log_to_ui(f"[bold red]Telegram Input Error:[/bold red] {e}")
                
        self.app.call_from_thread(_process)

    def trigger_background_naming(self, user_prompt: str, agent_response: str):
        session_id = self.session_manager.current_session_id
        name_map = get_session_name_map()
        if session_id in name_map:
            return
            
        agent = self.active_agent
        api_key = agent.get_api_key()
        if not api_key:
            return
            
        try:
            from langchain_openai import ChatOpenAI
            from langchain_core.messages import SystemMessage
            llm = ChatOpenAI(
                model=agent.model,
                api_key=api_key,
                base_url=agent.base_url,
                temperature=0,
                max_retries=1,
                model_kwargs={"reasoning_effort": getattr(agent, "reasoning_effort", "high")}
            )
            naming_prompt = (
                "Based on the following first user query and agent response of a session, "
                "generate a short, descriptive name (3-5 words max, no quotes, no file extensions, "
                "plain text) for this session.\n\n"
                f"User: {user_prompt[:200]}\n"
                f"Agent: {agent_response[:200]}"
            )
            res = llm.invoke([HumanMessage(content=naming_prompt)])
            name = res.content.strip().strip('"').strip("'")
            if name:
                name_map[session_id] = name
                save_session_name_map(name_map)
                self.log_to_ui(f"[bold green]Session Named: {name}[/]")
        except Exception as e:
            self.log_to_ui(f"[bold red]Session Naming Failed: {e}[/]")
            pass

    def handle_stt_input(self, text: str, action: str = "append"):
        """Handles pause-appends, auto-submits, and hotword deletions in the UI."""
        def _process():
            try:
                chat_input = self.query_one("#ai_chat_input", ChatInput)
                current_val = chat_input.text.strip() # CHANGED

                if action == "append":
                    if text:
                        new_val = (current_val + " " + text).strip()
                        chat_input.text = new_val # CHANGED
                        self.stt_append_history.append(text)
                
                elif action == "delete":
                    if self.stt_append_history:
                        last_text = self.stt_append_history.pop()
                        if chat_input.text.endswith(last_text): # CHANGED
                            chat_input.text = chat_input.text[:-len(last_text)].strip() # CHANGED
                        else:
                            chat_input.text = chat_input.text.replace(last_text, "").strip() # CHANGED

                elif action == "submit":
                    if text:
                        chat_input.text = (current_val + " " + text).strip() # CHANGED
                    
                    self.stt_append_history = [] 
                    
                    if chat_input.text.strip(): # CHANGED
                        self.on_input_submitted(ChatInput.Submitted(chat_input, chat_input.text)) # CHANGED

                chat_input.focus()
                
                # Move cursor to end
                lines = chat_input.text.split("\n")
                chat_input.cursor_location = (len(lines)-1, len(lines[-1])) # CHANGED
                
            except Exception as e:
                self.log_to_ui(f"[bold red]STT UI Binding Error:[/bold red] {e}")
                
        self.app.call_from_thread(_process)

    def mount_progress(self, tasks: list[str]):
        """Integrated Progress Mounting."""
        def _mount():
            try:
                # Log to the main chat
                self._write_log(Rule(title="[bold]Deep Research Modules Dispatched[/]", style="dim"))

                # Show the container
                container = self.query_one("#progress_container")
                container.styles.display = "block"

                # GET references to the panes
                left_pane = self.query_one("#progress_left")
                right_log = self.query_one("#progress_right", RichLog)

                # Only clear the inside of the left pane, not the whole dashboard!
                left_pane.query("*").remove()
                right_log.clear()

                from textual.containers import Horizontal
                for t in tasks:
                    # Construct ID (toolbox.py expects 'task_...')
                    safe_id = "task_" + "".join(c if c.isalnum() else "_" for c in t)

                    bar = ProgressBar(total=100, show_eta=False, id=f"prog_{safe_id}")

                    row = Horizontal(
                        Label(self.spinner_chars[0], id=f"spin_{safe_id}", classes="task_spinner"),
                        bar,
                        classes="task_row", id=f"row_{safe_id}"
                    )
                    left_pane.mount(row)

                # Ensure we scroll the main chat to reveal the new dashboard
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
                # 1. Stream the raw log directly to the Right Pane
                log_pane = self.query_one("#progress_right", RichLog)
                log_pane.write(msg)

                # 2. Update the Left Pane progress bar and spinner
                prog = self.query_one(f"#prog_{safe_id}", ProgressBar)
                spin = self.query_one(f"#spin_{safe_id}", Label)

                if percent is not None:
                    prog.update(progress=percent)
                    if percent >= 100:
                        spin.update("") # Stop animating when done

                self.app.call_after_refresh(
                    lambda: self.query_one("#ai_chat_scroll").scroll_end(animate=False)
                )
            except Exception: pass

        self.app.call_from_thread(_update)

    def hide_progress(self):
        """Removes progress bars on completion."""
        def _hide():
            try:
                self.query_one("#progress_container").styles.display = "none"
            except Exception:
                pass
        self.app.call_from_thread(_hide)
    
    def check_onboarding(self):
        """Checks if any agent is configured. If not, prompts for onboarding."""
        settings_path = os.path.join(self.agent_manager.agents_dir, "settings.json")
        is_pristine = not os.path.exists(settings_path)
        if is_pristine or (len(self.agent_manager.agents) == 1 and not self.active_agent.get_api_key()):
            self.call_after_refresh(self.show_onboarding_modal)

    def show_onboarding_modal(self):
        """Displays the minimal onboarding dialog screen."""
        def handle_onboarding(result):
            if result:
                old_name = self.active_agent.name
                
                # Update config fields dynamically with the provided onboarding choices
                new_config = AgentConfig(
                    name=result["name"],
                    backstory=result["backstory"],
                    model=result["model"],
                    base_url=result["base_url"],
                    color="#00FFFF",
                    enabled_tools=["read_file", "curl_url", "save_file", "edit_file", "dispatch_subagent", "run_terminal_command"],
                    reasoning_effort="high",
                    temperature=1.0
                )
                
                if result["name"] != old_name:
                    self.agent_manager.delete_agent(old_name)
                    self.agent_executors.pop(old_name, None)
                
                self.agent_manager.save_agent(new_config)
                self._update_agent_keys(new_config.name, result["api_key"])
                self.agent_manager.set_default_agent_name(new_config.name)
                self.select_agent(new_config.name)
                self.agent_executors.pop(new_config.name, None)
                self.update_status_bar()
                
                # Synchronize the main welcome banner
                new_renderable = get_welcome_banner(self, specific_agent=new_config.name, return_renderable=True)
                banners = self.query(".welcome_banner_box")
                if banners:
                    for banner in banners:
                        banner.update(new_renderable)
                
                self.log_to_ui(f"[bold green]Onboarding complete. Agent '{new_config.name}' is now active and ready.[/bold green]")
                self.notify(f"Agent '{new_config.name}' configured successfully!", severity="success")

        self.app.push_screen(OnboardingModal(), handle_onboarding)

    def _update_agent_keys(self, agent_name: str, primary_key: str):
        """Securely stores credentials to the OS Keyring and sets current session variables."""
        import keyring
        primary_user = f"agent_key_{agent_name.lower().replace(' ', '_')}"
        try:
            if primary_key:
                keyring.set_password("Federate", primary_user, primary_key)
                os.environ[f"AGENT_KEY_{agent_name.upper().replace(' ', '_')}"] = primary_key
        except Exception as e:
            self.notify(f"Keychain Access Failed: Could not save credentials to OS Keyring.\nDetail: {e}", severity="error")