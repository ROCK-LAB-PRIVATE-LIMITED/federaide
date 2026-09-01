#!/usr/bin/env python3
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
    
"""
FEDERaiDE Authenticated HTTP/HTTPS Agent Server
Pure REST API: POST for actions, GET for polling events.
Secured with Bearer Token / Master Password authentication.
"""

import sys
import os
import json
import time
import uuid
import glob
import re
import ssl
import socket
import secrets
import argparse
import threading
import warnings
import subprocess
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from dataclasses import asdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# Suppress LangChain model_kwargs parameter warning
warnings.filterwarnings("ignore", message=".*reasoning_effort.*")
warnings.filterwarnings("ignore", message=".*model_kwargs.*")

def get_local_ip():
    """Detects the primary outbound local network IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # Connect to a public IP (doesn't actually send packets) to determine the routed interface IP
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"

def generate_self_signed_cert(target_dir: str = "."):
    """Generates standard-compliant self-signed CA certificate and private key with SAN IP/DNS entries for Android/iOS/Desktop CA installation."""
    target_dir = os.path.abspath(target_dir or ".")
    os.makedirs(target_dir, exist_ok=True)

    cert_path = os.path.join(target_dir, "server.crt")
    key_path = os.path.join(target_dir, "server.key")
    ca_path = os.path.join(target_dir, "ca.crt")
    pem_path = os.path.join(target_dir, "cert.pem")
    local_ip = get_local_ip()

    openssl_bin = shutil.which("openssl")
    if not openssl_bin:
        print("❌ [Error] 'openssl' command-line tool not found.")
        print("   Please install openssl (e.g., 'pkg install openssl' on Termux, 'apt install openssl' on Debian/Ubuntu).")
        sys.exit(1)

    san_entries = [
        "DNS.1 = localhost",
        "IP.1 = 127.0.0.1",
    ]
    if local_ip and local_ip != "127.0.0.1":
        san_entries.append(f"IP.2 = {local_ip}")

    cnf_content = f"""[req]
distinguished_name = req_distinguished_name
x509_extensions = v3_ca
prompt = no

[req_distinguished_name]
C = US
O = FEDERaiDE
CN = {local_ip if local_ip != '127.0.0.1' else 'localhost'}

[v3_ca]
subjectKeyIdentifier = hash
basicConstraints = critical, CA:TRUE
keyUsage = critical, digitalSignature, keyEncipherment, keyCertSign, cRLSign
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = @alt_names

[alt_names]
{chr(10).join(san_entries)}
"""

    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as cnf_file:
        cnf_file.write(cnf_content)
        cnf_path = cnf_file.name

    try:
        cmd = [
            openssl_bin, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key_path,
            "-out", cert_path,
            "-days", "3650",
            "-config", cnf_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"❌ [Error] OpenSSL certificate generation failed: {res.stderr}")
            sys.exit(1)

        # Write aliases for different OS certificate pickers
        shutil.copy2(cert_path, ca_path)
        shutil.copy2(cert_path, pem_path)
    finally:
        if os.path.exists(cnf_path):
            try:
                os.remove(cnf_path)
            except Exception:
                pass

    print("=================================================================")
    print(" ✅ Self-Signed CA Certificate & Private Key Generated!")
    print(f" 📜 Server Cert  : {cert_path}")
    print(f" 🔑 Private Key  : {key_path}")
    print(f" 🛡️  CA Certificate: {ca_path} (or cert.pem)")
    print(f" 🌐 Bound SANs   : localhost, 127.0.0.1{f', {local_ip}' if local_ip != '127.0.0.1' else ''}")
    print("=================================================================")
    print(" 📱 To install on Android as a trusted CA certificate:")
    print(f"    1. Open Android Settings -> Security -> Encryption & credentials")
    print(f"    2. Tap 'Install a certificate' -> 'CA certificate' -> 'Install anyway'")
    print(f"    3. Select '{ca_path}' (or '{cert_path}' / '{pem_path}')")
    print("=================================================================")
    print(" 🚀 To start fedserve:")
    print(f"    fedserve --cert \"{cert_path}\" --key \"{key_path}\"")
    print("=================================================================")

# --- MODULE RESOLUTION ---
current_dir = os.path.dirname(os.path.abspath(__file__))
for p in [
    current_dir,
    os.path.join(current_dir, "src", "federate"),
    os.path.join(current_dir, "assets", "federate"),
]:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

import toolbox
import agent_core
from orchestration import AgentManager, SessionManager, AgentConfig, HistoryMessage, ScheduleManager
from commands import handle_ampersand_commands, process_shell_command, process_slash_command, SLASH_COMMANDS

# --- MESSAGE QUEUE FOR GET-POLLING ---
class EventBuffer:
    def __init__(self, max_history=1000):
        self.lock = threading.Lock()
        self.messages = []
        self.counter = 0
        self.max_history = max_history

    def push(self, payload):
        with self.lock:
            self.counter += 1
            self.messages.append({"id": self.counter, "payload": payload})
            if len(self.messages) > self.max_history:
                self.messages = self.messages[-self.max_history:]

    def get_after(self, after_id):
        with self.lock:
            return [m for m in self.messages if m["id"] > after_id]

# --- HEADLESS AGENT VIEW ---
def lock_keyring():
    """Explicitly locks EncryptedKeyring backends so credentials cannot be read without re-unlocking."""
    try:
        import keyring
        main_backend = keyring.get_keyring()
        backends = [main_backend]
        if hasattr(main_backend, "backends"):
            backends.extend(main_backend.backends)
        for b in backends:
            if type(b).__name__ == "EncryptedKeyring":
                b.__dict__.pop("keyring_key", None)
    except Exception:
        pass

class HeadlessServerView:
    def __init__(self):
        p = Path.home() / "FederateWorkspace"
        p.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = str(p.absolute())
        try:
            os.chdir(self.workspace_dir)
        except Exception:
            pass

        self.events = EventBuffer()

        class MockDirTree:
            def __init__(self, view): self._view = view
            @property
            def path(self): return self._view.workspace_dir

        class MockApp:
            def __init__(self, view):
                self._view = view
                self.mock_dir_tree = MockDirTree(view)
                self.run_configs = {"python": {"executable": sys.executable, "flags": "-u \"{file}\""}}
            def query_one(self, sel, *a, **kw):
                return self.mock_dir_tree if "dir_tree" in str(sel).lower() else self._view
            def call_from_thread(self, fn, *a, **kw):
                try: return fn(*a, **kw)
                except Exception: return None
            def call_after_refresh(self, fn, *a, **kw):
                try: return fn(*a, **kw)
                except Exception: return None
            def notify(self, msg, *a, **kw):
                self._view.log_to_ui(f"[bold cyan]Notice:[/] {msg}")

        self.app = MockApp(self)
        toolbox.CURRENT_APP = self.app
        toolbox.CURRENT_AGENT_VIEW = self
        toolbox.CURRENT_LOG_CB = self.log_to_ui

        self.agent_manager = AgentManager()
        self.session_manager = SessionManager()
        self.schedule_manager = ScheduleManager()
        self.current_batch_id = 0
        self.current_tokens = 0
        self.pdf_dpi = 150
        self.agent_mode = "PLAN"
        self._running_agents = set()
        self.turn_queue = []
        self.paused_queue = []
        self.turn_lock = threading.Lock()
        self.pending_tool_confirmations = {}
        self.tool_confirmation_results = {}
        self.telegram_manager = None
        self.current_telegram_chat_id = None
        self.last_screenshot_path = None

        # Single-client session tracking & lock state
        self.active_client_token = None
        self.last_client_activity = 0.0
        self.client_connected = False
        self._keyring_prompt_sent = False

        # Lock keyring initially on server boot
        lock_keyring()

        # Start background timers
        threading.Thread(target=self._scheduler_loop, daemon=True).start()
        threading.Thread(target=self._client_watchdog, daemon=True).start()

        default_name = self.agent_manager.get_default_agent_name()
        self.active_agent = self.agent_manager.get_agent(default_name) or list(self.agent_manager.agents.values())[0]

    def set_active_client(self, token: str):
        self.active_client_token = token
        self.last_client_activity = time.time()
        self.client_connected = True
        self._keyring_prompt_sent = False

        with self.events.lock:
            self.events.messages.clear()
            self.events.counter = 0

        # Check keyring lock state once upon initial client connection
        if toolbox.is_keyring_locked():
            print(" 🔐 [SERVER] Client connected: Keyring is locked. Requesting unlock...")
            self.emit({"type": "keyring_unlock_required"})
            self._keyring_prompt_sent = True
        else:
            self.update_status_bar()
            self.emit({
                "type": "init",
                "active_agent": self.active_agent.name,
                "agents": [{"name": a.name, "color": a.color, "model": a.model} for a in self.agent_manager.agents.values()],
                "needs_onboarding": self.is_onboarding_needed()
            })

    def _client_watchdog(self):
        # 30-minute grace period for backgrounding/screen sleep (1800 seconds)
        INACTIVITY_TIMEOUT = 1800.0
        while True:
            time.sleep(10.0)
            try:
                if self.client_connected and self.active_client_token:
                    if time.time() - self.last_client_activity > INACTIVITY_TIMEOUT:
                        print(" 🔒 [SERVER] Client session expired (inactivity > 30m). Locking keyring...")
                        self.client_connected = False
                        self.active_client_token = None
                        self._keyring_prompt_sent = False
                        lock_keyring()
            except Exception:
                pass

    def emit(self, payload):
        self.events.push(payload)

    def log_to_ui(self, msg, is_markdown=False):
        self.emit({"type": "log", "content": str(msg), "is_markdown": is_markdown})

    def write_message_block(self, header, content, color, is_markdown=True, silent=False):
        # Flush any pending AI text buffer before sending next block (e.g. before a tool call)
        self._flush_accumulated_ai()
        clean_header = re.sub(r'\[/?bold.*?\]', '', str(header)).strip()
        print(f" 📨 [SERVER] Message block sent: {clean_header}")
        self.emit({"type": "message_block", "header": header, "content": content, "color": color, "is_markdown": is_markdown, "silent": silent})

    def mount_ai_message_box(self, name, color):
        self._flush_accumulated_ai(name)
        if not hasattr(self, "_accumulated_ai_text"):
            self._accumulated_ai_text = {}
            self._accumulated_ai_color = {}
        self._accumulated_ai_text[name] = ""
        self._accumulated_ai_color[name] = color

    def update_ai_message(self, display_text):
        if not hasattr(self, "_accumulated_ai_text"):
            self._accumulated_ai_text = {}
            self._accumulated_ai_color = {}
        agent_name = self.active_agent.name if hasattr(self, "active_agent") else "Agent"
        self._accumulated_ai_text[agent_name] = display_text

    def _flush_accumulated_ai(self, agent_name=None):
        if not hasattr(self, "_accumulated_ai_text"):
            return
        target_name = agent_name or (self.active_agent.name if hasattr(self, "active_agent") else None)
        if not target_name:
            return
        content = self._accumulated_ai_text.get(target_name, "").strip()
        color = self._accumulated_ai_color.get(target_name, self.active_agent.color if hasattr(self, "active_agent") else "#00FFFF")
        if content:
            self._accumulated_ai_text[target_name] = ""
            clean_header = f"{target_name}:"
            print(f" 📨 [SERVER] Final response message sent: {clean_header}")
            self.emit({
                "type": "message_block",
                "header": f"[bold {color}]{target_name}:[/bold {color}]",
                "content": content,
                "color": color,
                "is_markdown": True,
                "silent": False
            })

    def render_tool_result_box(self, owner, color, summary, silent=False):
        print(f" ⚙️  [SERVER] Tool result sent for {owner}")
        self.emit({"type": "tool_result", "agent": owner, "color": color, "summary": str(summary), "silent": silent})

    def render_tool_error_box(self, owner, color, summary):
        print(f" ⚠️  [SERVER] Tool error sent for {owner}")
        self.emit({"type": "tool_error", "agent": owner, "color": color, "summary": str(summary)})

    def _toggle_spinner(self, show, agent_name="Agent", agent_color="#00FFFF"):
        if not show:
            # Turn ended: flush the completed AI response bubble to the client
            self._flush_accumulated_ai(agent_name)
        state_str = "STARTED" if show else "FINISHED"
        print(f" ⏳ [SERVER] Turn {state_str} ({agent_name})")
        self.emit({"type": "spinner", "show": show, "agent": agent_name, "color": agent_color})

    def render_latex_to_unicode_ext(self, text):
        return text

    def update_status_bar(self):
        self.emit({
            "type": "status_bar",
            "mode": self.agent_mode,
            "agent": self.active_agent.name,
            "agent_color": self.active_agent.color,
            "tokens": self.current_tokens,
            "model": self.active_agent.model,
            "cwd": self.workspace_dir
        })

    def update_tokens(self):
        try:
            history = self.session_manager.active_sessions.get(self.active_agent.name, [])
            text = "".join((m.content or "") + "".join(str(out.get("content", "")) for out in (m.tool_outputs or [])) for m in history)
            self.current_tokens = len(text) // 4
        except Exception:
            self.current_tokens = 0
        self.update_status_bar()

    def ensure_chatgpt_auth_for_agent(self, agent):
        try:
            from chatgpt_auth import is_chatgpt_oauth_agent, has_valid_chatgpt_token
            if is_chatgpt_oauth_agent(agent):
                return has_valid_chatgpt_token()
        except Exception:
            pass
        return True

    def get_executor(self, agent_config):
        return agent_core.get_executor_core(self, agent_config)

    def confirm_tool_execution(self, tool_name, arguments, agent_name="Agent"):
        call_id = str(uuid.uuid4())
        self.pending_tool_confirmations[call_id] = threading.Event()
        self.tool_confirmation_results[call_id] = False
        print(f" ⚠️  [SERVER] Tool authorization requested: {tool_name} ({agent_name}) - waiting for user on phone...")
        self.emit({"type": "confirm_tool", "call_id": call_id, "tool_name": tool_name, "arguments": arguments, "agent_name": agent_name})

        while not self.pending_tool_confirmations[call_id].is_set():
            if toolbox.ABORT_EVENT.is_set(): return False
            time.sleep(0.1)
        res = bool(self.tool_confirmation_results.get(call_id, False))
        print(f" {'✓' if res else '✗'} [SERVER] Tool authorization response for {tool_name}: {'APPROVED' if res else 'REJECTED'}")
        return res

    def request_clarification(self, options=None, agent_name="Agent"):
        call_id = str(uuid.uuid4())
        self.pending_tool_confirmations[call_id] = threading.Event()
        self.tool_confirmation_results[call_id] = ""
        self.emit({"type": "confirm_tool", "call_id": call_id, "tool_name": "get_user_clarification", "agent_name": agent_name, "arguments": {"options": options or []}})

        while not self.pending_tool_confirmations[call_id].is_set():
            if toolbox.ABORT_EVENT.is_set(): return ""
            time.sleep(0.1)
        return str(self.tool_confirmation_results.get(call_id, ""))

    def mount_progress(self, tasks):
        self.emit({"type": "mount_progress", "tasks": tasks})

    def update_progress(self, task_name, percent, log_text):
        self.emit({"type": "update_progress", "task": task_name, "percent": percent, "log": log_text})

    def hide_progress(self):
        self.emit({"type": "hide_progress"})

    def set_directory(self, new_dir: str):
        if os.path.exists(new_dir) and os.path.isdir(new_dir):
            self.workspace_dir = os.path.abspath(new_dir)
            try: os.chdir(self.workspace_dir)
            except Exception: pass
            self.update_status_bar()
            self.log_to_ui(f"[bold green]Workspace changed to:[/] {self.workspace_dir}")

    def select_agent(self, name: str) -> bool:
        agent = self.agent_manager.get_agent(name)
        if agent:
            self.active_agent = agent
            self.update_status_bar()
            self.emit({"type": "agent_selected", "name": agent.name, "color": agent.color, "model": agent.model})
            return True
        return False

    def action_abort(self):
        toolbox.ABORT_EVENT.set()
        if hasattr(self, "current_batch_id"):
            self.session_manager.abort_batch(self.current_batch_id)
        toolbox.nuke_all_threads()
        self._running_agents.clear()
        self.log_to_ui("[bold red]Operation Aborted by User.[/bold red]")

    def action_clear_all_contexts(self):
        self.action_abort()
        self.session_manager.clear_all_contexts()
        self.agent_mode = "PLAN"
        self.current_tokens = 0
        self.emit({"type": "clear_chat"})
        self.log_to_ui("[bold yellow]ALL CONTEXTS CLEARED. Fresh multiagent session started.[/bold yellow]")
        self.update_status_bar()

    def _get_last_scheduled(self, task, now):
        import calendar
        from datetime import timedelta
        try:
            task_date = datetime.strptime(getattr(task, "date_str", ""), "%Y-%m-%d") if getattr(task, "date_str", "") else now
            th, tm = map(int, task.time_str.split(":"))
            anchor = datetime(task_date.year, task_date.month, task_date.day, th, tm)
        except Exception:
            return None

        if anchor > now: return None

        repeat_mode = getattr(task, "repeat", "daily")
        candidate = anchor
        while True:
            if repeat_mode == "daily": nxt = candidate + timedelta(days=1)
            elif repeat_mode == "weekly": nxt = candidate + timedelta(weeks=1)
            elif repeat_mode == "monthly":
                month = candidate.month
                year = candidate.year + (month // 12)
                month = (month % 12) + 1
                max_day = calendar.monthrange(year, month)[1]
                nxt = datetime(year, month, min(task_date.day, max_day), th, tm)
            elif repeat_mode == "annually": nxt = datetime(candidate.year + 1, task_date.month, task_date.day, th, tm)
            else: nxt = candidate + timedelta(days=1)

            if nxt > now: break
            candidate = nxt
        return candidate

    def _scheduler_loop(self):
        while True:
            time.sleep(15)
            try:
                now = datetime.now()
                for task in self.schedule_manager.tasks:
                    if not getattr(task, "is_active", True): continue
                    if getattr(task, "snooze_until", 0.0) > time.time(): continue

                    last_scheduled = self._get_last_scheduled(task, now)
                    if last_scheduled:
                        sched_str = last_scheduled.strftime("%Y-%m-%d %H:%M")
                        if getattr(task, "last_run_date", "") != sched_str:
                            if self._running_agents: continue

                            task.last_run_date = sched_str
                            self.schedule_manager.save()
                            self.action_clear_all_contexts()

                            agent = self.agent_manager.get_agent(task.agent_name) or self.active_agent
                            self.log_to_ui(f"[bold yellow]Executing Scheduled Routine for {agent.name}...[/bold yellow]\n[dim]{task.prompt}[/dim]")
                            full_prompt = f"@{agent.name} [Automated Scheduled Task]:\n{task.prompt}"
                            self.process_input(full_prompt)
            except Exception:
                pass

    def run_agent_task(self, agent, prompt, override_thread_id=None, batch_id=0):
        threading.Thread(target=agent_core.run_agent_task_core, args=(self, agent, prompt, override_thread_id, batch_id), daemon=True).start()

    def process_input(self, prompt):
        if not prompt.strip(): return
        if prompt.startswith("!"):
            cmd = prompt[1:].strip()
            out = process_shell_command(cmd, self)
            self.log_to_ui(f"[bold red]Shell:[/bold red] {cmd}\n{out}")
            return
        if prompt.startswith("/"):
            if prompt.startswith("/schedule"):
                self.handle_schedule_command(prompt)
                return
            process_slash_command(prompt, self)
            return

        is_interrupt = False
        if self._running_agents:
            self.action_abort()
            is_interrupt = True

        self.current_batch_id += 1
        batch_id = self.current_batch_id

        all_agents = list(self.agent_manager.agents.values())
        clean_prompt = prompt
        if is_interrupt: clean_prompt = f"the User interrupted to say this: {clean_prompt}"

        seq_mentions = self.agent_manager.get_mentions(clean_prompt)
        par_mentions = self.agent_manager.get_parallel_mentions(clean_prompt)
        acting_agents = []

        if clean_prompt.strip().lower().startswith("@team"):
            clean_prompt = clean_prompt.strip()[5:].strip()
            for a in all_agents:
                self.session_manager.init_agent_session(a, all_agents)
                self.session_manager.join_conversation(self.active_agent.name, a, all_agents)
            acting_agents = all_agents
        elif clean_prompt.strip().lower().startswith("@room"):
            clean_prompt = clean_prompt.strip()[5:].strip()
            for name in list(self.session_manager.active_sessions.keys()):
                a = self.agent_manager.get_agent(name)
                if a:
                    self.session_manager.join_conversation(self.active_agent.name, a, all_agents)
                    acting_agents.append(a)
        else:
            if seq_mentions:
                target_agents = [self.agent_manager.get_agent(n) for n in seq_mentions if self.agent_manager.get_agent(n)]
                if target_agents:
                    if par_mentions:
                        with self.turn_lock: self.turn_queue = target_agents
                    else:
                        first = target_agents[0]
                        with self.turn_lock: self.turn_queue = target_agents[1:]
                        acting_agents.append(first)
            elif not par_mentions:
                acting_agents.append(self.active_agent)
            for name in par_mentions:
                p_agent = self.agent_manager.get_agent(name)
                if p_agent and p_agent not in acting_agents:
                    acting_agents.append(p_agent)

        if not acting_agents: acting_agents.append(self.active_agent)

        for a in acting_agents:
            self.session_manager.init_agent_session(a, all_agents)
            self.session_manager.join_conversation(self.active_agent.name, a, all_agents)

        time_stamp = f"[Time: {datetime.now().strftime('%H:%M')}]\n"
        processed_prompt = time_stamp + handle_ampersand_commands(clean_prompt, self)

        user_cfg = toolbox.load_global_settings()
        u_name = user_cfg.get("user_name", "User")
        u_color = user_cfg.get("user_color", "#dda0dd")

        self.write_message_block(f"[bold {u_color}]{u_name}:[/bold {u_color}]", clean_prompt, u_color, is_markdown=True)
        self.session_manager.broadcast_message(u_name, processed_prompt, is_ai=False)
        self.update_tokens()

        toolbox.ABORT_EVENT.clear()
        for agent in acting_agents:
            self.run_agent_task(agent, processed_prompt, batch_id=batch_id)

    def is_onboarding_needed(self):
        settings_path = os.path.join(self.agent_manager.agents_dir, "settings.json")
        return not os.path.exists(settings_path) or not self.active_agent.get_api_key()

    def get_suggestions(self, value: str):
        if not value: return
        if value.startswith("/"):
            matches = [{"match": cmd, "desc": "Slash Command"} for cmd in SLASH_COMMANDS if cmd.startswith(value)]
            self.emit({"type": "suggestions", "mode": "command", "matches": matches})
            return
        last_amp = value.rfind("&")
        if last_amp != -1:
            partial_path = value[last_amp + 1:].replace(r"\ ", " ")
            base_dir = self.workspace_dir
            search_pattern = os.path.join(base_dir, partial_path + "*")
            files = glob.glob(search_pattern)
            files.sort()
            matches = []
            for f in files[:8]:
                rel = os.path.relpath(f, base_dir).replace("\\", "/")
                if os.path.isdir(f): rel += "/"
                desc = "Directory" if os.path.isdir(f) else f"File ({os.path.getsize(f)/1024:.1f} KB)"
                matches.append({"match": rel, "desc": desc})
            self.emit({"type": "suggestions", "mode": "file", "prefix": value[:last_amp + 1], "matches": matches})
            return
        last_at = value.rfind("@")
        if last_at != -1:
            partial_name = value[last_at + 1:].lower()
            agent_names = list(self.agent_manager.agents.keys()) + ["team", "room"]
            matches = []
            for name in agent_names:
                if name.lower().startswith(partial_name):
                    agent = self.agent_manager.get_agent(name)
                    desc = agent.backstory[:50] + "..." if agent else "Broadcast mention"
                    matches.append({"match": name, "desc": desc})
            self.emit({"type": "suggestions", "mode": "agent", "prefix": value[:last_at + 1], "matches": matches})
            return
        self.emit({"type": "suggestions", "matches": []})

    def get_agent_data(self, name=None):
        agent = self.agent_manager.get_agent(name) or self.active_agent if (name and isinstance(name, str)) else self.active_agent
        data = asdict(agent)
        # NEVER transmit secrets to client — one-way security rule
        data["api_key"] = ""
        data["backup_api_key"] = ""
        all_tools = ["list_files", "search_web", "perform_research", "render_pdf", "manage_agenda", "read_file", "fetch_url", "save_file", "edit_file", "dispatch_coding_subagent", "run_terminal_command", "visual_computer_operation", "send_file_to_telegram"]
        print(f" 📋 [SERVER] Agent metadata requested: {agent.name} (Secrets sanitized)")
        self.emit({"type": "agent_data", "data": data, "all_tools": all_tools, "all_agent_names": list(self.agent_manager.agents.keys())})

    def save_agent_data(self, fields, is_new=False, old_name=None):
        def _verify_and_save():
            if not fields or not isinstance(fields, dict):
                self.emit({"type": "agent_save_status", "status": "failed", "error": "Invalid field data."})
                return
            name = fields.get("name", "").strip()
            if not name:
                self.emit({"type": "agent_save_status", "status": "failed", "error": "Agent name cannot be empty."})
                return

            fields_copy = dict(fields)
            api_key = fields_copy.pop("api_key", "").strip()
            backup_key = fields_copy.pop("backup_api_key", "").strip()

            # Preserve existing server-side keys if client sent empty string
            target_lookup = old_name or name
            existing_agent = self.agent_manager.get_agent(target_lookup)
            if existing_agent:
                if not api_key:
                    api_key = existing_agent.get_api_key()
                if not backup_key:
                    backup_key = existing_agent.get_backup_api_key()

            try:
                config = AgentConfig(**fields_copy)
            except Exception as ce:
                self.emit({"type": "agent_save_status", "status": "failed", "error": f"Config error: {ce}"})
                return

            try:
                import keyring
                safe_key_name = name.lower().replace(" ", "_")
                if api_key:
                    keyring.set_password("Federate", f"agent_key_{safe_key_name}", api_key)
                    os.environ[f"AGENT_KEY_{name.upper().replace(' ', '_')}"] = api_key
                if backup_key:
                    keyring.set_password("Federate", f"agent_backup_key_{safe_key_name}", backup_key)
                    os.environ[f"AGENT_BACKUP_KEY_{name.upper().replace(' ', '_')}"] = backup_key
            except Exception:
                pass

            self.log_to_ui(f"[dim yellow]Verifying credentials & translating backstory for '{name}'...[/dim yellow]")
            translated, error_msg = agent_core.translate_backstory(config)
            if error_msg:
                self.log_to_ui(f"[bold red]Agent verification failed for '{name}':[/bold red] {error_msg}")
                self.emit({"type": "agent_save_status", "status": "failed", "error": str(error_msg), "name": name})
                return

            try:
                cache_path = toolbox.get_storage_path("agents", "translated_backstories.json")
                cache = {}
                if os.path.exists(cache_path):
                    try:
                        with open(cache_path, "r", encoding="utf-8") as f: cache = json.load(f)
                    except Exception: pass
                cache[name] = {"original": config.backstory, "translated": translated}
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=4)
            except Exception:
                pass

            if not is_new and old_name and old_name != name and old_name in self.agent_manager.agents:
                self.agent_manager.delete_agent(old_name)

            self.agent_manager.save_agent(config)
            self.agent_manager.set_default_agent_name(name)
            self.select_agent(name)
            self.log_to_ui(f"[bold green]Agent '{name}' verified & backstory saved successfully.[/bold green]")

            self.emit({
                "type": "init",
                "active_agent": self.active_agent.name,
                "agents": [{"name": a.name, "color": a.color, "model": a.model} for a in self.agent_manager.agents.values()],
                "needs_onboarding": self.is_onboarding_needed()
            })
            self.emit({"type": "agent_save_status", "status": "success", "name": name})

        threading.Thread(target=_verify_and_save, daemon=True).start()

    def delete_agent(self, name):
        if len(self.agent_manager.agents) <= 1:
            self.log_to_ui("[bold red]Cannot delete the only remaining agent.[/bold red]")
            return
        self.agent_manager.delete_agent(name)
        next_name = list(self.agent_manager.agents.keys())[0]
        self.select_agent(next_name)
        self.emit({
            "type": "init",
            "active_agent": self.active_agent.name,
            "agents": [{"name": a.name, "color": a.color, "model": a.model} for a in self.agent_manager.agents.values()],
            "needs_onboarding": self.is_onboarding_needed()
        })

    def get_global_settings(self):
        settings = toolbox.load_global_settings()
        print(" ⚙️  [SERVER] Global settings requested")
        self.emit({"type": "global_settings_data", "data": settings})

    def save_global_settings(self, settings):
        toolbox.save_global_settings(settings)
        print(" ⚙️  [SERVER] Global settings updated")
        self.log_to_ui("[bold green]Global settings saved.[/bold green]")

    def get_sessions_list(self):
        sessions_dir = toolbox.get_storage_path("sessions")
        files = sorted(glob.glob(os.path.join(sessions_dir, "*.json")), key=os.path.getmtime, reverse=True)
        name_map = agent_core.get_session_name_map()
        results = []
        for f in files:
            base = os.path.basename(f).replace(".json", "")
            parts = base.split("_")
            sess_id = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else ""
            friendly = name_map.get(sess_id, base)
            results.append({"path": f, "name": friendly, "id": sess_id})
        print(f" 📂 [SERVER] Sessions list requested ({len(results)} found)")
        self.emit({"type": "sessions_list_data", "sessions": results})

    def get_schedules_data(self):
        tasks_data = [asdict(t) for t in self.schedule_manager.tasks]
        self.emit({"type": "schedules_data", "tasks": tasks_data})

    def _clean_tool_summary(self, t_name: str, t_content: str) -> str:
        if t_name in ["search_web", "SearchWeb"]:
            return "[Search results successfully parsed and delivered to active agent context]"
        content_str = str(t_content or "")
        if len(content_str) > 500 and "[ImageBase64:" not in content_str:
            return content_str[:500] + '...'
        return content_str

    def load_session_file(self, filepath):
        if not os.path.exists(filepath): return
        try:
            with open(filepath, "r", encoding="utf-8") as f: data = json.load(f)
            base = os.path.basename(filepath).replace(".json", "")
            parts = base.split("_")
            sess_id = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else base
            owner_raw = "_".join(parts[2:]) if len(parts) >= 3 else parts[-1]
            matched_owner = self.agent_manager.get_agent(owner_raw) or self.agent_manager.get_agent(owner_raw.replace("_", " "))
            owner = matched_owner.name if matched_owner else owner_raw

            self.session_manager.current_session_id = sess_id
            valid_msgs = []
            for m in data:
                if isinstance(m, dict):
                    clean_m = {k: v for k, v in m.items() if k in HistoryMessage.__dataclass_fields__}
                    valid_msgs.append(HistoryMessage(**clean_m))
            self.session_manager.active_sessions[owner] = valid_msgs
            self.select_agent(owner)

            user_cfg = toolbox.load_global_settings()
            u_name = user_cfg.get("user_name", "User")
            u_color = user_cfg.get("user_color", "#dda0dd")

            self.emit({"type": "clear_chat"})
            self.log_to_ui(f"[bold green]Restored Session: {sess_id} (agent: {owner})[/bold green]")

            for msg in data:
                if not isinstance(msg, dict): continue
                role = msg.get("role")
                if role == "system": continue
                content = msg.get("content") or ""

                if role == "ai":
                    owner_agent = self.agent_manager.get_agent(owner)
                    color = owner_agent.color if owner_agent else (self.active_agent.color or "#3ddbd9")
                    if content and content.strip():
                        self.write_message_block(f"[bold {color}]{owner}:[/bold {color}]", content, color, is_markdown=True, silent=True)
                    if msg.get("tool_calls"):
                        for tc in msg["tool_calls"]:
                            tc_name = tc.get("name", "tool")
                            tc_args = str(tc.get("args", {}))
                            call_text = f"[#808080]Calling Tool: {tc_name} with args: {tc_args}[/#808080]"
                            self.write_message_block(f"[bold {color}]{owner} (Tool Call):[/bold {color}]", call_text, color, is_markdown=False, silent=True)
                    if msg.get("tool_outputs"):
                        for out in msg["tool_outputs"]:
                            t_name = out.get("name", "tool")
                            t_content = str(out.get("content", ""))
                            summary = self._clean_tool_summary(t_name, t_content)
                            self.render_tool_result_box(owner, color, summary, silent=True)
                elif role == "human":
                    intercom_match = re.search(r'<AGENT_INTERCOM sender="([^"]+)">([\s\S]*?)</AGENT_INTERCOM>', content)
                    tool_match = re.search(r'<AGENT_INTERCOM_TOOL_RESPONSE agent="([^"]+)" tool="([^"]+)"[^>]*>([\s\S]*?)</AGENT_INTERCOM_TOOL_RESPONSE>', content)
                    if intercom_match:
                        sender_label = intercom_match.group(1)
                        synced_agent = self.agent_manager.get_agent(sender_label)
                        color = synced_agent.color if synced_agent else "#3ddbd9"
                        msg_text = intercom_match.group(2).strip()
                        self.write_message_block(f"[bold {color}]{sender_label}:[/bold {color}]", msg_text, color, is_markdown=True, silent=True)
                    elif tool_match:
                        agent_name = tool_match.group(1)
                        tool_name = tool_match.group(2)
                        synced_agent = self.agent_manager.get_agent(agent_name)
                        color = synced_agent.color if synced_agent else "#3ddbd9"
                        tool_output_content = tool_match.group(3).strip()
                        summary = self._clean_tool_summary(tool_name, tool_output_content)
                        self.render_tool_result_box(agent_name, color, summary, silent=True)
                    else:
                        clean_content = re.sub(r'^\s*(?:\[(?:Time|Today\'s date)[^\]]*\]\s*)+', '', content, flags=re.IGNORECASE).strip()
                        self.write_message_block(f"[bold {u_color}]{u_name}:[/bold {u_color}]", clean_content, u_color, is_markdown=True, silent=True)
        except Exception as e:
            self.log_to_ui(f"[bold red]Failed to load session:[/bold red] {e}")

# --- HTTP REQUEST HANDLER (AUTHENTICATED POST & GET) ---
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def create_handler(view: HeadlessServerView, valid_auth_tokens: set, valid_passwords: set):
    class AgentRequestHandler(BaseHTTPRequestHandler):
        def _send_cors_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

        def _is_authenticated(self) -> bool:
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
                if view.active_client_token and token == view.active_client_token:
                    return True
                if not view.active_client_token and (token in valid_auth_tokens):
                    return True
            return False

        def _send_unauthorized(self):
            res = json.dumps({"status": "unauthorized", "message": "Authentication required or token expired."}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(res)

        def do_OPTIONS(self):
            self.send_response(200)
            self._send_cors_headers()
            self.end_headers()

        def do_GET(self):
            parsed_path = self.path.split("?", 1)
            endpoint = parsed_path[0]
            query_str = parsed_path[1] if len(parsed_path) > 1 else ""

            if not self._is_authenticated():
                self._send_unauthorized()
                return

            view.last_client_activity = time.time()
            view.client_connected = True

            if endpoint == "/api/poll":
                # If reconnecting after a disconnect, check keyring lock state once
                if not view._keyring_prompt_sent and toolbox.is_keyring_locked():
                    view.emit({"type": "keyring_unlock_required"})
                    view._keyring_prompt_sent = True

                after_id = 0
                for param in query_str.split("&"):
                    if param.startswith("after="):
                        try: after_id = int(param.split("=")[1])
                        except ValueError: after_id = 0

                msgs = view.events.get_after(after_id)
                body = json.dumps({"messages": msgs}).encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(body)
                return

            if endpoint == "/api/status":
                data = {
                    "active_agent": view.active_agent.name,
                    "mode": view.agent_mode,
                    "agents": [{"name": a.name, "color": a.color, "model": a.model} for a in view.agent_manager.agents.values()],
                    "needs_onboarding": view.is_onboarding_needed()
                }
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)

            if self.path == "/api/auth":
                try:
                    payload = json.loads(post_data.decode("utf-8"))
                    entered_secret = payload.get("token") or payload.get("password") or ""
                    entered_secret = str(entered_secret).strip()

                    auth_success = False
                    if entered_secret in valid_auth_tokens or entered_secret in valid_passwords:
                        auth_success = True
                    elif agent_core.is_master_password_set():
                        session_tok = agent_core.unlock_core(entered_secret)
                        if session_tok:
                            auth_success = True

                    if auth_success:
                        # Single-client enforcement: disconnect previous client & lock keyring
                        lock_keyring()

                        new_session_token = secrets.token_hex(32)
                        view.set_active_client(new_session_token)

                        res = json.dumps({"status": "ok", "token": new_session_token}).encode("utf-8")
                        self.send_response(200)
                    else:
                        res = json.dumps({"status": "unauthorized", "message": "Invalid password or token."}).encode("utf-8")
                        self.send_response(401)

                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(res)))
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(res)
                except Exception as e:
                    res = json.dumps({"status": "error", "message": str(e)}).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(res)))
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(res)
                return

            if not self._is_authenticated():
                self._send_unauthorized()
                return

            if self.path == "/api/action":
                try:
                    payload = json.loads(post_data.decode("utf-8"))
                    self._handle_action(payload)
                    res = json.dumps({"status": "ok"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(res)))
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(res)
                except Exception as e:
                    res = json.dumps({"status": "error", "message": str(e)}).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(res)))
                    self._send_cors_headers()
                    self.end_headers()
                    self.wfile.write(res)
                return

            self.send_response(404)
            self.end_headers()

        def _handle_action(self, data):
            action = data.get("action")
            if action in ("get_status", "ui_ready"):
                if toolbox.is_keyring_locked():
                    view.emit({"type": "keyring_unlock_required"})
                else:
                    view.update_status_bar()
                    view.emit({
                        "type": "init",
                        "active_agent": view.active_agent.name,
                        "agents": [{"name": a.name, "color": a.color, "model": a.model} for a in view.agent_manager.agents.values()],
                        "needs_onboarding": view.is_onboarding_needed()
                    })
            elif action == "keyring_unlock":
                pwd = data.get("password", "").strip()
                if toolbox.unlock_keyring(pwd):
                    print(" 🔓 [SERVER] Keyring unlocked successfully.")
                    view._keyring_prompt_sent = False
                    view.emit({"type": "keyring_unlock_success"})
                    view.update_status_bar()
                    view.emit({
                        "type": "init",
                        "active_agent": view.active_agent.name,
                        "agents": [{"name": a.name, "color": a.color, "model": a.model} for a in view.agent_manager.agents.values()],
                        "needs_onboarding": view.is_onboarding_needed()
                    })
                else:
                    print(" ❌ [SERVER] Keyring unlock failed: incorrect password.")
                    view.emit({"type": "keyring_unlock_failed", "error": "Unlock failed. Password incorrect."})
            elif action == "keyring_reset":
                pwd = data.get("password", "").strip()
                try:
                    import keyring
                    backend = keyring.get_keyring()
                    backends = [backend]
                    if hasattr(backend, "backends"): backends.extend(backend.backends)
                    for b in backends:
                        if type(b).__name__ == "EncryptedKeyring":
                            if hasattr(b, "file_path") and b.file_path and os.path.exists(b.file_path):
                                os.remove(b.file_path)
                            b.__dict__["keyring_key"] = pwd
                    print(" 🔄 [SERVER] Keyring reset and unlocked.")
                    view._keyring_prompt_sent = False
                    view.emit({"type": "keyring_unlock_success"})
                    view.update_status_bar()
                    view.emit({
                        "type": "init",
                        "active_agent": view.active_agent.name,
                        "agents": [{"name": a.name, "color": a.color, "model": a.model} for a in view.agent_manager.agents.values()],
                        "needs_onboarding": view.is_onboarding_needed()
                    })
                except Exception as e:
                    view.emit({"type": "keyring_unlock_failed", "error": f"Reset failed: {e}"})
            elif action in ("save_attachment", "import_shared_file"):
                fname = data.get("filename", "")
                src = data.get("source_path", "").replace("file://", "")
                b64_data = data.get("data", "")
                dest_path = os.path.join(view.workspace_dir, fname)
                if src and os.path.exists(src):
                    import shutil
                    shutil.copy2(src, dest_path)
                elif fname and b64_data:
                    import base64 as b64_lib
                    if "," in b64_data: b64_data = b64_data.split(",", 1)[1]
                    file_bytes = b64_lib.b64decode(b64_data)
                    with open(dest_path, "wb") as f: f.write(file_bytes)
            elif action == "input":
                view.process_input(data.get("text", ""))
            elif action == "abort":
                view.action_abort()
            elif action == "clear_all":
                view.action_clear_all_contexts()
            elif action == "select_agent":
                view.select_agent(data.get("name"))
            elif action == "set_mode":
                mode = data.get("mode")
                if mode in ("PLAN", "INTERMEDIATE", "EXECUTE"):
                    view.agent_mode = mode
                    view.update_status_bar()
                    view.log_to_ui(f"System security permission mode: [bold]{view.agent_mode}[/bold]")
            elif action == "set_directory":
                view.set_directory(data.get("path"))
            elif action == "get_suggestions":
                view.get_suggestions(data.get("value", ""))
            elif action == "get_agent_data":
                view.get_agent_data(data.get("name"))
            elif action == "save_agent_data":
                view.save_agent_data(data.get("fields"), data.get("is_new", False), data.get("old_name"))
            elif action == "delete_agent":
                view.delete_agent(data.get("name"))
            elif action == "get_global_settings":
                view.get_global_settings()
            elif action == "save_global_settings":
                view.save_global_settings(data.get("settings"))
            elif action == "get_sessions":
                view.get_sessions_list()
            elif action == "get_schedules":
                view.get_schedules_data()
            elif action == "delete_schedule":
                t_id = data.get("id")
                if t_id:
                    view.schedule_manager.delete_task(t_id)
                    view.get_schedules_data()
            elif action == "load_session":
                view.load_session_file(data.get("path"))
            elif action == "list_workspace":
                rel_path = data.get("path", "").strip()
                target_dir = os.path.abspath(os.path.join(view.workspace_dir, rel_path))
                if not target_dir.startswith(view.workspace_dir) or not os.path.exists(target_dir):
                    target_dir = view.workspace_dir
                    rel_path = ""
                items = []
                try:
                    for entry in sorted(os.listdir(target_dir)):
                        if entry.startswith("."): continue
                        full_p = os.path.join(target_dir, entry)
                        is_dir = os.path.isdir(full_p)
                        size = 0 if is_dir else os.path.getsize(full_p)
                        items.append({"name": entry, "is_dir": is_dir, "size": size})
                except Exception: pass
                items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
                view.emit({"type": "workspace_list", "current_path": rel_path, "items": items})
            elif action == "prepare_share":
                import zipfile, mimetypes, shutil
                rel_path = data.get("path", "").strip()
                is_folder = data.get("is_folder", False)
                target_path = os.path.abspath(os.path.join(view.workspace_dir, rel_path))
                if not target_path.startswith(view.workspace_dir) or not os.path.exists(target_path): return
                cache_dir = os.path.abspath(os.path.join(os.path.expanduser("~"), ".federate", "cache"))
                os.makedirs(cache_dir, exist_ok=True)
                if is_folder:
                    base_name = os.path.basename(target_path) or "workspace"
                    out_filename = f"{base_name}.zip"
                    dest_path = os.path.join(cache_dir, out_filename)
                    with zipfile.ZipFile(dest_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                        for root, _, files in os.walk(target_path):
                            for f in files:
                                abs_f = os.path.join(root, f)
                                zipf.write(abs_f, os.path.relpath(abs_f, target_path))
                    mime_type = "application/zip"
                else:
                    out_filename = os.path.basename(target_path)
                    dest_path = os.path.join(cache_dir, out_filename)
                    shutil.copy2(target_path, dest_path)
                    mime_type = mimetypes.guess_type(out_filename)[0] or "application/octet-stream"
                view.emit({"type": "share_file_path", "filename": out_filename, "path": dest_path, "mime_type": mime_type})
            elif action == "delete_workspace_item":
                import shutil
                rel_path = data.get("path", "").strip()
                is_folder = data.get("is_folder", False)
                target_path = os.path.abspath(os.path.join(view.workspace_dir, rel_path))
                if target_path == view.workspace_dir or not target_path.startswith(view.workspace_dir + os.sep) or not os.path.exists(target_path):
                    view.log_to_ui(f"[bold red]Cannot delete path: {rel_path}[/]")
                    return
                try:
                    if is_folder: shutil.rmtree(target_path)
                    else: os.remove(target_path)
                    view.log_to_ui(f"[bold green]Deleted:[/] {rel_path}")
                    parent_rel = os.path.dirname(rel_path)
                    self._handle_action({"action": "list_workspace", "path": parent_rel})
                except Exception as e:
                    view.log_to_ui(f"[bold red]Failed to delete:[/] {e}")
            elif action == "clear_workspace_cache":
                cache_dir = os.path.abspath(os.path.join(os.path.expanduser("~"), ".federate", "cache"))
                count = 0
                if os.path.exists(cache_dir):
                    import shutil
                    for entry in os.listdir(cache_dir):
                        full_p = os.path.join(cache_dir, entry)
                        try:
                            if os.path.isdir(full_p): shutil.rmtree(full_p)
                            else: os.remove(full_p)
                            count += 1
                        except Exception: pass
                view.emit({"type": "workspace_cache_cleared", "count": count})
            elif action == "tool_response":
                cid = data.get("call_id")
                appr = data.get("approved")
                view.tool_confirmation_results[cid] = data.get("response", appr)
                if cid in view.pending_tool_confirmations:
                    view.pending_tool_confirmations[cid].set()

        def log_message(self, format, *args):
            pass

    return AgentRequestHandler

# --- SERVER BOOTSTRAP ---
def main(cli_args=None):
    parser = argparse.ArgumentParser(description="FEDERaiDE Authenticated HTTP/HTTPS Agent Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8443, help="Port to listen on (default: 8443)")
    parser.add_argument("--token", default=None, help="Fixed access token for API authentication")
    parser.add_argument("--password", default=None, help="Password for API authentication")
    parser.add_argument("--cert", default=None, help="Path to SSL certificate (.pem or .crt)")
    parser.add_argument("--key", default=None, help="Path to SSL private key (.key)")
    parser.add_argument("--create", nargs="?", const=".", default=None, metavar="DIR", help="Generate valid self-signed server.crt and server.key with SAN IP entries and exit")
    args = parser.parse_args(cli_args)

    if args.create is not None:
        generate_self_signed_cert(args.create)
        sys.exit(0)

    valid_tokens = set()
    valid_passwords = set()

    if args.token:
        valid_tokens.add(args.token.strip())
    if args.password:
        valid_passwords.add(args.password.strip())

    # If no token or password was passed, auto-generate a secure token
    auto_generated_token = None
    if not valid_tokens and not valid_passwords:
        auto_generated_token = secrets.token_hex(24)
        valid_tokens.add(auto_generated_token)

    # Resolve certificate paths to absolute paths before HeadlessServerView changes cwd
    cert_path = os.path.abspath(args.cert) if args.cert else None
    key_path = os.path.abspath(args.key) if args.key else None

    view = HeadlessServerView()
    handler_class = create_handler(view, valid_tokens, valid_passwords)
    server = ThreadedHTTPServer((args.host, args.port), handler_class)

    protocol = "http"
    if cert_path and key_path:
        if not os.path.exists(cert_path):
            print(f"⚠️  [SSL Error] Certificate file not found at: {cert_path}")
        elif not os.path.exists(key_path):
            print(f"⚠️  [SSL Error] Key file not found at: {key_path}")
        else:
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
                server.socket = ctx.wrap_socket(server.socket, server_side=True)
                protocol = "https"
            except Exception as e:
                print(f"⚠️  [SSL Error] Failed to enable HTTPS: {e}")

    local_ip = get_local_ip()

    print("=================================================================")
    print(f" 🚀 FEDERaiDE Authenticated Agent Server Live ({protocol.upper()})")
    if args.host == "0.0.0.0":
        print(f" 📱 Mobile URL : {protocol}://{local_ip}:{args.port}")
        print(f" 💻 Local URL  : {protocol}://127.0.0.1:{args.port}")
    else:
        print(f" 🌐 URL        : {protocol}://{args.host}:{args.port}")
    
    if auto_generated_token:
        print(f" 🔑 Auth Token : {auto_generated_token}")
        print(" (Enter the Mobile URL and Auth Token in your phone's app)")
    elif args.token:
        print(f" 🔑 Auth Token : {args.token}")
    elif args.password:
        print(" 🔑 Password   : Server Password Authentication Active")
    print("=================================================================")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping agent server...")
        server.server_close()

if __name__ == "__main__":
    main()