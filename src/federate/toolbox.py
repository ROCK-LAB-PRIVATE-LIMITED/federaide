import os
import sys
import platform
import subprocess
import requests
import fnmatch
import json
import concurrent.futures
import re
import threading
import datetime
import uuid
import io
import base64
import traceback
import json
import tiktoken
import time
import random
from pathlib import Path
from bs4 import BeautifulSoup
from markdownify import markdownify as md

import ctypes

_LAST_LLM_CALL_TIME = 0.0
_LLM_LOCK = threading.Lock()

# --- THE HAMMER: GLOBAL THREAD KILLER ---
_ACTIVE_THREADS = set()
_THREAD_LOCK = threading.Lock()

def register_thread():
    with _THREAD_LOCK:
        _ACTIVE_THREADS.add(threading.get_ident())

def unregister_thread():
    with _THREAD_LOCK:
        _ACTIVE_THREADS.discard(threading.get_ident())

def nuke_all_threads():
    """Injects a fatal SystemExit directly into the bytecode of all registered zombie threads."""
    with _THREAD_LOCK:
        current_id = threading.get_ident()
        for t_id in list(_ACTIVE_THREADS):
            if t_id == current_id:
                continue # Don't commit seppuku on the main UI thread
            try:
                # Forcefully throw a SystemExit inside the target thread's execution context
                res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(t_id), ctypes.py_object(SystemExit))
                if res > 1:
                    # If it failed, revert the effect to prevent memory corruption
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(t_id), 0)
            except Exception:
                pass
        _ACTIVE_THREADS.clear()
# ----------------------------------------

# Persistent Global Settings Helper
GLOBAL_SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".federate", "global_settings.json")

DEFAULT_GLOBAL_SETTINGS = {
    "user_name": "User",
    "user_color": "#dda0dd",
    "skipped_version": "",
    "search_pacing_delay": 65.0,
    "max_search_results": 10,
    "scraper_max_bytes": 1000000,
    "scraper_timeout": 120.0,
    "scraper_max_tokens": 30000,
    "max_api_retries": 20,
    "api_retry_delay": 15.0,
    "quota_retry_delay": 120.0,
    "max_research_agents": 4,
    "research_context_tokens": 28000,
    "research_min_length": 5000,
    "max_shrink_attempts": 15,
    "pdf_dpi": 150,
    "pdf_footer_text": "FEDERATE RESEARCH REPORT",
    "pdf_body_font": "Space Grotesk",
    "pdf_header_font": "Michroma",
    "pdf_code_font": "Space Mono",
    "pdf_body_font_size": "11pt",
    "pdf_h1_font_size": "28pt",
    "keep_verbatim_count": 2,
    "research_image_system_enabled": False,
    "research_images_max": 10,
    "research_image_retries": 1,
    "research_images_as_links": False,
    "autoupdate_on_launch": False,
    "tool_result_visibility": "private",
}



def load_global_settings() -> dict:
    settings = DEFAULT_GLOBAL_SETTINGS.copy()
    if os.path.exists(GLOBAL_SETTINGS_FILE):
        try:
            with open(GLOBAL_SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings.update(json.load(f))
        except Exception:
            pass
    return settings

def save_global_settings(settings: dict):
    try:
        os.makedirs(os.path.dirname(GLOBAL_SETTINGS_FILE), exist_ok=True)
        with open(GLOBAL_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4)
    except Exception:
        pass

_DYNAMIC_PACING_DELAY = float(load_global_settings()["search_pacing_delay"])

# Persistent search pacing state file (RFC 1918 & cross-restart safety)
_SEARCH_STATE_PATH = os.path.join(str(Path.home()), ".federate", "search_state.json")

def _load_last_search_time() -> float:
    try:
        if os.path.exists(_SEARCH_STATE_PATH):
            with open(_SEARCH_STATE_PATH, "r", encoding="utf-8") as f:
                return float(json.load(f).get("last_search_time", 0.0))
    except Exception:
        pass
    return 0.0

def _save_last_search_time(t: float):
    try:
        os.makedirs(os.path.dirname(_SEARCH_STATE_PATH), exist_ok=True)
        with open(_SEARCH_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"last_search_time": t}, f)
    except Exception:
        pass

_LAST_SEARCH_TIME = _load_last_search_time()
_SEARCH_LOCK = threading.Lock()

def _get_search_delay() -> float:
    """Paces searches globally across all threads, spacing them by 65 +- 9 seconds."""
    global _LAST_SEARCH_TIME
    with _SEARCH_LOCK:
        now = time.time()
        _LAST_SEARCH_TIME = max(_LAST_SEARCH_TIME, _load_last_search_time())
        pacing_base = load_global_settings().get("search_pacing_delay", 65.0)
        jitter = random.uniform(-9.0, 9.0) if pacing_base > 9.0 else 0.0
        pacing_target = pacing_base + jitter
        
        if now - _LAST_SEARCH_TIME > pacing_target:
            _LAST_SEARCH_TIME = now - pacing_target
            
        elapsed = now - _LAST_SEARCH_TIME
        if elapsed < pacing_target:
            sleep_needed = pacing_target - elapsed
        else:
            sleep_needed = 0.0
            
        _LAST_SEARCH_TIME = now + sleep_needed # No cap any more
        # Persist to disk so it survives app crashes and restarts
        _save_last_search_time(_LAST_SEARCH_TIME)
    return sleep_needed
    
from markdown import markdown
from fake_useragent import UserAgent
from ddgs import DDGS
from PIL import Image

from langchain_core.tools import tool, StructuredTool
from pydantic import BaseModel, Field, create_model
from typing import TypedDict, Annotated, Dict, List, Optional, Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, ToolMessage, AIMessage, BaseMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_core.runnables import RunnableConfig

try:
    from weasyprint import HTML, CSS
    WEASYPRINT_AVAILABLE = True
except ImportError:
    WEASYPRINT_AVAILABLE = False


import sqlite3
import time
from langgraph.checkpoint.sqlite import SqliteSaver
from rich.markup import escape
# Global references so tools can communicate with the active Textual app
CURRENT_APP = None
CURRENT_AGENT_VIEW = None
CURRENT_LOG_CB = None
ABORT_EVENT = threading.Event()



# --- GLOBAL SYSTEM STORAGE REDIRECTION ---
# Redirects all internal state/memories outside the code folder to your safe Home Directory.

from pathlib import Path

# --- GLOBAL SYSTEM STORAGE REDIRECTION ---
# Redirects all internal state/memories outside the code folder to your safe Home Directory.
FEDERATE_DIR = os.path.join(str(Path.home()), ".federate")

DEFAULT_VENV_PATH = os.path.join(FEDERATE_DIR, "defaultVenv")

def _bootstrap_scratchpad_venv():
    """Silently creates a default scratchpad virtual environment in the background if missing."""
    try:
        if not os.path.exists(DEFAULT_VENV_PATH):
            # Use the host system's running Python executable to guarantee version matching
            subprocess.run(
                [sys.executable, "-m", "venv", DEFAULT_VENV_PATH], 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL, 
                check=True
            )
    except Exception:
        pass

# Run bootstrapping on a daemon thread to keep UI startup instant and non-blocking.
# Since the function is now defined above, this resolves the NameError.
threading.Thread(target=_bootstrap_scratchpad_venv, daemon=True).start()

def get_storage_path(*args):
    """Explicitly builds a path inside FEDERATE_DIR for 'agents' or 'sessions'."""
    if args and args[0] in ["agents", "sessions"]:
        return os.path.join(FEDERATE_DIR, *args)
    return os.path.join(*args)

def get_locked_keyring():
    """
    Detects if an EncryptedKeyring (from keyrings.alt) is currently active and locked.
    This type of keyring requires a manual password and blocks the UI if accessed while locked.
    Native backends (macOS Keychain, Windows Credential Manager) do not use this class
    and will not trigger this detection.
    """
    try:
        import keyring
        try:
            from keyrings.alt.file import EncryptedKeyring
        except ImportError:
            # If the library that provides the manual encrypted file isn't installed, 
            # we are likely using a native OS keychain which won't block the UI.
            return None
            
        backend = keyring.get_keyring()
        
        # Check the primary backend and any backends it might be chaining
        backends_to_check = [backend]
        if hasattr(backend, 'backends'):
            backends_to_check.extend(backend.backends)
            
        for b in backends_to_check:
            # We check the class type. EncryptedKeyring is the specific one that blocks.
            if isinstance(b, EncryptedKeyring):
                # If 'keyring_key' is NOT in __dict__, the backend is locked/uninitialized
                # and will trigger a terminal-blocking getpass() call upon use.
                if 'keyring_key' not in b.__dict__:
                    return b
    except Exception:
        pass
    return None

def is_keyring_locked():
    return get_locked_keyring() is not None

def unlock_keyring(password: str) -> bool:
    """Attempts to unlock all EncryptedKeyring backends with the given password."""
    try:
        import keyring
        main_backend = keyring.get_keyring()
        backends = [main_backend]
        if hasattr(main_backend, 'backends'):
            backends.extend(main_backend.backends)
            
        unlocked_any = False
        for b in backends:
            if type(b).__name__ == "EncryptedKeyring":
                # Set on __dict__ to bypass property and set the attribute
                b.__dict__['keyring_key'] = password
                unlocked_any = True
        return unlocked_any
    except Exception:
        return False

def check_persistent_abort():
    """Checks if the current batch ID has been permanently aborted."""
    if CURRENT_APP:
        try:
            agent_view = CURRENT_APP.query_one("#ai_agent_view")
            task_batch = getattr(thread_context, "batch_id", 0)
            if task_batch != 0 and hasattr(agent_view.session_manager, "aborted_batch_ids"):
                if task_batch in agent_view.session_manager.aborted_batch_ids:
                    raise Exception("Task was permanently aborted by user.")
        except Exception as e:
            if "aborted" in str(e).lower() or "interrupted" in str(e).lower():
                raise e

def check_abort():
    """Checks if the current task should stop immediately."""
    if ABORT_EVENT.is_set():
        raise Exception("Operation forcefully aborted by user.")
    
    check_persistent_abort()
    
    # Check if a new batch of tasks has started (Interrupt System)
    if CURRENT_APP:
        try:
            agent_view = CURRENT_APP.query_one("#ai_agent_view")
            current_batch = getattr(agent_view, "current_batch_id", 0)
            task_batch = getattr(thread_context, "batch_id", 0)
            if task_batch != 0 and task_batch != current_batch:
                raise Exception("Task interrupted by a newer user request.")
        except Exception as e:
            if "interrupted" in str(e).lower() or "aborted" in str(e).lower():
                raise e

DB_PATH = get_storage_path(str(Path.home()), ".federate", ".federate_state.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
shared_db_conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=60.0)
shared_db_conn.execute("PRAGMA journal_mode=WAL;")  # Allows simultaneous reading & writing
shared_memory = SqliteSaver(shared_db_conn)
shared_memory.setup() # Automatically creates the required SQL tables

def purge_db_checkpoints():
    """Purges ephemeral LangGraph checkpoints and vacuums the database on clean exit."""
    try:
        cursor = shared_db_conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = [row[0] for row in cursor.fetchall()]
        
        # 1. Delete all rows
        for table in tables:
            cursor.execute(f"DELETE FROM {table};")
            
        # 2. MUST COMMIT DELETION FIRST (SQLite forbids VACUUM inside an open transaction)
        shared_db_conn.commit()
        
        # 3. Truncate WAL & VACUUM (now outside transaction)
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        cursor.execute("VACUUM;")
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        shared_db_conn.commit()
        
        print("\n[FEDERaiDE] Checkpoint database purged and vacuumed on exit.\n")
    except Exception as e:
        print(f"\n[FEDERaiDE] Checkpoint purge error: {e}\n")

def _get_agent(config: RunnableConfig) -> str:
    """Extracts the agent name natively from the LangGraph session ID or thread context."""
    if hasattr(thread_context, "agent_name") and thread_context.agent_name:
        return thread_context.agent_name
        
    thread_id = config.get("configurable", {}).get("thread_id", "")
    if not thread_id: return "Rita"
    
    # Handle automated recovery thread IDs (e.g., sess_123_Rita_rst_1785142122)
    if thread_id.startswith("sess_"):
        parts = thread_id.split("_", 2)
        if len(parts) >= 3:
            agent_part = parts[2]
            if "_rst_" in agent_part:
                return agent_part.split("_rst_")[0]
            return agent_part
            
    return thread_id.split("_")[-1]

def resilient_invoke(model, messages):
    """Wraps raw LLM calls in a retry loop, but fails fast on fatal API errors."""
    global _LAST_LLM_CALL_TIME, _DYNAMIC_PACING_DELAY
    max_retries = load_global_settings().get("max_api_retries", 20)
    for attempt in range(max_retries): 
        check_abort()
        sleep_needed = 0.0
        
        # Check if we are running a local model (loopback, LAN IP, or mDNS domain) to bypass pacing entirely
        base_url = getattr(model, "base_url", "") or getattr(model, "openai_api_base", "")
        is_local = False
        if base_url:
            base_url_str = str(base_url).lower().strip()
            
            from urllib.parse import urlparse
            import ipaddress
            
            # Ensure a double slash scheme exists so urlparse extracts the hostname correctly
            url_to_parse = base_url_str if "://" in base_url_str else "//" + base_url_str
            try:
                parsed = urlparse(url_to_parse)
                hostname = parsed.hostname
            except Exception:
                hostname = None
                
            if hostname:
                if hostname == "localhost" or hostname.endswith(".local") or hostname == "::1":
                    is_local = True
                else:
                    try:
                        ip = ipaddress.ip_address(hostname)
                        if ip.is_private or ip.is_loopback:
                            is_local = True
                    except ValueError:
                        pass
                        
            # Robust fallback for fuzzy string matches (handles raw IPs, custom ports, etc.)
            if not is_local:
                if "localhost" in base_url_str or ".local" in base_url_str or "::1" in base_url_str:
                    is_local = True
                else:
                    clean_ip = base_url_str.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
                    try:
                        ip = ipaddress.ip_address(clean_ip)
                        if ip.is_private or ip.is_loopback:
                            is_local = True
                    except ValueError:
                        if any(clean_ip.startswith(prefix) for prefix in ["192.168.", "10.", "127."]):
                            is_local = True
                        elif clean_ip.startswith("172."):
                            try:
                                second_octet = int(clean_ip.split(".")[1])
                                if 16 <= second_octet <= 31:
                                    is_local = True
                            except (IndexError, ValueError):
                                pass
        
        # Universal Adaptive Pacing
        if _DYNAMIC_PACING_DELAY > 0.0 and not is_local:
            with _LLM_LOCK:
                now = time.time()
                # Apply the current delay with +/- 9s jitter to match the user's streamlined queue specs
                jitter = random.uniform(-9.0, 9.0)
                pacing_target = max(10.0, _DYNAMIC_PACING_DELAY + jitter)
                
                # If _LAST_LLM_CALL_TIME is far in the past, reset it to now
                if now - _LAST_LLM_CALL_TIME > pacing_target:
                    _LAST_LLM_CALL_TIME = now - pacing_target
                    
                elapsed = now - _LAST_LLM_CALL_TIME
                if elapsed < pacing_target:
                    sleep_needed = pacing_target - elapsed
                else:
                    sleep_needed = 0.0
                
                # Cap the virtual queue to prevent compounding sleeps from growing infinitely.
                # Accommodates up to 4 parallel sub-agents staggered perfectly.
                _LAST_LLM_CALL_TIME = now + sleep_needed # Now completely uncapped supports as many as needed
                
            if sleep_needed > 0:
                # Sleep outside the lock in small increments to stay responsive to Ctrl+A (Abort)
                for _ in range(int(sleep_needed * 10)):
                    check_abort()
                    time.sleep(0.1)
                    
        try:
            res = model.invoke(messages)
            # Successful call: rapidly decay the pacing delay back toward 0.0 for local/high-quota APIs
            if _DYNAMIC_PACING_DELAY > 0.0:
                with _LLM_LOCK:
                    _DYNAMIC_PACING_DELAY = max(0.0, _DYNAMIC_PACING_DELAY - 1.5)
            return res
        except Exception as e:
            error_str = str(e)
            error_str_lower = error_str.lower()
            
            # 1. Quota / Rate limits: dynamically scale up delay and bubble up to let run_subagent checkpoint
            if any(term in error_str_lower for term in ["429", "ratelimit", "exhausted", "quota", "limit exceeded", "too many requests"]):
                with _LLM_LOCK:
                    if _DYNAMIC_PACING_DELAY < 56.0:
                        _DYNAMIC_PACING_DELAY = 65.0  # Activate with a safe baseline of ~1 RPM
                    else:
                        _DYNAMIC_PACING_DELAY = min(120.0, _DYNAMIC_PACING_DELAY * 1.5)  # Scale up on subsequent hits
                    # Reset queue time to "now" so waiting threads don't wait on stale future schedules
                    _LAST_LLM_CALL_TIME = time.time()
                raise e
                
            # 2. Fail fast if the model is dead, missing, or rejecting our tool schema
            if "400" in error_str or "404" in error_str or "not found" in error_str_lower or "not exist" in error_str_lower:
                safe_print(f"[bold red] FATAL API ERROR:[/bold red] {escape(error_str)}")
                raise e # Instantly crash the tool so the UI stops spinning
                
            # 3. Standard connection drops: print a single-line summary and retry
            first_line = error_str.splitlines()[0] if error_str else "Unknown connection issue"
            clean_err = first_line[:120] + "..." if len(first_line) > 120 else first_line
            retry_delay = load_global_settings().get("api_retry_delay", 15.0)
            safe_print(f" Connection dropped. Retrying in {int(retry_delay)}s... ({clean_err})")
            time.sleep(retry_delay)
    raise Exception("Max network retries exceeded.")

def log_tool(msg: str):
    """Bridge for tools to log to the active UI instance."""
    try:
        if CURRENT_APP:
            # We use a generic approach to avoid circular imports
            agent_view = CURRENT_APP.query_one("#ai_agent_view")
            agent_view.log_to_ui(msg)
    except:
        print(f"[Fallback]: {msg}")

def html_to_markdown(html_content: str) -> str:
    """Helper to convert fetched HTML to proper markdown text."""
    try:
        # ATX heading style uses # instead of underlining for headers
        return md(html_content, heading_style="ATX", strip=['script', 'style'])
    except Exception:
        return html_content

def get_safe_path(filepath: str):
    if CURRENT_APP:
        try:
            # Always use the IDE's current DirectoryTree path as the base
            base_dir = os.path.abspath(str(CURRENT_APP.query_one("#dir_tree").path))
        except Exception:
            base_dir = os.path.abspath(os.getcwd())
    else:
        base_dir = os.path.abspath(os.getcwd())
        
    # FIX: Resolve relative paths against base_dir, NOT the process CWD
    if not os.path.isabs(filepath):
        target_path = os.path.abspath(get_storage_path(base_dir, filepath))
    else:
        target_path = os.path.abspath(filepath)
    
    # Security check remains the same but is now accurate
    if os.path.commonpath([base_dir, target_path]) != base_dir:
        raise ValueError("Security Violation: Path is outside the allowed directory. Access Denied.")
        
    return target_path, os.path.relpath(target_path, base_dir)

def _get_venv_python(venv_dir: str) -> str:
    """Returns the path to the python executable within a venv, platform-agnostic."""
    if os.name == "nt":
        return get_storage_path(venv_dir, "Scripts", "python.exe")
    return get_storage_path(venv_dir, "bin", "python")

def load_dynamic_tools(agent_name: str) -> List[StructuredTool]:
    """Dynamically loads scripts from agents/skills/<agent_name>/active_tools/ and wraps them as StructuredTools."""
    safe_agent_name = agent_name.replace(" ", "_")
    active_tools_dir = get_storage_path("agents", "skills", safe_agent_name, "active_tools")
    if not os.path.exists(active_tools_dir):
        return []

    dynamic_tools = []
    for tool_folder in os.listdir(active_tools_dir):
        tool_path = get_storage_path(active_tools_dir, tool_folder)
        if not os.path.isdir(tool_path): continue
        
        schema_path = get_storage_path(tool_path, "schema.json")
        metadata_path = get_storage_path(tool_path, "metadata.json")
        
        if not os.path.exists(schema_path) or not os.path.exists(metadata_path):
            continue
            
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            with open(metadata_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            
            tool_name = schema.get("name", tool_folder)
            description = schema.get("description", "Dynamic tool skill.")
            arg_order = schema.get("arg_order") or meta.get("arg_order")
            entry_point = meta.get("entry_point")
            test_input = meta.get("test_input", {})
            
            if not entry_point: continue
            
            script_path = get_storage_path(tool_path, "logic", entry_point)
            if not os.path.exists(script_path): continue

            venv_dir = get_storage_path(tool_path, "venv")
            python_exe = _get_venv_python(venv_dir)

            mode_str = f"Positional CLI arguments order: [{', '.join(arg_order)}]" if arg_order else "Keyword CLI flags (--key value)"
            enhanced_description = f"{description}\n\nExecution Mode: {mode_str}"
            if test_input:
                sample_call = f"{tool_name}(" + ", ".join([f"{k}={repr(v)}" for k, v in test_input.items()]) + ")"
                enhanced_description += f"\nExample Call: {sample_call}"

            def make_run_func(s_path, t_name, p_exe, t_dir, a_order, t_input):
                def run_dynamic_script(**kwargs):
                    try:
                        check_abort()
                        log_tool(f"Executing dynamic skill: [bold cyan]{t_name}[/]")

                        env = os.environ.copy()
                        venv_bin = os.path.dirname(p_exe)
                        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
                        env["VIRTUAL_ENV"] = t_dir

                        if s_path.endswith(".py") and os.path.exists(p_exe):
                            cmd = [p_exe, s_path]
                        elif s_path.endswith(".sh"):
                            cmd = ["sh", s_path]
                        else:
                            cmd = [s_path]

                        # Filter out unprovided / None arguments completely
                        active_kwargs = {k: v for k, v in kwargs.items() if v is not None}

                        # --- ARGUMENT PASSING: Direct & Non-destructive ---
                        if a_order:
                            # Positional Mode
                            for arg_name in a_order:
                                clean_arg = arg_name.lstrip("-")
                                val = active_kwargs.get(clean_arg, active_kwargs.get(arg_name))
                                if val is not None:
                                    if isinstance(val, list):
                                        cmd.extend([str(item) for item in val])
                                    else:
                                        cmd.append(str(val))
                            # Pass any extra kwargs not in a_order as flags
                            for k, v in active_kwargs.items():
                                clean_k = k.lstrip("-")
                                if k not in a_order and clean_k not in a_order:
                                    if isinstance(v, bool):
                                        if v: cmd.append(f"--{clean_k}")
                                    elif isinstance(v, list):
                                        if all(isinstance(item, (str, int, float)) for item in v):
                                            cmd.append(f"--{clean_k}")
                                            cmd.extend([str(item) for item in v])
                                        else:
                                            cmd.extend([f"--{clean_k}", json.dumps(v)])
                                    elif isinstance(v, dict):
                                        cmd.extend([f"--{clean_k}", json.dumps(v)])
                                    else:
                                        cmd.extend([f"--{clean_k}", str(v)])
                        else:
                            # Keyword / Flag Mode
                            for k, v in active_kwargs.items():
                                clean_k = k.lstrip("-")
                                if isinstance(v, bool):
                                    if v: cmd.append(f"--{clean_k}")
                                elif isinstance(v, list):
                                    if all(isinstance(item, (str, int, float)) for item in v):
                                        cmd.append(f"--{clean_k}")
                                        cmd.extend([str(item) for item in v])
                                    else:
                                        cmd.extend([f"--{clean_k}", json.dumps(v)])
                                elif isinstance(v, dict):
                                    cmd.extend([f"--{clean_k}", json.dumps(v)])
                                else:
                                    cmd.extend([f"--{clean_k}", str(v)])

                        log_tool(f"Command: [dim]{' '.join(cmd)}[/]")

                        proc = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            #stdin=subprocess.DEVNULL,
                            text=True,
                            cwd=get_storage_path(t_dir, "logic"),
                            start_new_session=True,
                            env=env
                        )

                        while True:
                            check_abort()
                            try:
                                stdout, stderr = proc.communicate(timeout=0.1)
                                if isinstance(stdout, bytes): stdout = stdout.decode('utf-8', errors='replace')
                                if isinstance(stderr, bytes): stderr = stderr.decode('utf-8', errors='replace')
                                break
                            except subprocess.TimeoutExpired:
                                pass

                        # Loud execution report featuring concrete tool calling examples
                        executed_cmd_str = " ".join(cmd)
                        agent_call_str = f"{t_name}(" + ", ".join([f"{k}={repr(v)}" for k, v in active_kwargs.items()]) + ")"

                        report = [
                            f"[Executed Shell Command]: {executed_cmd_str}",
                            f"[Agent Call]: {agent_call_str}",
                            f"[Exit Code]: {proc.returncode}",
                            "",
                            f"--- PERFECTLY FORMED TOOL CALL EXAMPLES ({t_name}) ---"
                        ]

                        if t_input:
                            test_ex = f"{t_name}(" + ", ".join([f"{k}={repr(v)}" for k, v in t_input.items()]) + ")"
                            report.append(f"  Example 1 (Verified Staged Input): {test_ex}")

                        if a_order:
                            pos_ex = f"{t_name}(" + ", ".join([f"{a}='<val>'" for a in a_order]) + ")"
                            report.append(f"  Example 2 (Positional Syntax): {pos_ex}")
                            report.append(f"  [Mode]: Positional CLI Arguments -> Maps to order: {a_order}")
                        else:
                            report.append(f"  Example 2 (Keyword Syntax): {t_name}(key_name='value', boolean_flag='true')")
                            report.append(f"  [Mode]: Keyword CLI Flags -> Maps to '--key_name value'")

                        report.append("-----------------------------------------------------------------")

                        if stdout.strip():
                            report.append(f"\nSTDOUT:\n{stdout.strip()}")
                        else:
                            report.append("\nSTDOUT: (empty)")
                        if stderr.strip():
                            report.append(f"\nSTDERR:\n{stderr.strip()}")

                        return "\n".join(report)

                    except Exception as e:
                        if "aborted" in str(e).lower() or "interrupted" in str(e).lower():
                            try: os.killpg(os.getpgid(proc.pid), 9)
                            except: pass
                            raise e
                        return f"Error running dynamic skill {t_name}: {e}"
                return run_dynamic_script

            from pydantic import BaseModel, ConfigDict, create_model

            class DynamicToolBase(BaseModel):
                model_config = ConfigDict(extra='allow')

                def model_dump(self, *args, **kwargs):
                    d = super().model_dump(*args, **kwargs)
                    if getattr(self, '__pydantic_extra__', None):
                        d.update(self.__pydantic_extra__)
                    return d

                def dict(self, *args, **kwargs):
                    d = super().dict(*args, **kwargs)
                    if getattr(self, '__pydantic_extra__', None):
                        d.update(self.__pydantic_extra__)
                    return d

            field_definitions = {}
            if isinstance(test_input, dict):
                for k, v in test_input.items():
                    clean_k = k.lstrip("-")
                    field_definitions[clean_k] = (Optional[Any], None)

            if isinstance(arg_order, list):
                for a in arg_order:
                    clean_a = a.lstrip("-")
                    if clean_a not in field_definitions:
                        field_definitions[clean_a] = (Optional[Any], None)

            if isinstance(schema, dict) and "properties" in schema and isinstance(schema["properties"], dict):
                for k in schema["properties"].keys():
                    clean_k = k.lstrip("-")
                    if clean_k not in field_definitions:
                        field_definitions[clean_k] = (Optional[Any], None)

            ToolInputModel = create_model(
                f"{tool_name}_Input",
                __base__=DynamicToolBase,
                **field_definitions
            )

            dynamic_tools.append(StructuredTool.from_function(
                func=make_run_func(script_path, tool_name, python_exe, tool_path, arg_order, test_input),
                name=tool_name,
                description=enhanced_description,
                args_schema=ToolInputModel
            ))

        except Exception as e:
            print(f"Error loading dynamic skill {tool_folder}: {e}")

    return dynamic_tools

# --- SWE AGENT TOOLS ---

def format_numbered_lines(lines: List[str], start_line_num: int = 1, total_lines: Optional[int] = None) -> str:
    """Formats lines with right-aligned, space-padded line numbers so colons and code indentation align."""
    if not lines:
        return ""
    if total_lines is None:
        max_line_num = start_line_num + len(lines) - 1
    else:
        max_line_num = max(total_lines, start_line_num + len(lines) - 1)
        
    width = len(str(max_line_num))
    
    return "\n".join(
        f"{str(start_line_num + i).rjust(width)}: {line.rstrip('\r\n')}"
        for i, line in enumerate(lines)
    )
    
@tool
def read_file(filepath: str) -> str:
    """Reads a file and returns its content. Natively supports plain text, PDFs, PNGs, JPGs, and other images."""
    try:
        safe_path, display_path = get_safe_path(filepath)
        ext = os.path.splitext(safe_path)[1].lower()
        
        # Check for binary image/PDF formats
        if ext in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf'}:
            log_tool(f"Visualizing file: [cyan]{display_path}[/cyan]")
            return f"[Attached Image: {safe_path}]"
            
        log_tool(f"Reading file: [cyan]{display_path}[/cyan]")
        with open(safe_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return format_numbered_lines(lines, start_line_num=1)
    except Exception as e:
        return f"Error reading file: {e}"

@tool
def save_file(filepath: str, content: str) -> str:
    """Saves new content to a file, overwriting it if it exists."""
    try:
        safe_path, display_path = get_safe_path(filepath)
        
        # FIX: Create parent directories if they don't exist
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        
        log_tool(f"Saving file:[cyan] {display_path}[/cyan]")
        with open(safe_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Successfully saved {display_path}"
    except Exception as e:
        return f"Error saving file: {e}"

@tool
def edit_file(filepath: str, start_line: int, end_line: int, new_content: str) -> str:
    """Replaces lines from start_line to end_line (inclusive, 1-indexed) in the given file with new_content."""
    try:
        safe_path, display_path = get_safe_path(filepath)
        log_tool(f"Editing file:[cyan] {display_path} (lines {start_line}-{end_line})[/cyan]")
        
        with open(safe_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Sanitize boundary inputs
        start_idx = max(0, start_line - 1)
        end_idx = max(start_idx, end_line) # Ensure end_idx >= start_idx

        # Calculate edit position in updated list
        edit_pos = min(start_idx, len(lines))
        
        # Prepare replacement lines
        replacement = [line + "\n" for line in new_content.splitlines()]
        
        # Apply edit
        lines = lines[:start_idx] + replacement + lines[end_idx:]
        
        # Write back to disk
        with open(safe_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        total_lines = len(lines)
        if total_lines == 0:
            return f"Successfully edited {display_path} (file is now empty)."

        # Calculate snippet range in the POST-EDIT file
        snippet_start_idx = max(0, edit_pos - 20)
        last_edited_idx = edit_pos + max(0, len(replacement) - 1)
        snippet_end_idx = min(total_lines, last_edited_idx + 1 + 20)

        snippet_lines = lines[snippet_start_idx:snippet_end_idx]
        
        # Format snippet with rstrip('\r\n') to prevent terminal rendering bugs
        snippet = format_numbered_lines(
            snippet_lines,
            start_line_num=snippet_start_idx + 1,
            total_lines=total_lines
        )

        return f"Successfully edited {display_path}\n\nSnippet around edit:\n{snippet}"
    except Exception as e:
        return f"Error editing file: {e}"

@tool
def list_files(directory: str = None) -> str:
    """Lists code files, PDFs, and PNGs in the directory as a tree structure, relative to the project root."""
    try:
        if not directory:
            directory = CURRENT_APP.query_one("#dir_tree").path if CURRENT_APP else os.getcwd()
                
        safe_dir, display_dir = get_safe_path(directory)
        
        # UI Log shows the relative target (e.g. '.' or 'folder_name')
        log_dir_name = display_dir if display_dir != "." else "Project Root"
        log_tool(f"Listing files in:[cyan] {log_dir_name}[/cyan]")
        
        ignore_patterns =['.git', '__pycache__', 'node_modules', 'venv', '.venv', '.idea', '.vscode']
        
        # Determine active workspace root base directory
        try:
            base_dir = str(CURRENT_APP.query_one("#dir_tree").path) if CURRENT_APP else os.getcwd()
        except Exception:
            base_dir = os.getcwd()

        # 1. Load project root-level ignore rules (standard fallback)
        for ignore_file in ('.gitignore', '.listignore'):
            ignore_path = os.path.join(base_dir, ignore_file)
            if os.path.exists(ignore_path):
                with open(ignore_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            if line.endswith('/'): 
                                line = line[:-1]
                            # Standardize directory separators for cross-platform matches
                            line = line.replace('/', os.sep)
                            ignore_patterns.append(line)

        # 2. Load directory-level ignore rules (cascading subdirectory overrides)
        if os.path.abspath(safe_dir) != os.path.abspath(base_dir):
            for ignore_file in ('.gitignore', '.listignore'):
                ignore_path = os.path.join(safe_dir, ignore_file)
                if os.path.exists(ignore_path):
                    with open(ignore_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                if line.endswith('/'): 
                                    line = line[:-1]
                                line = line.replace('/', os.sep)
                                ignore_patterns.append(line)

        def is_ignored(rel_path):
            parts = rel_path.split(os.sep)
            for pattern in ignore_patterns:
                if any(fnmatch.fnmatch(part, pattern) for part in parts) or fnmatch.fnmatch(rel_path, pattern):
                    return True
            return False

        ALLOWED_EXTENSIONS = {
            '.py', '.go', '.rs', '.c', '.h', '.cpp', '.hpp', '.js', '.jsx', '.ts', '.tsx', 
            '.html', '.css', '.json', '.md', '.txt', '.yaml', '.yml', '.sh', '.bat',
            '.java', '.sql', '.toml', '.pdf', '.png', '.sgy', '.segy', '.dlis', '.lis', '.las'
        }

        valid_rel_paths =[]
        for root, dirs, files in os.walk(safe_dir):
            dirs[:] =[d for d in dirs if not is_ignored(os.path.relpath(get_storage_path(root, d), safe_dir))]
            
            for file in files:
                rel_path = os.path.relpath(get_storage_path(root, file), safe_dir)
                if is_ignored(rel_path):
                    continue
                    
                ext = os.path.splitext(file)[1].lower()
                if ext in ALLOWED_EXTENSIONS or file in['Dockerfile', 'Makefile']:
                    valid_rel_paths.append(rel_path)

        if not valid_rel_paths:
            return "No matching files found."

        # Convert flat paths to a nested dictionary
        tree_dict = {}
        for path in valid_rel_paths:
            parts = path.split(os.sep)
            current = tree_dict
            for part in parts:
                current = current.setdefault(part, {})

        # Recursively format the dictionary into a tree string
        def format_tree(d, prefix=""):
            lines =[]
            # Sort folders first, then files alphabetically
            keys = sorted(d.keys(), key=lambda k: (not bool(d[k]), k.lower()))
            for i, key in enumerate(keys):
                is_last = i == (len(keys) - 1)
                connector = "└── " if is_last else "├── "
                lines.append(f"{prefix}{connector}{key}")
                if d[key]:
                    extension_prefix = "    " if is_last else "│   "
                    lines.extend(format_tree(d[key], prefix + extension_prefix))
            return lines

        return "\n".join(format_tree(tree_dict))
    except Exception as e:
        return f"Error listing files: {e}"

@tool
def run_terminal_command(command: str) -> str:
    """Executes an arbitrary terminal command in the project root with live output streaming."""
    try:
        # Get the project root from the IDE
        base_dir = os.path.abspath(CURRENT_APP.query_one("#dir_tree").path)
        
        log_tool(f"Running command: [cyan]{command}[/cyan]")
        
        clean_env = os.environ.copy()
        
        # 1. Clean the host environment's venv variables
        host_venv = clean_env.pop("VIRTUAL_ENV", None)
        clean_env.pop("UV_ACTIVE", None)
        
        paths = clean_env.get("PATH", "").split(os.pathsep)
        if host_venv:
            host_bin = os.path.join(host_venv, "Scripts" if os.name == "nt" else "bin")
            paths = [p for p in paths if os.path.normpath(p) != os.path.normpath(host_bin)]
            
        # 2. Inject our scratchpad defaultVenv instead
        scratch_bin = os.path.join(DEFAULT_VENV_PATH, "Scripts" if os.name == "nt" else "bin")
        paths.insert(0, scratch_bin)
        
        clean_env["PATH"] = os.pathsep.join(paths)
        clean_env["VIRTUAL_ENV"] = DEFAULT_VENV_PATH
        
        # Use start_new_session=True to create a new process group for the command
        # This allows us to kill the command and all its children
        proc = subprocess.Popen(
            command, 
            shell=True, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            cwd=base_dir,
            start_new_session=True,
            env=clean_env
        )
        
        # Buffers to track what we have already printed to the chat UI
        stdout_logged = ""
        stderr_logged = ""
        
        # Poll for completion OR abort/interrupt while streaming output chunks live
        while True:
            # 1. User Abort Check
            if ABORT_EVENT.is_set():
                # Forcefully kill the process or process group
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
                        proc.kill()  # Windows: Unconditionally terminates the process
                    else:
                        os.killpg(os.getpgid(proc.pid), 9)  # macOS/Linux: Terminates process group
                except Exception:
                    pass
                return "Error: Command aborted by user."
            
            # 2. Batch ID check for interrupts
            if CURRENT_AGENT_VIEW: # <--- CHANGED THIS
                try:
                    task_batch = getattr(thread_context, "batch_id", 0)
                    if task_batch != 0 and task_batch != getattr(CURRENT_AGENT_VIEW, "current_batch_id", 0):
                        try:
                            if os.name == "nt":
                                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
                                proc.kill()
                            else:
                                os.killpg(os.getpgid(proc.pid), 9)
                        except Exception:
                            pass
                        return "Error: Command interrupted by newer request."
                except Exception:
                    pass

            try:
                # Attempt to complete the process execution
                stdout, stderr = proc.communicate(timeout=0.1)
                
                # Ensure they are strings even if text=True failed to decode early
                if isinstance(stdout, bytes): stdout = stdout.decode('utf-8', errors='replace')
                if isinstance(stderr, bytes): stderr = stderr.decode('utf-8', errors='replace')

                # Command finished! Stream the remaining fresh output to the UI
                new_out = stdout[len(stdout_logged):] if stdout else ""
                new_err = stderr[len(stderr_logged):] if stderr else ""
                
                if new_out:
                    new_out_clean = re.sub(r'[A-Za-z0-9+/=]{100,}', '<base64_chunk_omitted>', new_out)
                    log_tool(f"[#808080]{escape(new_out_clean)}[/#808080]")
                if new_err:
                    new_err_clean = re.sub(r'[A-Za-z0-9+/=]{100,}', '<base64_chunk_omitted>', new_err)
                    log_tool(f"[bold red]{escape(new_err_clean)}[/bold red]")
                    
                break  # Exit loop successfully
                
            except subprocess.TimeoutExpired as e:
                # Process is still running. Grab what has been captured so far.
                stdout = e.stdout or ""
                stderr = e.stderr or ""
                
                # Ensure they are strings (Python 3.13 Popen TimeoutExpired often contains bytes)
                if isinstance(stdout, bytes): stdout = stdout.decode('utf-8', errors='replace')
                if isinstance(stderr, bytes): stderr = stderr.decode('utf-8', errors='replace')

                # Isolate the newly printed text since the last check
                new_out = stdout[len(stdout_logged):]
                new_err = stderr[len(stderr_logged):]
                
                # If new text arrived, push it immediately to the UI and update the markers
                if new_out:
                    new_out_clean = re.sub(r'[A-Za-z0-9+/=]{100,}', '<base64_chunk_omitted>', new_out)
                    log_tool(f"[#808080]{escape(new_out_clean)}[/#808080]")
                    stdout_logged = stdout
                if new_err:
                    new_err_clean = re.sub(r'[A-Za-z0-9+/=]{100,}', '<base64_chunk_omitted>', new_err)
                    log_tool(f"[bold red]{escape(new_err_clean)}[/bold red]")
                    stderr_logged = stderr
            
        return f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    except Exception as e:
        return f"Error: {e}"

@tool
def fetch_url(url: str) -> str:
    """Fetches text/HTML content from a URL."""
    try:
        from bs4 import BeautifulSoup
        from curl_cffi import requests as cffi_requests
        from markdownify import markdownify as md
        
        check_abort()
        log_tool(f"Fetching URL:[cyan] {url}[/cyan]")
        
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Bypasses Cloudflare/WAFs using Chrome TLS impersonation
        resp = cffi_requests.get(url, impersonate="chrome", timeout=150, allow_redirects=True)
        resp.raise_for_status()
        
        check_abort()
        
        # Reference Logic Implementation:
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # 1. Remove "Junk" tags found in url_md.py
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            tag.decompose()
            
        # 2. Extract body and convert to markdown using ATX style
        cleaned_html = str(soup.body or soup)
        return md(cleaned_html, heading_style="ATX").strip()

    except Exception as e:
        return f"Error fetching URL: {e}"

@tool
def search_web(query: str) -> str:
    """Search for current information, facts, websites, or general web content."""
    # 1. Paced search delay (OUTSIDE the lock) using the global search pacing queue
    try:
        check_abort()
        sleep_time = _get_search_delay()
        if sleep_time > 0.0:
            log_tool(f" Throttling search to prevent rate limits (sleeping {int(sleep_time)}s)...")
            for _ in range(int(sleep_time * 10)):
                check_abort()
                time.sleep(0.1)
        log_tool(f"Searching for:[cyan] '{query}'[/cyan]...")
    except Exception as e:
        return f"Search failed: {str(e)}"

    # 2. Execute search
    try:
        import subprocess, json, os
        bin_path = get_storage_path(os.path.dirname(os.path.abspath(__file__)), "bin", "federate_search" + (".exe" if os.name == "nt" else ""))
        
        max_results = str(load_global_settings().get("max_search_results", 10))
        # Use Popen to allow interruption mid-search
        proc = subprocess.Popen(
            [bin_path, query, max_results],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True
        )
        
        while proc.poll() is None:
            if ABORT_EVENT.is_set():
                try:
                    import os as os_lib
                    os_lib.killpg(os_lib.getpgid(proc.pid), 9)
                except: pass
                raise Exception("Search aborted by user.")
            
            # Also check batch_id mid-poll
            if CURRENT_APP:
                try:
                    agent_view = CURRENT_APP.query_one("#ai_agent_view")
                    task_batch = getattr(thread_context, "batch_id", 0)
                    if task_batch != 0 and task_batch != getattr(agent_view, "current_batch_id", 0):
                        try:
                            import os as os_lib
                            os_lib.killpg(os_lib.getpgid(proc.pid), 9)
                        except: pass
                        raise Exception("Search interrupted by new request.")
                except: pass
            
            time.sleep(0.1)

        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            return f"Search failed: {stderr}"
            
        results = [{"title": r["Title"], "href": r["Link"], "body": r["Snippet"]} for r in json.loads(stdout)]
            
        if not results:
            return f"No web results found for '{query}'"
        
        output = f"Found {len(results)} web results for '{query}':\n\n"
        for i, result in enumerate(results[:10], 1):
            url = result.get('href', result.get('link', 'No URL'))
            output += f"{i}. **{result.get('title', 'No title')}**\n"
            output += f"   {url}\n" 
            output += f"   {result.get('body', 'No description')[:250]}...\n\n"
        
        return output
    except Exception as e:
        return f"Search failed: {str(e)}"

# =========================================================================================
# ==================== DEEP RESEARCH MULTI-AGENT ORCHESTRATOR =============================
# =========================================================================================

TARGET_CONTEXT_TOKENS = 28000
FINAL_ANSWER_MIN_LENGTH = 5000
thread_context = threading.local()

def safe_print(text):
    """Bridge orchestrator logging natively to Textual IDE and update Live UI Split-Panes."""
    if not text: return
    task_id = getattr(thread_context, 'task_name', 'System')
    
    if task_id != 'System' and CURRENT_APP:
        # --- NEW: Sub-Agent logs bypass the main chat and go straight to the Progress Dash ---
        try:
            agent_view = CURRENT_APP.query_one("#ai_agent_view")
            if hasattr(agent_view, "update_progress"):
                percent = None
                if isinstance(text, str):
                    match = re.search(r"\[(\d+)/(\d+)\]", text)
                    if match:
                        curr, total = map(int, match.groups())
                        percent = min((curr / total) * 100, 100.0)
                    elif "Finished" in text or "Success" in text or "finished" in text.lower():
                        percent = 100.0
                    elif "Initializing" in text:
                        percent = 5.0

                # Send directly to the right-pane RichLog
                agent_view.update_progress(task_id, percent, f"[{task_id}] {text}")
        except Exception:
            pass
    else:
        # --- System logs (Master Orchestrator) still show in the Main Chat ---
        log_tool(f"[dim cyan][{task_id}][/dim cyan] {text}")

    # Fallback disk logging
    log_path = getattr(thread_context, 'log_path', None)
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{task_id} ::: {text}\n")
        except: pass

def sanitize_filename(name):
    clean_name = re.sub(r'[<>:"/\\|?*]', '', name).strip().strip('.')
    return clean_name if len(clean_name) > 0 else "output_report"

def get_gmt_string():
    now_gmt = datetime.datetime.now(datetime.timezone.utc)
    return f"Todays date is {now_gmt.strftime('%Y-%m-%d')} and the current time is {now_gmt.strftime('%H:%M:%S')} GMT."

DEFAULT_PDF_CSS_TEMPLATE = """
{bundled_fonts_css}

@import url('https://fonts.googleapis.com/css2?family=Michroma&family=Space+Grotesk:wght@300;400;700&family=Space+Mono&display=swap');

body {{
  font-family: "{body_font}", sans-serif;
  font-size: {body_font_size};
  line-height: 1.6;
  color: #111;
}}

h1, h2, h3, h4 {{
  font-family: "{header_font}", sans-serif;
  letter-spacing: 0.04em;
  margin-top: 2em;
}}

code, pre {{
  font-family: "{code_font}", monospace;
  font-size: 9.5pt;
}}

pre {{
  background: #0f1117;
  color: #e6e6e6;
  padding: 1em;
  border-radius: 6px;
  overflow-x: auto;
}}

@page {{
  size: A4;
  margin: 20mm;

  @bottom-left {{
    content: "Page " counter(page) " of " counter(pages);
    font-family: "{body_font}", sans-serif;
    font-size: 8pt;
    color: #666;
  }}

  @bottom-right {{
    content: "{footer_text}";
    font-family: "{header_font}", sans-serif;
    font-size: 8pt;
    color: #666;
  }}
}}

h1, h2 {{
  page-break-before: always;
}}

h1 {{
  break-before: page;
  text-align: center;
  margin-top: 40%;
  font-size: {h1_font_size};
  letter-spacing: 0.08em;
}}

img {{
  max-width: 100%;
  height: auto;
  display: block;
  margin: 1.5em auto;
  border-radius: 4px;
}}
"""

def render_markdown_to_pdf(md_path: str, pdf_path: str):
    if not WEASYPRINT_AVAILABLE:
        safe_print(" [PDF ERROR] weasyprint not installed.")
        return
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()
        global_settings = load_global_settings()
        if global_settings.get("research_images_as_links", False):
            md_text = md_text.replace('![', '[')

        html_text = markdown(md_text, extensions=["fenced_code", "tables", "toc", "codehilite", "extra"])
        
        # 1. Respect user workspace CSS overrides if present
        css_file = Path("styles/style.css") if Path("styles/style.css").exists() else Path("style.css")
        
        if css_file.exists():
            stylesheet = CSS(filename=str(css_file))
        else:
            # 2. Build dynamic stylesheet from global settings
            footer_text = global_settings.get("pdf_footer_text", "FEDERATE RESEARCH REPORT")
            body_font = global_settings.get("pdf_body_font", "Space Grotesk")
            header_font = global_settings.get("pdf_header_font", "Michroma")
            code_font = global_settings.get("pdf_code_font", "Space Mono")
            body_font_size = global_settings.get("pdf_body_font_size", "11pt")
            h1_font_size = global_settings.get("pdf_h1_font_size", "28pt")

            # Resolve local bundled fonts directory relative to this package file
            package_dir = os.path.dirname(os.path.abspath(__file__))
            fonts_dir = Path(package_dir) / "styles" / "fonts"

            font_declarations = []
            if (fonts_dir / "Michroma-Regular.ttf").exists():
                font_declarations.append(
                    f"@font-face {{ font-family: 'Michroma'; src: url('{(fonts_dir / 'Michroma-Regular.ttf').as_uri()}'); }}"
                )
            if (fonts_dir / "SpaceGrotesk-VariableFont_wght.ttf").exists():
                font_declarations.append(
                    f"@font-face {{ font-family: 'Space Grotesk'; src: url('{(fonts_dir / 'SpaceGrotesk-VariableFont_wght.ttf').as_uri()}'); }}"
                )
            if (fonts_dir / "SpaceGrotesk-Bold.ttf").exists():
                font_declarations.append(
                    f"@font-face {{ font-family: 'Space Grotesk'; font-weight: bold; src: url('{(fonts_dir / 'SpaceGrotesk-Bold.ttf').as_uri()}'); }}"
                )

            bundled_fonts_css = "\n".join(font_declarations)

            css_string = DEFAULT_PDF_CSS_TEMPLATE.format(
                bundled_fonts_css=bundled_fonts_css,
                footer_text=footer_text,
                body_font=body_font,
                header_font=header_font,
                code_font=code_font,
                body_font_size=body_font_size,
                h1_font_size=h1_font_size
            )
            stylesheet = CSS(string=css_string)

        html = HTML(string=html_text, base_url=str(Path.cwd()))
        html.write_pdf(pdf_path, stylesheets=[stylesheet])
        safe_print(f" [PDF] Success: {pdf_path}")
    except Exception as e:
        safe_print(f" [PDF ERROR] WeasyPrint failed: {e}")

def get_token_status(messages: List[BaseMessage]) -> str:
    try: encoding = tiktoken.get_encoding("cl100k_base")
    except: encoding = tiktoken.get_encoding("cl100k_base")
    total = sum(len(encoding.encode(str(m.content or ""))) + 4 for m in messages)
    target_tokens = load_global_settings().get("research_context_tokens", 28000)
    return f"[{total}/{target_tokens}]"

def get_total_tokens(messages: List[BaseMessage]) -> int:
    try: encoding = tiktoken.get_encoding("cl100k_base")
    except: encoding = tiktoken.get_encoding("cl100k_base")
    return sum(len(encoding.encode(str(m.content or ""))) + 4 for m in messages)

def scrape_full_content(url: str, max_tokens_per_fetch: int = None) -> str:
    try:
        from curl_cffi import requests as cffi_requests
        
        if max_tokens_per_fetch is None:
            max_tokens_per_fetch = load_global_settings().get("scraper_max_tokens", 30000)
        
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        with cffi_requests.Session(impersonate="chrome") as session:
            resp = session.get(url, timeout=15, stream=True, allow_redirects=True)
            resp.raise_for_status()
            
            max_bytes = load_global_settings().get("scraper_max_bytes", 1000000)
            content = b""
            
            # --- THE FIX: Absolute Time Limit ---
            start_time = time.time()
            timeout = load_global_settings().get("scraper_timeout", 120.0)
            
            for chunk in resp.iter_content(chunk_size=8192):
                content += chunk
                
                # Size Kill-Switch
                if len(content) > max_bytes:
                    safe_print(f"[NETWORK] Kill switch engaged: Page exceeds {max_bytes / 1000000:.1f}MB. Stopping download.")
                    break
                    
                # Time Kill-Switch (Abort if page takes longer than configured timeout)
                if time.time() - start_time > timeout:
                    safe_print(f"  [NETWORK] Time switch engaged: Server too slow (>{int(timeout)}s). Aborting.")
                    break
                    
            raw_html = content.decode("utf-8", errors="ignore")

        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(["script", "style", "noscript", "form", "svg", "img", "iframe", "button"]):
            tag.decompose()
        
        markdown_str = md(str(soup.body or soup), heading_style="ATX").strip()
        
        encoding = tiktoken.get_encoding("cl100k_base")
        tokens = encoding.encode(markdown_str)
        if len(tokens) > max_tokens_per_fetch:
            markdown_str = encoding.decode(tokens[:max_tokens_per_fetch]) + "\n\n... (Content truncated) ..."
            
        return markdown_str
    except Exception as e:
        return f"SCRAPE_ERROR: {str(e)}"

# --- SubAgent State & Schemas ---
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    hidden_urls: Dict[int, str]
    source_manifest: Dict[int, str]

class SearchWeb(BaseModel):
    query: str = Field(description="Search terms")

class FetchDetails(BaseModel):
    index: int = Field(description="1-10")

class FinalResponse(BaseModel):
    answer: str = Field(description="Report content")

class ResearchPlan(BaseModel):
    tasks: List[Dict[str, str]] = Field(description="List of dicts with 'search_prompt' and 'task_name'")

class ProjectName(BaseModel):
    title: str = Field(description="3-5 words, technical, no special characters. Cannot be generic like 'Research Project'")

# --- SubAgent Graph Nodes ---
def node_decide(state: AgentState):
    check_abort()
    tokens = get_total_tokens(state["messages"])
    target_tokens = load_global_settings().get("research_context_tokens", 28000)
    if tokens < target_tokens:
        # Enforce research via Prompt
        prompt = f"QUOTA: {tokens}/{target_tokens}. You MUST use the 'SearchWeb' tool to find more technical details. Do not quit."
        model = thread_context.llm.bind_tools([SearchWeb])
    else:
        prompt = f"QUOTA MET: {tokens}/{TARGET_CONTEXT_TOKENS}. Use 'FinalResponse' tool to synthesize the report now."
        model = thread_context.llm.bind_tools([FinalResponse])
    
    return {"messages": [resilient_invoke(model, state["messages"] + [HumanMessage(content=prompt)])]}

def node_agent_select(state: AgentState):
    check_abort()
    tokens = get_total_tokens(state["messages"])
    prompt = (
        f"QUOTA: {tokens}/{TARGET_CONTEXT_TOKENS}. "
        "Review the search results. If useful, use FetchDetails(index=X). "
        "If irrelevant, use SearchWeb(query=...) to try a different query. "
        "YOU MUST USE ONE OF THESE TWO TOOLS."
    )
    model = thread_context.llm.bind_tools([FetchDetails, SearchWeb])
    return {"messages": [resilient_invoke(model, state["messages"] + [HumanMessage(content=prompt)])]}

def node_execute_search(state: AgentState):
    check_abort()
    last_msg = state["messages"][-1]
    tool_call = last_msg.tool_calls[0]
    query = tool_call["args"].get("query", "more details")
    
    current_urls = state.get("hidden_urls", {})
    current_manifest = state.get("source_manifest", {})
    
    # 1. Paced search delay (OUTSIDE the lock) using the global search pacing queue
    try:
        check_abort()
        sleep_time = _get_search_delay()
        if sleep_time > 0.0:
            safe_print(f" Throttling search to prevent rate limits (sleeping {int(sleep_time)}s)...")
            for _ in range(int(sleep_time * 10)):
                check_abort()
                time.sleep(0.1)
        safe_print(f" [SEARCH] '{query}'")
    except Exception as e:
        if "aborted" in str(e).lower() or "interrupted" in str(e).lower(): raise e
        content = f"Search Error: {e}"
        return {"messages":[ToolMessage(content=content, tool_call_id=tool_call["id"], name="SearchWeb")],
                "hidden_urls": current_urls, "source_manifest": current_manifest}

    # 2. Execute search
    try:
        import subprocess, json, os
        bin_path = get_storage_path(os.path.dirname(os.path.abspath(__file__)), "bin", "federate_search" + (".exe" if os.name == "nt" else ""))
        
        max_results = str(load_global_settings().get("max_search_results", 10))
        # Use Popen to allow interruption mid-search
        proc = subprocess.Popen(
            [bin_path, query, max_results],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True
        )
        
        while proc.poll() is None:
            check_abort()
            time.sleep(0.1)

        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            content = f"Search failed: {stderr}"
        else:
            results = [{"title": r["Title"], "href": r["Link"], "body": r["Snippet"]} for r in json.loads(stdout)]
            start_idx = len(current_urls) + 1
            content = f"Results for '{query}':\n\n"
            for i, r in enumerate(results, start=start_idx):
                current_urls[i] = r.get('href', r.get('link', ''))
                current_manifest[i] = r.get('title', 'Unknown')
                content += f"{i}. {r.get('title')}\n   Snippet: {r.get('body')}\n\n"
    except Exception as e:
        if "aborted" in str(e).lower() or "interrupted" in str(e).lower(): raise e
        content = f"Search Error: {e}"

    return {"messages":[ToolMessage(content=content, tool_call_id=tool_call["id"], name="SearchWeb")],
            "hidden_urls": current_urls, "source_manifest": current_manifest}

def node_execute_fetch(state: AgentState):
    check_abort()
    last_msg = state["messages"][-1]
    status = get_token_status(state["messages"])
    
    if not hasattr(last_msg, "tool_calls") or len(last_msg.tool_calls) == 0:
        safe_print(f"  [RECOVERY] AI missed tool call. Retrying...")
        return {"messages":[SystemMessage(content="Error: You responded with text. You MUST use the FetchDetails tool with an index, or SearchWeb tool to start another search.")]}

    tool_call = last_msg.tool_calls[0]
    tool_name = tool_call["name"]

    if tool_name == "SearchWeb":
        query = tool_call["args"].get("query", "more info")
        safe_print(f" [PIVOT] AI rejected snippets. New Search: '{query}'")
        return {"messages":[ToolMessage(content=f"Pivot: Starting new search for {query}", tool_call_id=tool_call["id"], name=tool_name)]}

    try: idx = int(tool_call["args"].get("index", 0))
    except: idx = 0

    if idx == 0:
        content = "Agent skipped fetching."
    else:
        url = state.get("hidden_urls", {}).get(idx)
        if not url: content = f"Error: Index {idx} not found."
        else:
            safe_print(f" [FETCH] Index {idx} | {status}")
            content = scrape_full_content(url)
            check_abort()
            
    return {"messages":[ToolMessage(content=content, tool_call_id=tool_call["id"], name="FetchDetails")]}

def node_final(state: AgentState):
    check_abort()
    tokens = get_total_tokens(state["messages"])
    manifest = state.get("source_manifest", {})
    safe_print(f" [TARGET REACHED] {tokens} tokens. Synthesizing final answer...")
    reference_table = "\n".join([f"[Source {i}]: {title}" for i, title in manifest.items()])
    
    min_length = load_global_settings().get("research_min_length", 5000)
    final_instruction = f"""
        CITATION LOOKUP TABLE (Use these numbers):
        {reference_table}

        CRITICAL INSTRUCTIONS:
        1. Produce a massive, professional technical report.
        2. You MUST cite your sources inline using [Source X] format.
        3. Every claim or technical detail must be followed by at least one [Source X] tag.
        4. Use the Lookup Table above to ensure the numbers match the correct information.
        5. If response is shorter than {min_length} tokens, it will be rejected.
        """
    try:
        check_abort()
        res = resilient_invoke(thread_context.llm, state["messages"] + [HumanMessage(content=final_instruction)])
        if not res.content or len(res.content.strip()) < min_length:
            return {"messages":[AIMessage(content="RETRY_REQUIRED: Final answer was blank or too short.")]}
        return {"messages": [res]}
    except Exception as e:
        if "aborted" in str(e).lower() or "interrupted" in str(e).lower(): raise e
        return {"messages":[AIMessage(content=f"FINAL_SYNTH_ERROR: {str(e)}")]}

# --- SubAgent Routing ---
def route_after_decide(state: AgentState):
    last_msg = state["messages"][-1]
    tokens = get_total_tokens(state["messages"])
    target_tokens = load_global_settings().get("research_context_tokens", 28000)
    if hasattr(last_msg, "tool_calls") and len(last_msg.tool_calls) > 0:
        tool_name = last_msg.tool_calls[0]["name"]
        if tokens < target_tokens:
            if tool_name == "SearchWeb": return "search"
            safe_print(f"[ENFORCER] AI tried to quit at {tokens} tokens. Forcing more research.")
            return "decide" 
        return "search" if tool_name == "SearchWeb" else "final"
    return "decide"

def route_after_search(state: AgentState): return "select" if state["messages"][-1].type == "tool" else "decide"
def route_after_fetch(state: AgentState): return "decide" if state["messages"][-1].type == "tool" else "select"
def route_after_final(state: AgentState):
    content = state["messages"][-1].content or ""
    if "RETRY_REQUIRED" in content or "FINAL_SYNTH_ERROR" in content or len(content.strip()) < 100:
        safe_print("[VALIDATION] Blank or inadequate answer detected. Forcing retry...")
        return "retry"
    if hasattr(state["messages"][-1], "tool_calls") and state["messages"][-1].tool_calls:
        return "retry"
    safe_print(" [SUCCESS] Final report accepted.")
    return "end"

# Compile SubAgent Graph
builder = StateGraph(AgentState)
builder.add_node("decide", node_decide)
builder.add_node("search_exec", node_execute_search)
builder.add_node("select_idx", node_agent_select)
builder.add_node("fetch_exec", node_execute_fetch)
builder.add_node("final", node_final)

builder.add_edge(START, "decide")
builder.add_conditional_edges("decide", route_after_decide, {"search": "search_exec", "final": "final", "decide": "decide"})
builder.add_conditional_edges("search_exec", route_after_search, {"select": "select_idx", "decide": "decide"})
builder.add_edge("select_idx", "fetch_exec")
builder.add_conditional_edges("fetch_exec", route_after_fetch, {"decide": "decide", "select": "select_idx"})
builder.add_conditional_edges("final", route_after_final, {"retry": "final", "end": END})
subagent_app = builder.compile(checkpointer=shared_memory)

# --- Execution Hooks ---
def run_subagent(search_prompt, task_name, output_dir=".", config=None, batch_id: int = 0):
    register_thread()
    try:
        return _run_subagent_logic(search_prompt, task_name, output_dir, config, batch_id)
    finally:
        unregister_thread()

def _run_subagent_logic(search_prompt, task_name, output_dir=".", config=None, batch_id: int = 0):
    thread_context.task_name = task_name 
    thread_context.log_path = get_storage_path(output_dir, "research_status.log")
    thread_context.batch_id = batch_id
    
    # FIX: Default to Gemini instead of Stepfun, and prioritize passed config
    llm_model = config.get("model_name", "google/gemini-2.5-flash:free") if config else "google/gemini-2.5-flash:free"
    
    thread_context.llm = ChatOpenAI(
        model=llm_model,
        api_key=config.get("api_key", ""),
        base_url=config.get("base_url", "https://openrouter.ai/api/v1"),
        temperature=0,
        model_kwargs={"reasoning_effort": "high"}
    )

    safe_print(f"Initializing {task_name}...")
    filename = sanitize_filename(task_name)
    md_path = get_storage_path(output_dir, f"{filename}.md")
    pdf_path = get_storage_path(output_dir, f"{filename}.pdf")
    
    # Thread ID for Persistence
    thread_id = "research_" + "".join(c if c.isalnum() else "_" for c in task_name)
    run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 5000}
    
    shrink_attempts = 0       # <--- ADD THIS
    max_shrink = load_global_settings().get("max_shrink_attempts", 15)
    
    # Resumption Loop
    for attempt in range(1000):
        check_abort()
        try:
            state = subagent_app.get_state(run_config)
            
            if not state.values:
                safe_print(f" {task_name}: Researching...")
                time_msg = get_gmt_string()
                final_state = subagent_app.invoke(
                    {"messages":[SystemMessage(content=time_msg), ("user", search_prompt)], "hidden_urls": {}, "source_manifest": {}},
                    run_config
                )
            else:
                safe_print(f" {task_name}: Resuming from saved checkpoint...")
                final_state = subagent_app.invoke(None, run_config)
            
            # Processing Results
            report_text = final_state["messages"][-1].content
            url_map = final_state.get("hidden_urls", {})

            def inject_links(match):
                idx = int(match.group(1))
                url = url_map.get(idx)
                return f"[[Source {idx}]({url})]" if url else f"[Source {idx}]"

            final_message = re.sub(r"\[Source (\d+)\]", inject_links, report_text)
            
            safe_print(f"[POST-PROCESS] Linked {len(url_map)} sources in final Markdown.")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(final_message)
            
            render_markdown_to_pdf(md_path, pdf_path)
            safe_print(f" {task_name}: Finished.")
            # --- START TELEGRAM DISPATCH HOOK ---
            if CURRENT_APP:
                try:
                    agent_view = CURRENT_APP.query_one("#ai_agent_view")
                    chat_id = getattr(agent_view, "current_telegram_chat_id", None)
                    
                    if chat_id:
                        tm = agent_view.telegram_manager
                        # Use send_voiceover instead of send_message to avoid the text wall
                        tm.send_voiceover(chat_id, 
                                         full_text=final_message, 
                                         notification_text=f" {task_name} Research Complete.")
                        
                        # Send the PDF Document
                        tm.send_document(chat_id, pdf_path, caption=f"PDF Report: {task_name}")
                except Exception: pass
            # --- END TELEGRAM DISPATCH HOOK ---
            return final_message
            
        except Exception as e:
            error_str = str(e)
            if "interrupted" in error_str.lower() or "aborted" in error_str.lower():
                raise e
                
            # 1. API Rate Limits / Quota Exhaustion
            if any(term in error_str.lower() for term in ["429", "ratelimit", "exhausted", "quota", "limit exceeded", "too many requests"]):
                quota_delay = load_global_settings().get("quota_retry_delay", 120.0)
                safe_print(f" {task_name}: API Rate Limit/Quota hit. Pausing {int(quota_delay)}s...")
                time.sleep(quota_delay)
                
            # 2. Database Write Collisions
            elif "locked" in error_str:
                safe_print(f" {task_name}: Database lock collision. Waiting for disk queue...")
                time.sleep(15)
                
            # 3. THE FIX: Context Length Exceeded Recovery (Simple Truncation)
            elif "400" in error_str and ("context_length_exceeded" in error_str or "maximum context length" in error_str):
                shrink_attempts += 1
                
                # --- EXPLICIT GIVE UP CLAUSE ---
                if shrink_attempts > max_shrink:
                    safe_print(f" {task_name}: FATAL. Max truncation attempts ({max_shrink}) reached. Aborting module so Master Orchestrator can proceed.")
                    return f"Error in {task_name}: Context limits exhausted."

                safe_print(f" {task_name}: Context limit exceeded (Truncation attempt {shrink_attempts}/{max_shrink}). Simple truncation of last document...")
                
                try:
                    state = subagent_app.get_state(run_config)
                    if state.values and "messages" in state.values:
                        
                        # TARGET: Always the LAST tool message (the fetch that broke the limit)
                        fetch_msgs =[m for m in state.values["messages"] if getattr(m, 'type', '') == 'tool']
                        if fetch_msgs:
                            last_tool = fetch_msgs[-1]
                            content = str(last_tool.content)
                            
                            if len(content) > 1000:
                                # Keep only the first 1/3 of the content as a simple truncation
                                kept_content = content[:len(content)//3]
                                    
                                kept_content += "\n\n[Note: Content forcefully truncated to fit context limits.]"
                                
                                # Overwrite the massive message in the database with the shrunken one
                                last_tool.content = kept_content
                                subagent_app.update_state(run_config, {"messages": [last_tool]})
                                
                                safe_print(f" {task_name}: Truncated last document from {len(content)} to {len(kept_content)} chars. Retrying immediately...")
                                continue # Loop back and retry the main LLM call!
                            else:
                                # Edge Case: The last message is already tiny, but we are STILL over the limit.
                                # This means the *accumulated* history is too large. We must discard the oldest tool result.
                                safe_print(f" {task_name}: Last document is too small to shrink. Dropping oldest tool result to free memory...")
                                valid_tools =[m for m in fetch_msgs if "[Content dropped" not in str(m.content)]
                                if valid_tools:
                                    oldest_tool = valid_tools[0]
                                    oldest_tool.content = "[Content dropped to save context memory]"
                                    subagent_app.update_state(run_config, {"messages": [oldest_tool]})
                                    continue
                                else:
                                    safe_print(f" {task_name}: FATAL. Cannot shrink further. Aborting.")
                                    return f"Error in {task_name}: Context history unresolvable."
                        else:
                            safe_print(f" {task_name}: No tool messages found to shrink. Retrying in 15s...")
                            time.sleep(15)
                    else:
                        time.sleep(15)
                except Exception as shrink_e:
                    safe_print(f" {task_name}: Failed to shrink context ({shrink_e}). Retrying in 15s...")
                    time.sleep(15)
                    
            # 4. Standard Network Drops
            else:
                safe_print(f" {task_name}: Network dropped. State saved. Retrying in 15s... ({e})")
                time.sleep(15)
    
    
# --- Vision and Master Orchestrator ---
class VisionImageAgent:
    def __init__(self, api_key, base_url, model_name):
        self.llm = ChatOpenAI(model=model_name, api_key=api_key, base_url=base_url, temperature=0.1, max_retries=5, timeout=120)
        try:
            self.ddgs = DDGS()
        except Exception as e:
            safe_print(f" [!] Warning: Initial image search session instantiation failed: {e}")
            self.ddgs = None

    def find_and_verify_single_image(self, query, master_context):
        global_settings = load_global_settings()
        max_retries = int(global_settings.get("research_image_retries", 1))
        results = []
        
        for attempt in range(1, max_retries + 1):
            if not self.ddgs:
                try:
                    self.ddgs = DDGS()
                except Exception:
                    pass

            try:
                if self.ddgs:
                    results = list(self.ddgs.images(query, max_results=8))
                else:
                    results = []

                if results:
                    break
                else:
                    safe_print(f" [!] Image search attempt {attempt}/{max_retries} returned no results.")
            except Exception as e:
                safe_print(f" [!] Image search attempt {attempt}/{max_retries} failed: {e}")
            
            if attempt < max_retries:
                try:
                    self.ddgs = DDGS()
                except Exception as init_err:
                    safe_print(f" [!] Session regeneration failed: {init_err}")
                    self.ddgs = None
                
                sleep_time = (attempt * 2.0) + random.uniform(0.1, 0.9)
                time.sleep(sleep_time)

        if not results:
            return None

        # 2. Iterate and download candidates sequentially
        for r in results:
            url = r['image']
            try:
                resp = requests.get(url, timeout=5)
                resp.raise_for_status()  # Fast fail on 403 Forbidden / 406 Not Acceptable
                img = Image.open(io.BytesIO(resp.content))
                if img.size[0] < 320 or img.size[1] < 240: continue

                b64_resampled = self._resample_for_model(resp.content)
                if not b64_resampled: continue
                
                check_msg = {
                    "role": "user",
                    "content":[
                        {
                            "type": "text", 
                            "text": f"""
                            Is this image a high-quality, professional visual for the given context?
                            
                            MASTER CONTEXT:
                            {master_context}
                            
                            REJECTION CRITERIA (Respond NO if any apply):
                            1. It is a screenshot of a research paper or document text.
                            2. It is text-heavy (charts are okay, walls of text are not).
                            3. It is only vaguely related to the technical core of the context.
                            
                            If it is a PERFECT match, respond: 'YES: [detailed caption for the image]'.
                            Otherwise, respond: 'NO'.
                            """
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_resampled}"}}
                    ]
                }
                
                res = resilient_invoke(self.llm, [check_msg]).content.strip()
                if res.upper().startswith("YES"):
                    caption = res.split(":", 1)[1].strip() if ":" in res else f"Visualization of {query}"
                    return {"url": url, "description": caption}
            except Exception:
                continue
        return None

    def _resample_for_model(self, image_bytes):
        try:
            img = Image.open(io.BytesIO(image_bytes))
            if img.mode != 'RGB': img = img.convert('RGB')
            img_resampled = img.resize((320, 240), Image.Resampling.LANCZOS)
            buffered = io.BytesIO()
            img_resampled.save(buffered, format="JPEG")
            return base64.b64encode(buffered.getvalue()).decode('utf-8')
        except: return None

class MasterOrchestrator:
    def __init__(self, api_key, base_url, model_name):
        self.llm = ChatOpenAI(model=model_name, api_key=api_key, base_url=base_url, temperature=0, max_retries=5, timeout=120, model_kwargs={"reasoning_effort": "high"})
    
    def generate_project_name(self, query: str, clarifications: str):
        # Removed tool_choice
        model_with_tools = self.llm.bind_tools([ProjectName])
        prompt = (
            f"Initial Query: {query}\n"
            f"Clarifications: {clarifications}\n"
            "TASK: You MUST call the 'ProjectName' tool to provide a professional project title. "
            "Do not respond with plain text. Use the tool."
        )
        res = resilient_invoke(model_with_tools, [SystemMessage(content=prompt), HumanMessage(content="Please proceed.")])
        
        # Fallback if model ignores tool calling
        if res.tool_calls: 
            return res.tool_calls[0]["args"].get("title", "Research_Project")
        return sanitize_filename(res.content[:50]) if res.content else "Research_Project"

    def plan_research(self, initial_query: str, clarifications: str):
        # Removed tool_choice
        model_with_tools = self.llm.bind_tools([ResearchPlan])
        plan_prompt = (
            f"{get_gmt_string()}\n"
            f"Initial Request: {initial_query}\n"
            f"Clarifications: {clarifications}\n"
            "TASK: You MUST call the 'ResearchPlan' tool to break this into 3-10 modules. "
            "Do not provide a text response. You MUST use the tool."
        )
        
        for attempt in range(3):
            try:
                res = resilient_invoke(model_with_tools, [SystemMessage(content=plan_prompt), HumanMessage(content="Please proceed.")])
                if res.tool_calls:
                    tasks = res.tool_calls[0]["args"].get("tasks", [])
                    if tasks: return tasks
                # Fallback: If model sent text, try to regex it or just try again
                time.sleep(2)
            except Exception: pass
        return []
    
    def execute_subagents(self, tasks: List[Dict], output_dir=".", config=None, batch_id: int = 0):
        if not tasks: return[]
        for i, t in enumerate(tasks):
            letter = chr(65 + i)
            if not t['task_name'].startswith("ANNEXURE"):
                t['task_name'] = f"ANNEXURE {letter} - {t['task_name']}"
        
        safe_print(f"\n[ORCHESTRATOR] Dispatching {len(tasks)} sub-agents...")
        results =[]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = {
                executor.submit(run_subagent, t['search_prompt'], t['task_name'], output_dir, config, batch_id): t['task_name'] 
                for t in tasks
            }
            
            while futures:
                check_abort()
                done, not_done = concurrent.futures.wait(futures.keys(), timeout=1.0, return_when=concurrent.futures.FIRST_COMPLETED)
                
                for future in done:
                    task_name = futures.pop(future)
                    try:
                        data = future.result()
                        if isinstance(data, str) and not data.startswith("Error in"):
                            results.append({"task": task_name, "content": data})
                    except Exception as exc:
                        if "aborted" in str(exc).lower() or "interrupted" in str(exc).lower():
                            executor.shutdown(wait=False, cancel_futures=True)
                            raise exc
                        safe_print(f" [EXCEPTION] {task_name}: {exc}")
                
        return results

    def finalize_report(self, original_query: str, all_results: List[Dict], project_title: str, vision_config=None):
        safe_print("\n[ORCHESTRATOR] Synthesizing final master report...")
        combined_context = ""
        for r in all_results:
            combined_context += f"\n\n--- {r['task']} ---\n{r['content']}\n"
    
        image_assets = []
        search_history =[]
        
        global_settings = load_global_settings()
        vision_pipeline_enabled = global_settings.get("research_image_system_enabled", False)
        is_android_sys = platform.system() == "Android"
        
        if vision_config and vision_config.get("enabled") and vision_pipeline_enabled and not is_android_sys:
            try:
                vision_agent = VisionImageAgent(vision_config["api_key"], vision_config["base_url"], vision_config["model_name"])
                max_images = int(global_settings.get("research_images_max", 10))
                for i in range(max_images):
                    check_abort()
                    query_prompt = f"REPORT DATA: {combined_context}\nPREVIOUS: {json.dumps(search_history)}\nTASK: Generate a SINGLE image search query for the NEXT image. Keep the query as short and generic as possible while still matching the overall context. Return ONLY the search query text."
                    query = resilient_invoke(self.llm, [SystemMessage(content=query_prompt), HumanMessage(content="Please proceed.")]).content.strip().replace('"', '')
                    safe_print(f" [MASTER] Turn {i+1}: Generating search for '{query}'")
                    
                    asset = vision_agent.find_and_verify_single_image(query, combined_context)
                    check_abort()
                    if asset:
                        image_assets.append(asset)
                        search_history.append({"query": query, "result": asset['description']})
                        safe_print(f" [SUCCESS] Asset {len(image_assets)} confirmed.")
                    else:
                        search_history.append({"query": query, "result": "REJECTED"})
            except Exception as e:
                if "aborted" in str(e).lower() or "interrupted" in str(e).lower():
                    raise e
                safe_print(f" [LIMIT/ERROR] Image loop salvaged at {len(image_assets)} assets: {e}")

        assets_md = "\n".join([f"- ![{a['description']}]({a['url']})" for a in image_assets])
        synthesis_prompt = f"""
        # {original_query}
        DATA: \n{combined_context}\n
        AVAILABLE VISUAL ASSETS: \n{assets_md}\n
        INSTRUCTIONS:
        1. Write a massive, professional technical report, incorporating the findings from each ANNEXURE provided in the data.
        2. You MUST integrate the images provided in the assets list into the flow of the text.
        3. Place the Markdown image tag immediately after the paragraph that references it.
        """
        
        report_content = ""
        for attempt in range(3):
            check_abort()
            res = resilient_invoke(self.llm, [SystemMessage(content=synthesis_prompt), HumanMessage(content="Please proceed.")])
            report_content = res.content if res.content else ""
            
            report_lower = report_content.lower()
            has_references = "reference" in report_lower or "sources" in report_lower
            ends_cleanly = report_content.strip()[-1] in [".", "!", "?", "”", '"', "*", "\n"] if report_content.strip() else False
            
            # Simple length, reference, and completion validation check
            if len(report_content) > 10000 and has_references and ends_cleanly:
                break
            else:
                safe_print(f" [ORCHESTRATOR] Final report failed validation (Length: {len(report_content)} chars, Ends cleanly: {ends_cleanly}, Has references: {has_references}). Retrying full rewrite...")
                
        return report_content

@tool
def perform_research(topic: str, specific_instructions: str = "") -> str:
    """
    Conducts deep, multi-agent parallel research on a given topic using an autonomous orchestrator.
    Generates a comprehensive PDF and Markdown report with citations and optional images.
    Use this when the user asks for a deep dive, comprehensive report, or thorough research on a complex subject.
    """
    try:
        # 1. Base Defaults (Switched away from stepfun)
        api_key, base_url, model = "", "https://openrouter.ai/api/v1", "google/gemini-2.5-flash:free"
        max_agents = load_global_settings().get("max_research_agents", 4)
        vision_enabled = True
        vision_model = "nvidia/nemotron-nano-12b-v2-vl:free"
        
        # 2. DYNAMICALLY LOAD FROM ACTIVE MULTI-AGENT IN UI
        batch_id = getattr(thread_context, "batch_id", 0)
        if CURRENT_APP:
            try:
                agent_view = CURRENT_APP.query_one("AIAgentView")
                if hasattr(agent_view, "active_agent"):
                    agent = agent_view.active_agent
                    if agent.use_backup and agent.backup_model:
                        model = agent.backup_model
                        base_url = agent.backup_base_url or agent.base_url
                        api_key = agent.get_backup_api_key() or agent.get_api_key()
                    else:
                        model = agent.model
                        base_url = agent.base_url
                        api_key = agent.get_api_key()
                    vision_enabled = agent.is_capable_vision
                    vision_model = model if vision_enabled else vision_model
            except Exception as e:
                log_tool(f"[dim red]Config fetch error: {e}[/dim red]")

        # 3. Fallback to old agent_config.json ONLY if API key is missing
        config_path = "agent_config.json"
        if not api_key and os.path.exists(config_path):
            with open(config_path, "r") as f:
                data = json.load(f)
                api_key = data.get("api_key", api_key)
                base_url = data.get("base_url", base_url)
                model = data.get("model", model)
                max_agents = int(data.get("max_agents", max_agents))
                vision_enabled = data.get("vision_enabled", vision_enabled)
                vision_model = data.get("vision_model", vision_model)
                
        if not api_key:
            return "Error: API Key is missing. Please configure it in the IDE."
            
        master = MasterOrchestrator(api_key, base_url, model)
        
        safe_base_dir, _ = get_safe_path(".")
        raw_title = master.generate_project_name(topic, specific_instructions)
        project_title = sanitize_filename(raw_title)
        timestamp = datetime.datetime.now().strftime("%y%m%d_%H%M")
        folder_name = f"{project_title}_{timestamp}_{uuid.uuid4().hex[:6]}"
        research_root = get_storage_path(safe_base_dir, "research")
        os.makedirs(research_root, exist_ok=True)
        output_dir = get_storage_path(research_root, folder_name)
        os.makedirs(output_dir, exist_ok=True)
        
        log_tool(f"Starting Deep Research Plan for '{topic}'...")
        tasks = master.plan_research(topic, specific_instructions)
        if not tasks:
            return "Failed to generate a research plan. Please try again or rephrase the topic."
            
        tasks_to_run = tasks[:max_agents]
        
        # --- CRITICAL FIX: Pre-apply 'ANNEXURE' renaming so UI IDs match Executor IDs perfectly ---
        for i, t in enumerate(tasks_to_run):
            letter = chr(65 + i)
            if not t['task_name'].startswith("ANNEXURE"):
                t['task_name'] = f"ANNEXURE {letter} - {t['task_name']}"
        
        # --- Mount Live UI Progress Bars ---
        try:
            if CURRENT_APP:
                CURRENT_APP.query_one("#ai_agent_view").mount_progress([t['task_name'] for t in tasks_to_run])
        except Exception as e: 
            log_tool(f"[dim red]UI Tracking Setup Error: {e}[/dim red]")

        agent_config = {"api_key": api_key, "base_url": base_url, "model_name": model}
        sub_reports = master.execute_subagents(tasks_to_run, output_dir=output_dir, config=agent_config, batch_id=batch_id) 
        
        if not sub_reports:
            try:
                if CURRENT_APP: CURRENT_APP.query_one("#ai_agent_view").hide_progress()
            except Exception: pass
            return "All research modules failed to return data."
            
        vision_config = {
            "enabled": vision_enabled,
            "api_key": api_key,
            "base_url": base_url,
            "model_name": vision_model
        }
        
        # Inform the user in the logs that Synthesis has begun
        log_tool("[bold magenta]Master Orchestrator:[/bold magenta] Synthesis and image gathering begun...")
        
        final_report = master.finalize_report(topic, sub_reports, project_title, vision_config=vision_config)
        
        md_path = get_storage_path(output_dir, f"{project_title}.md")
        pdf_path = get_storage_path(output_dir, f"{project_title}.pdf")
        
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(final_report)
            
        render_markdown_to_pdf(md_path, pdf_path)
        # --- Send to Telegram ---
        if CURRENT_APP:
            try:
                agent_view = CURRENT_APP.query_one("#ai_agent_view")
                chat_id = getattr(agent_view, "current_telegram_chat_id", None)
                if chat_id:
                    tm = agent_view.telegram_manager
                    # Send only the summary bubble + voiceover + final PDF
                    tm.send_voiceover(chat_id, 
                                     full_text=final_report, 
                                     notification_text=f" FINAL CONSOLIDATED REPORT READY: {project_title}")
                    tm.send_document(chat_id, pdf_path, caption=f"Consolidated Report PDF: {project_title}")
            except: pass
        # --- Tear Down UI Progress Bars ---
        try:
            if CURRENT_APP:
                CURRENT_APP.query_one("#ai_agent_view").hide_progress()
        except Exception: pass
        
        return f"Deep research completed successfully! Saved in directory: {folder_name}\n\n--- FINAL REPORT CONTENT ---\n{final_report}"
        
    except Exception as e:
        error_str = str(e)
        if "interrupted" in error_str.lower() or "aborted" in error_str.lower():
            raise e
            
        try:
            if CURRENT_APP: CURRENT_APP.query_one("#ai_agent_view").hide_progress()
        except Exception: pass
        return f"An error occurred during deep research: {str(e)}\n{traceback.format_exc()}"
        

def get_go_state_path():
    """Locates the Fyne Go App's global state file found via find command."""
    app_id = "com.local.devplanner.v4"
    filename = "dev_state_v4.json"
    sys_os = platform.system()
    
    home = os.path.expanduser("~")
    
    if sys_os == "Darwin": # macOS
        possible_paths = [
            # THIS IS WHERE YOUR FIND COMMAND LOCATED IT:
            get_storage_path(home, "Library/Preferences/fyne", app_id, filename),
            # Backup common Fyne paths:
            get_storage_path(home, "Library/Application Support/fyne", app_id, filename),
            get_storage_path(home, "Library/Application Support", app_id, filename)
        ]
    elif sys_os == "Windows":
        appdata = os.environ.get("APPDATA", "")
        possible_paths = [
            get_storage_path(appdata, "fyne", app_id, filename),
            get_storage_path(appdata, app_id, filename)
        ]
    else: # Linux
        possible_paths = [
            get_storage_path(home, ".local/share/fyne", app_id, filename),
            get_storage_path(home, ".local/share", app_id, filename)
        ]

    for path in possible_paths:
        if os.path.exists(path):
            return path
            
    return None

@tool
def send_file_to_telegram(filepath: str, caption: str = "") -> str:
    """
    Sends a file from the local workspace to the active Telegram user.
    Use this tool when the user asks you on Telegram to send them a file, report, script, image, or dataset.
    """
    try:
        if not CURRENT_APP:
            return "Error: System UI not active."
        
        agent_view = CURRENT_APP.query_one("#ai_agent_view")
        tm = getattr(agent_view, "telegram_manager", None)
        chat_id = getattr(agent_view, "current_telegram_chat_id", None)
        
        if not tm or not tm.bot_token or not tm.is_running:
            return "Error: Telegram bot is not connected or active."

        # Master Check from /telegram menu
        if not getattr(tm, "allow_file_sending", False):
            return "Sending files to Telegram is disabled. Please enable this from /telegram menu first."

        if not chat_id:
            return "Error: No active Telegram chat session found."

        safe_path, display_path = get_safe_path(filepath)
        if not os.path.exists(safe_path):
            return f"Error: File '{filepath}' not found in workspace."

        tm.send_document(chat_id, safe_path, caption=caption or f"File: {display_path}")
        return f"Successfully sent '{display_path}' to Telegram."
    except Exception as e:
        return f"Error sending file to Telegram: {e}"

@tool
def manage_agenda(action: str, agenda_data: str = "", project_name: str = "") -> str:
    """
    Manages the local agenda. 
    Actions:
    - "read": Get local goals.json.
    - "write": Save to local goals.json.
    - "import_from_gui": List all projects/features in the Go DevPlanner.
    - "fetch_external_data": Takes a 'project_name' from the Go list, finds its path, 
      and returns that project's specific features and its remote 'goals.json' (if it exists).
    """
    try:
        safe_path, display_path = get_safe_path("goals.json")
        
        # 1. Standard Read/Write (Local Sandbox Only)
        if action == "read":
            if not os.path.exists(safe_path): return "Local goals.json not found."
            with open(safe_path, 'r', encoding='utf-8') as f: return f.read()
            
        elif action == "write":
            parsed = json.loads(agenda_data)
            with open(safe_path, 'w', encoding='utf-8') as f:
                f.write(json.dumps(parsed, indent=4))
            return "Successfully updated local goals.json"

        # 2. The "Hardcoded Pipe" to the outside world
        go_state_file = get_go_state_path()
        if not go_state_file or not os.path.exists(go_state_file):
            return "Go DevPlanner state file not found."
            
        with open(go_state_file, 'r', encoding='utf-8') as f:
            go_state = json.load(f)

        if action == "import_from_gui":
            # Just returns the list of project names and their high-level features
            projects = go_state.get("projects", [])
            summary = [{"name": p['name'], "features": p.get('features', [])} for p in projects]
            return json.dumps(summary, indent=2)

        elif action == "fetch_external_data":
            # Finds the absolute path of a named project and reads its metadata
            target_proj = next((p for p in go_state.get("projects", []) if p['name'] == project_name), None)
            if not target_proj: return f"Project '{project_name}' not found in Go Planner."
            
            remote_path = target_proj.get("path", "")
            remote_goals_file = get_storage_path(remote_path, "goals.json")
            
            result = {
                "gui_features": target_proj.get("features", []),
                "remote_goals_json": None
            }
            
            if os.path.exists(remote_goals_file):
                with open(remote_goals_file, 'r', encoding='utf-8') as f:
                    result["remote_goals_json"] = json.loads(f.read())
            
            return json.dumps(result, indent=2)

        return "Invalid action."
    except Exception as e:
        return f"Agenda Error: {e}"




_MEMORY_FILE_LOCK = threading.Lock()

def _load_memory_json(filepath: str) -> list:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []

def _save_memory_json(filepath: str, data: list):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

@tool
def update_core_memory(section: str, subject: str = "", content: str = "", id: str = "", config: RunnableConfig = None) -> str:
    """
    Manages core memorylets in USER.json or MEMORY.json.
    
    ROUTING RULES:
    - section='USER': User traits, role, background, preferences.
    - section='MEMORY': Facts about project, environment, codebase, architecture, or rules.
    
    OPERATIONS:
    1. ADD: Pass section, subject, and content. Leave 'id' empty ("").
    2. EDIT: Pass section, target 'id', updated subject, and updated content.
    3. DELETE: Pass section and target 'id', with content="" (empty string).
    """
    agent_name = _get_agent(config) if config else "Agent"
    sec = section.upper().strip() if section else ""
    
    # Early Guard 1: Invalid Section
    if sec not in ["MEMORY", "USER"]:
        return f"[{agent_name}] Error: 'section' must be 'MEMORY' or 'USER'."
        
    # Clean and sanitize string parameters
    target_id = id.strip() if id and id.lower() not in ["null", "none", "undefined"] else ""
    text_content = content.strip()
    text_subject = subject.strip()
    
    # Early Guard 2: Complete Blank Call
    if not target_id and not text_subject and not text_content:
        return f"[{agent_name}] Error: Invalid parameters. Provide 'content' to add a new memorylet, or an 'id' to edit/delete."

    # Early Guard 3: Attempting to ADD without content
    if not target_id and not text_content:
        return f"[{agent_name}] Error: 'content' is required when creating a new memorylet."

    # Thread lock prevents race conditions when LLM invokes multiple tool calls simultaneously
    with _MEMORY_FILE_LOCK:
        memory_dir = get_storage_path("agents", "memory", agent_name)
        file_path = get_storage_path(memory_dir, f"{sec}.json")
        memories = _load_memory_json(file_path)
        
        # 1. DELETE OPERATION (Target ID provided, content is empty)
        if target_id and not text_content:
            initial_count = len(memories)
            memories = [m for m in memories if m.get("id") != target_id]
            if len(memories) == initial_count:
                return f"[{agent_name}] Error: Memorylet with ID '{target_id}' not found in {sec}.json."
            _save_memory_json(file_path, memories)
            return f"[{agent_name}] Memorylet '{target_id}' deleted from {sec}.json."
            
        # 2. EDIT OPERATION (Target ID provided, content is non-empty)
        elif target_id:
            found = False
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for m in memories:
                if m.get("id") == target_id:
                    if text_subject:
                        m["subject"] = text_subject
                    m["content"] = text_content
                    m["timestamp"] = now_str
                    found = True
                    break
            if not found:
                return f"[{agent_name}] Error: Memorylet with ID '{target_id}' not found in {sec}.json. Omit 'id' to create a new memorylet."
            _save_memory_json(file_path, memories)
            return f"[{agent_name}] Memorylet '{target_id}' updated in {sec}.json."
            
        # 3. ADD OPERATION (No Target ID provided, content is non-empty)
        else:
            # Simple auto-increment serial integer ID: "1", "2", "3", ...
            existing_ids = [int(m["id"]) for m in memories if str(m.get("id", "")).isdigit()]
            new_id = str(max(existing_ids) + 1) if existing_ids else "1"
            
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            memorylet = {
                "id": new_id,
                "subject": text_subject or "General",
                "content": text_content,
                "timestamp": now_str
            }
            memories.append(memorylet)
            _save_memory_json(file_path, memories)
            return f"[{agent_name}] Memorylet '{new_id}' successfully saved to {sec}.json."

@tool
def save_skill(skill_name: str, content: str, config: RunnableConfig) -> str:
    """Saves a procedural skill (playbook/steps) for future use."""
    agent_name = _get_agent(config)
    skills_dir = get_storage_path("agents", "skills", agent_name)
    os.makedirs(skills_dir, exist_ok=True)
    
    safe_name = "".join([c if c.isalnum() or c == '_' else "_" for c in skill_name])
    path = get_storage_path(skills_dir, f"{safe_name}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Skill '{safe_name}' saved to {agent_name}'s procedural memory library."

@tool
def prepare_active_skill(tool_name: str, source_paths: List[str], entry_point: str, test_input: Dict[str, Any], dependencies: List[str] = None, custom_dependency_paths: List[str] = None, pre_install_commands: List[str] = None, arg_order: List[str] = None, config: RunnableConfig = None) -> str:
    """
    Stage 1 of Learning a new Executable Tool. Sets up environment and performs a test run.
    The tool is placed in 'staged_tools/' and NOT registered yet.

    Args:
        tool_name: Unique name for the tool.
        source_paths: Paths to the core logic files.
        entry_point: The main file to execute.
        test_input: A dictionary of sample inputs for the validation run.
        dependencies: List of PyPI packages to install.
        custom_dependency_paths: List of absolute paths to local source dependencies (e.g., custom libraries) to build/install.
        pre_install_commands: List of shell commands to run BEFORE installing dependencies (e.g., for building C++ deps with CMake).
        arg_order: Optional list for positional logic.
    """
    agent_name = _get_agent(config)
    
    # --- MODE RESTRICTIONS ---
    if CURRENT_AGENT_VIEW:
        mode = getattr(CURRENT_AGENT_VIEW, "agent_mode", "PLAN")
        if mode == "PLAN":
            return "Error: Skill staging and compilation is not allowed in SAFE (PLAN) mode. Please switch to SEMI-AUTO or FULL-AUTO mode."
        elif mode == "INTERMEDIATE":
            kwargs = {
                "tool_name": tool_name,
                "source_paths": source_paths,
                "entry_point": entry_point,
                "test_input": test_input,
                "dependencies": dependencies,
                "custom_dependency_paths": custom_dependency_paths,
                "pre_install_commands": pre_install_commands,
                "arg_order": arg_order
            }
            if not CURRENT_AGENT_VIEW.confirm_tool_execution("prepare_active_skill", kwargs, agent_name=agent_name):
                return "Error: Tool execution of 'prepare_active_skill' was rejected by the user."

    safe_agent_name = agent_name.replace(" ", "_")
    staging_dir = get_storage_path("agents", "skills", safe_agent_name, "staged_tools", tool_name)
    
    if os.path.exists(staging_dir):
        import shutil
        shutil.rmtree(staging_dir)
        
    os.makedirs(get_storage_path(staging_dir, "logic"), exist_ok=True)
    
    try:
        # 1. Copy Files
        import shutil
        for sp in source_paths:
            src_abs, _ = get_safe_path(sp)
            if os.path.isdir(src_abs):
                shutil.copytree(src_abs, get_storage_path(staging_dir, "logic"), dirs_exist_ok=True)
            else:
                shutil.copy2(src_abs, get_storage_path(staging_dir, "logic"))
        
        # 2. Setup Venv
        venv_dir = get_storage_path(staging_dir, "venv")
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        python_exe = _get_venv_python(venv_dir)
        
        env = os.environ.copy()
        venv_bin = os.path.dirname(python_exe)
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = staging_dir
        env["GOWORK"] = "off" # Ensure Go builds inside skills are isolated

        # 3. Pre-Install Commands (Custom build logic)
        if pre_install_commands:
            log_tool(f"Running pre-install commands for {tool_name}...")
            for cmd_str in pre_install_commands:
                log_tool(f"  Executing: {cmd_str}")
                # We run these commands in the logic directory
                subprocess.run(cmd_str, shell=True, check=True, env=env, cwd=get_storage_path(staging_dir, "logic"), stdin=subprocess.DEVNULL, capture_output=True, text=True)

        # 4. Install Dependencies
        # Phase 1: PyPI
        if dependencies:
            log_tool(f"Installing PyPI dependencies for {tool_name}: {', '.join(dependencies)}")
            subprocess.run([python_exe, "-m", "pip", "install"] + dependencies, check=True, capture_output=True, env=env, stdin=subprocess.DEVNULL)
            
        # Phase 2: Custom Source Paths
        if custom_dependency_paths:
            for cp in custom_dependency_paths:
                log_tool(f"Installing custom source dependency: {cp}")
                subprocess.run([python_exe, "-m", "pip", "install", cp], check=True, capture_output=True, env=env, stdin=subprocess.DEVNULL)

        # 5. Test Run
        log_tool(f"Performing validation run for {tool_name}...")
        script_path = get_storage_path(staging_dir, "logic", entry_point)
        
        env = os.environ.copy()
        venv_bin = os.path.dirname(python_exe)
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = staging_dir

        cmd = [python_exe if entry_point.endswith(".py") else "sh", script_path]
        
        # --- ARGUMENT PASSING: Hybrid Logic ---
        if arg_order:
            # SAFETY CHECK: Catch partial arg_order configurations before running
            mismatched = [k for k in test_input.keys() if k not in arg_order]
            if mismatched:
                return (
                    f"Error: Parameter(s) {mismatched} were provided in 'test_input' "
                    f"but are missing from your specified 'arg_order' list: {arg_order}.\n"
                    f"To fix this:\n"
                    f"- If you want to use Positional Mode, every parameter in 'test_input' must have a position defined in 'arg_order'.\n"
                    f"- If you want to use Keyword Mode (passing flags like --param), do NOT specify an 'arg_order' list at all."
                )

            for arg_name in arg_order:
                val = test_input.get(arg_name)
                if val is not None:
                    if isinstance(val, list):
                        # Variadic Spreading: ["a", "b"] -> ... a b
                        cmd.extend([str(item) for item in val])
                    else:
                        cmd.append(str(val))
        else:
            # Keyword Mode: Pass complex objects as JSON strings, simple ones as-is
            for k, v in test_input.items():
                # SAFETY NORMALIZATION: Strip any leading dashes from the key to prevent "----flag" compounding
                clean_k = k.lstrip("-")
                
                # SMART FLAG PARSING: If value is boolean, treat as standard CLI switch/flag (e.g. --info)
                if isinstance(v, bool):
                    if v:
                        cmd.append(f"--{clean_k}")
                    # If False, omit the flag entirely (standard CLI behavior)
                elif isinstance(v, list):
                    # HYBRID LIST GUARD: Unpack simple file/string lists, but keep JSON dumps for complex nested arrays
                    if all(isinstance(item, (str, int, float)) for item in v):
                        cmd.append(f"--{clean_k}")
                        cmd.extend([str(item) for item in v])
                    else:
                        val_str = json.dumps(v)
                        cmd.extend([f"--{clean_k}", val_str])
                else:
                    val_str = json.dumps(v) if isinstance(v, dict) else str(v)
                    cmd.extend([f"--{clean_k}", val_str])
        # --------------------------------------
        
        proc = subprocess.run(cmd, text=True, capture_output=True, cwd=get_storage_path(staging_dir, "logic"), env=env, stdin=subprocess.DEVNULL)
        
        stdout = proc.stdout
        stderr = proc.stderr
        if isinstance(stdout, bytes): stdout = stdout.decode('utf-8', errors='replace')
        if isinstance(stderr, bytes): stderr = stderr.decode('utf-8', errors='replace')

        # Evaluate Test Success
        test_passed = False
        executed_cmd = " ".join(cmd)
        result = f"--- TEST RUN RESULTS ({tool_name}) ---\nCOMMAND EXECUTED:\n{executed_cmd}\n\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}\n\n"
        if proc.returncode == 0:
            if not stdout.strip():
                result += "FAILURE: Tool executed with exit code 0 but returned EMPTY stdout. A valid tool must print some output or confirmation to STDOUT during the test validation run."
            else:
                test_passed = True
                result += "SUCCESS: Tool executed correctly. Call 'finalize_active_skill' to register it."
        else:
            result += f"FAILURE: Tool exited with code {proc.returncode}. Fix the logic and try again."

        # Save metadata and test_input for Stage 2 auto-schema generation
        meta = {
            "entry_point": entry_point, 
            "dependencies": dependencies or [],
            "custom_dependency_paths": custom_dependency_paths or [],
            "pre_install_commands": pre_install_commands or [],
            "arg_order": arg_order,
            "test_input": test_input,
            "test_passed": test_passed
        }
        with open(get_storage_path(staging_dir, "metadata.json"), "w") as f:
            json.dump(meta, f)
            
        return result
        
    except subprocess.CalledProcessError as e:
        if os.path.exists(staging_dir):
            import shutil
            shutil.rmtree(staging_dir)
        err_msg = f"Command '{e.cmd}' returned non-zero exit status {e.returncode}."
        if e.stderr:
            err_msg += f"\nSTDERR:\n{e.stderr}"
        if e.stdout:
            err_msg += f"\nSTDOUT:\n{e.stdout}"
        return f"Error during staging: {err_msg}"
    except Exception as e:
        if os.path.exists(staging_dir): 
            import shutil
            shutil.rmtree(staging_dir)
        return f"Error during staging: {e}"

@tool
def finalize_active_skill(tool_name: str, tool_description: str, usage_guide: str, arg_order: List[str] = None, config: RunnableConfig = None) -> str:
    """
    Stage 2 of Learning a new Executable Tool. Commits a successfully tested tool to the live library.
    No JSON schema required!
    
    Args:
        tool_name: Unique name for the tool.
        tool_description: Concise overview of what the tool does.
        usage_guide: COMPULSORY. A comprehensive manual in Markdown format with usage examples.
        arg_order: Optional sequence list for positional arguments (e.g. ['param1', 'param2']). Omit for keyword flags mode.
    """
    agent_name = _get_agent(config)
    
    # --- MODE RESTRICTIONS ---
    if CURRENT_AGENT_VIEW:
        mode = getattr(CURRENT_AGENT_VIEW, "agent_mode", "PLAN")
        if mode == "PLAN":
            return "Error: Skill finalization is not allowed in SAFE (PLAN) mode. Please switch to SEMI-AUTO or FULL-AUTO mode."
        elif mode == "INTERMEDIATE":
            kwargs = {
                "tool_name": tool_name,
                "tool_description": tool_description,
                "usage_guide": usage_guide,
                "arg_order": arg_order
            }
            if not CURRENT_AGENT_VIEW.confirm_tool_execution("finalize_active_skill", kwargs, agent_name=agent_name):
                return "Error: Tool execution of 'finalize_active_skill' was rejected by the user."

    safe_agent_name = agent_name.replace(" ", "_")
    base_skills = get_storage_path("agents", "skills", safe_agent_name)
    staging_dir = get_storage_path(base_skills, "staged_tools", tool_name)
    active_dir = get_storage_path(base_skills, "active_tools", tool_name)
    
    if not os.path.exists(staging_dir):
        return f"Error: Tool '{tool_name}' is not in staging. Call 'prepare_active_skill' first."
        
    meta_path = get_storage_path(staging_dir, "metadata.json")
    staged_meta = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                staged_meta = json.load(f)
            if not staged_meta.get("test_passed", False):
                return f"Error: HARD GUARDRAIL ACTIVATED\nTool '{tool_name}' FAILED its validation test during `prepare_active_skill`. You MUST fix the logic and re-run `prepare_active_skill` until it succeeds."
        except Exception:
            pass

    if arg_order is None:
        arg_order = staged_meta.get("arg_order")

    try:
        # 1. Create schema.json without schema parameter boilerplate
        schema = {
            "name": tool_name, 
            "description": tool_description, 
            "arg_order": arg_order
        }
        with open(get_storage_path(staging_dir, "schema.json"), "w", encoding="utf-8") as f:
            json.dump(schema, f, indent=4)
            
        # 2. Mandatory: Create Usage Manual (Passive Skill)
        safe_manual_name = "".join([c if c.isalnum() or c == '_' else "_" for c in tool_name]) + "_manual"
        manual_path = get_storage_path(base_skills, f"{safe_manual_name}.md")
        with open(manual_path, "w", encoding="utf-8") as f:
            f.write(f"# Tool Manual: {tool_name}\n\n## Description\n{tool_description}\n\n## Usage Guide\n{usage_guide}")

        # 3. Move to active
        if os.path.exists(active_dir):
            import shutil
            shutil.rmtree(active_dir)
        os.makedirs(os.path.dirname(active_dir), exist_ok=True)
        os.rename(staging_dir, active_dir)
        
        # --- VERSION CONTROL: GIT INIT ---
        try:
            # 1. Init
            subprocess.run(["git", "init"], cwd=active_dir, check=True, capture_output=True)
            # 2. Gitignore
            with open(get_storage_path(active_dir, ".gitignore"), "w") as f:
                f.write("venv/\n__pycache__/\n*.pyc\n")
            # 3. Initial Commit
            subprocess.run(["git", "add", "."], cwd=active_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial registration"], cwd=active_dir, check=True, capture_output=True)
            git_status = "Version control initialized."
        except Exception as ge:
            git_status = f"Git Initialization failed: {ge}"
            
        return f"Success! {tool_name} is now a permanent capability. Usage manual saved as a passive skill. {git_status} Reset session to use it."
    except Exception as e:
        return f"Error finalizing skill: {e}"

@tool
def fix_active_skill(tool_name: str, action: str, documentation: str = None, tool_description: str = None, file_path: str = None, source_path: str = None, content: str = None, commit_message: str = None, dependencies: List[str] = None, arg_order: List[str] = None, start_line: int = None, end_line: int = None, config: RunnableConfig = None) -> str:
    """
    Allows editing and maintaining an existing Active Skill with automatic version control.
    
    Actions:
    - 'list': List all files in the tool's directory.
    - 'read': Read a specific file with line numbers (relative to tool root).
    - 'edit': Replace a range of lines (start_line to end_line) in a file and return a 20-line surrounding snippet.
    - 'replace': Replace an entire file's content or copy from source_path.
    - 'install': Add/Update pip dependencies in the tool's venv.
    - 'commit': Manually commit changes.
    
    Args:
        tool_name: The tool to maintain.
        action: The action to perform (list, read, edit, replace, install, commit).
        documentation: Optional manual markdown content to update.
        tool_description: Optional updated description summary.
        file_path: Target file inside tool library (e.g. 'script.py').
        source_path: For 'replace' action: Workspace path to copy code from.
        content: For 'edit' or 'replace' action: Code string to write or replace with.
        start_line: For 'edit' action: 1-indexed starting line number.
        end_line: For 'edit' action: 1-indexed ending line number.
        dependencies: For 'install' action: List of pip packages.
        arg_order: Optional sequence list for positional CLI arguments.
    """
    agent_name = _get_agent(config)
    
    # Determine if this call is strictly a read-only operation
    action_clean = (action or "").lower().strip()
    is_read_only = (
        action_clean in ["list", "read"] 
        and not documentation 
        and not tool_description 
        and arg_order is None 
        and not content 
        and not source_path 
        and not dependencies 
        and not commit_message
    )

    # --- MODE RESTRICTIONS ---
    if CURRENT_AGENT_VIEW and not is_read_only:
        mode = getattr(CURRENT_AGENT_VIEW, "agent_mode", "PLAN")
        if mode == "PLAN":
            return f"Error: Modifying active skills (action='{action}') is not allowed in SAFE (PLAN) mode. Please switch to SEMI-AUTO or FULL-AUTO mode."
        elif mode == "INTERMEDIATE":
            kwargs = {
                "tool_name": tool_name,
                "action": action,
                "documentation": documentation,
                "tool_description": tool_description,
                "file_path": file_path,
                "source_path": source_path,
                "content": content,
                "commit_message": commit_message,
                "dependencies": dependencies,
                "arg_order": arg_order,
                "start_line": start_line,
                "end_line": end_line
            }
            if not CURRENT_AGENT_VIEW.confirm_tool_execution("fix_active_skill", kwargs, agent_name=agent_name):
                return "Error: Tool execution of 'fix_active_skill' was rejected by the user."

    safe_agent_name = agent_name.replace(" ", "_")
    active_dir = get_storage_path("agents", "skills", safe_agent_name, "active_tools", tool_name)
    
    if not os.path.exists(active_dir):
        return f"Error: Tool '{tool_name}' not found."
        
    # Update documentation ONLY IF explicitly passed
    if documentation:
        try:
            safe_name = "".join([c if c.isalnum() or c == '_' else "_" for c in tool_name])
            manual_path = get_storage_path("agents", "skills", safe_agent_name, f"{safe_name}_manual.md")
            with open(manual_path, "w", encoding="utf-8") as f:
                f.write(documentation)
        except Exception as e:
            return f"Error updating manual: {e}"
        
    # Update schema.json ONLY IF tool_description or arg_order is explicitly passed
    if tool_description or arg_order is not None:
        try:
            schema_path = get_storage_path(active_dir, "schema.json")
            schema_data = {}
            if os.path.exists(schema_path):
                try:
                    with open(schema_path, "r", encoding="utf-8") as f:
                        schema_data = json.load(f)
                except Exception:
                    pass
            
            schema_data["name"] = tool_name
            if tool_description:
                schema_data["description"] = tool_description
            if arg_order is not None:
                schema_data["arg_order"] = arg_order
            
            os.makedirs(os.path.dirname(schema_path), exist_ok=True)
            with open(schema_path, "w", encoding="utf-8") as f:
                json.dump(schema_data, f, indent=4)
            
            subprocess.run(["git", "add", "schema.json"], cwd=active_dir, capture_output=True)
            subprocess.run(["git", "-c", "user.name='Maven'", "-c", "user.email='maven@internal'", "commit", "-m", f"Update schema.json for {tool_name}"], cwd=active_dir, capture_output=True)
        except Exception as e:
            return f"Error updating schema.json: {e}"
        
    try:
        if action_clean == "list":
            files = []
            for root, _, filenames in os.walk(active_dir):
                if "venv" in root or ".git" in root: continue
                for f in filenames:
                    rel_path = os.path.relpath(get_storage_path(root, f), active_dir)
                    if rel_path.startswith("logic/"):
                        rel_path = rel_path[6:]
                    files.append(rel_path)
            return "Files in tool directory:\n" + "\n".join(files)
            
        elif action_clean == "read":
            if not file_path: return "Error: 'file_path' required."
            if file_path in ["manual", "documentation"]:
                safe_name = "".join([c if c.isalnum() or c == '_' else "_" for c in tool_name])
                path = get_storage_path(os.path.dirname(os.path.dirname(active_dir)), f"{safe_name}_manual.md")
            else:
                path = get_storage_path(active_dir, file_path)
                if not os.path.exists(path):
                    logic_path = get_storage_path(active_dir, "logic", file_path)
                    if os.path.exists(logic_path):
                        path = logic_path
                    else:
                        return f"Error: File '{file_path}' not found."
            
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return format_numbered_lines(lines, start_line_num=1)

        elif action_clean == "edit" and start_line is not None and end_line is not None:
            if not file_path: return "Error: 'file_path' required."
            if content is None: return "Error: 'content' required for partial edit action."
            
            if file_path in ["manual", "documentation"]:
                safe_name = "".join([c if c.isalnum() or c == '_' else "_" for c in tool_name])
                target_path = get_storage_path(os.path.dirname(os.path.dirname(active_dir)), f"{safe_name}_manual.md")
            else:
                target_path = get_storage_path(active_dir, file_path)
                if not os.path.exists(target_path) and os.path.isdir(get_storage_path(active_dir, "logic")):
                    target_path = get_storage_path(active_dir, "logic", file_path)
            
            if not os.path.exists(target_path):
                return f"Error: File '{file_path}' not found."

            with open(target_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            start_idx = max(0, start_line - 1)
            end_idx = max(0, end_line)

            edit_pos = min(start_idx, len(lines))
            replacement = [line + "\n" for line in content.splitlines()]
            lines = lines[:start_idx] + replacement + lines[end_idx:]

            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.writelines(lines)

            # Auto-stage and Auto-commit
            git_rel_path = os.path.relpath(target_path, active_dir)
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            subprocess.run(["git", "add", git_rel_path], cwd=active_dir, check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.name='Maven'", "-c", "user.email='maven@internal'", "commit", "-m", f"Auto-update: {file_path} (lines {start_line}-{end_line}) @ {timestamp}"], cwd=active_dir, check=False, capture_output=True)

            total_lines = len(lines)
            if total_lines == 0:
                return f"File '{file_path}' edited and committed automatically (file is now empty)."

            snippet_start_idx = max(0, edit_pos - 20)
            last_edited_idx = edit_pos + max(0, len(replacement) - 1)
            snippet_end_idx = min(total_lines, last_edited_idx + 1 + 20)

            snippet_lines = lines[snippet_start_idx:snippet_end_idx]
            snippet = format_numbered_lines(
                snippet_lines,
                start_line_num=snippet_start_idx + 1,
                total_lines=total_lines
            )

            return f"File '{file_path}' edited (lines {start_line}-{end_line}) and committed automatically.\n\nSnippet around edit:\n{snippet}"

        elif action_clean in ["replace", "edit"]:
            if not file_path: return f"Error: 'file_path' required for {action_clean} action."
            if not source_path and content is None: return f"Error: Either 'source_path' or 'content' required for {action_clean} action."
            
            if file_path in ["manual", "documentation"]:
                safe_name = "".join([c if c.isalnum() or c == '_' else "_" for c in tool_name])
                target_path = get_storage_path(os.path.dirname(os.path.dirname(active_dir)), f"{safe_name}_manual.md")
            else:
                target_path = get_storage_path(active_dir, file_path)
                if not os.path.exists(target_path) and os.path.isdir(get_storage_path(active_dir, "logic")):
                    target_path = get_storage_path(active_dir, "logic", file_path)
            
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            
            if source_path:
                src_abs, _ = get_safe_path(source_path)
                import shutil
                shutil.copy2(src_abs, target_path)
            else:
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(content)
            
            # Auto-stage and Auto-commit
            git_rel_path = os.path.relpath(target_path, active_dir)
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            subprocess.run(["git", "add", git_rel_path], cwd=active_dir, check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.name='Maven'", "-c", "user.email='maven@internal'", "commit", "-m", f"Auto-update: {file_path} @ {timestamp}"], cwd=active_dir, check=False, capture_output=True)
            
            return f"File '{file_path}' has been replaced (from {'workspace' if source_path else 'string'}) and committed automatically."

        elif action_clean == "install":
            if not dependencies: return "Error: 'dependencies' (list of strings) required for 'install' action."
            
            venv_dir = get_storage_path(active_dir, "venv")
            if not os.path.exists(venv_dir):
                return f"Error: Virtual environment not found for tool '{tool_name}'."
            
            python_exe = _get_venv_python(venv_dir)
            try:
                log_tool(f"Installing dependencies for {tool_name}: {', '.join(dependencies)}")
                subprocess.run([python_exe, "-m", "pip", "install"] + dependencies, check=True, capture_output=True)
                
                meta_path = get_storage_path(active_dir, "metadata.json")
                if os.path.exists(meta_path):
                    with open(meta_path, "r") as f:
                        meta = json.load(f)
                    
                    existing_deps = meta.get("dependencies", [])
                    for d in dependencies:
                        if d not in existing_deps:
                            existing_deps.append(d)
                    meta["dependencies"] = existing_deps
                    
                    with open(meta_path, "w") as f:
                        json.dump(meta, f, indent=4)
                    
                    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    subprocess.run(["git", "add", "metadata.json"], cwd=active_dir, check=True, capture_output=True)
                    subprocess.run(["git", "-c", "user.name='Maven'", "-c", "user.email='maven@internal'", "commit", "-m", f"Auto-dependency update @ {timestamp}"], cwd=active_dir, check=True, capture_output=True)
                    
                return f"Successfully installed dependencies: {', '.join(dependencies)}. Committed automatically."
            except subprocess.CalledProcessError as pe:
                err = pe.stderr.decode() if pe.stderr else str(pe)
                return f"Pip install failed: {err}"
            
        elif action_clean == "commit":
            if not commit_message: return "Error: 'commit_message' required."
            res = subprocess.run(["git", "commit", "-m", commit_message], cwd=active_dir, text=True, capture_output=True)
            if res.returncode == 0:
                return f"Changes committed: {res.stdout}"
            return f"Commit failed or nothing to commit:\n{res.stderr}"
            
        return f"Invalid action: {action}"
    except Exception as e:
        return f"Error fixing tool: {e}"

@tool
def manage_active_skill(action: str, tool_name: str, new_name: str = None, config: RunnableConfig = None) -> str:
    """
    Manages the lifecycle of registered active skills.
    Actions: 'remove' (deletes the tool), 'rename' (changes tool identity).
    """
    agent_name = _get_agent(config)
    
    # --- MODE RESTRICTIONS ---
    if CURRENT_AGENT_VIEW:
        mode = getattr(CURRENT_AGENT_VIEW, "agent_mode", "PLAN")
        if mode == "PLAN":
            return "Error: Managing/deleting active skills is not allowed in SAFE (PLAN) mode. Please switch to SEMI-AUTO or FULL-AUTO mode."
        elif mode == "INTERMEDIATE":
            kwargs = {
                "action": action,
                "tool_name": tool_name,
                "new_name": new_name
            }
            if not CURRENT_AGENT_VIEW.confirm_tool_execution("manage_active_skill", kwargs, agent_name=agent_name):
                return "Error: Tool execution of 'manage_active_skill' was rejected by the user."

    safe_agent_name = agent_name.replace(" ", "_")
    active_dir = get_storage_path("agents", "skills", safe_agent_name, "active_tools", tool_name)
    
    if not os.path.exists(active_dir):
        return f"Error: Tool '{tool_name}' not found."
        
    try:
        import shutil
        if action == "remove":
            shutil.rmtree(active_dir)
            return f"Tool '{tool_name}' has been deleted."
        elif action == "rename":
            if not new_name: return "Error: 'new_name' required for rename action."
            target_dir = get_storage_path(os.path.dirname(active_dir), new_name)
            if os.path.exists(target_dir): return f"Error: Tool '{new_name}' already exists."
            
            # Update schema.json internal name
            schema_path = get_storage_path(active_dir, "schema.json")
            if os.path.exists(schema_path):
                with open(schema_path, "r") as f: s = json.load(f)
                s["name"] = new_name
                with open(schema_path, "w") as f: json.dump(s, f, indent=4)
                
            os.rename(active_dir, target_dir)
            return f"Tool '{tool_name}' renamed to '{new_name}'."
        return f"Invalid action: {action}"
    except Exception as e:
        return f"Error managing tool: {e}"

@tool
def read_skill(skill_name: str, config: RunnableConfig) -> str:
    """Reads the playbook (.md) or schema (.json) of a previously saved skill."""
    agent_name = _get_agent(config)
    skills_dir = get_storage_path("agents", "skills", agent_name)
    safe_name = "".join([c if c.isalnum() or c == '_' else "_" for c in skill_name])

    # Try .md first (Passive Skill)
    md_path = get_storage_path(skills_dir, f"{safe_name}.md")
    if os.path.exists(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            return f.read()

    # Try .json next (Active Skill)
    json_path = get_storage_path(skills_dir, f"{safe_name}.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            return f.read()

    return f"Skill '{skill_name}' not found in library (Checked .md and .json)."

@tool
def delete_passive_skill(skill_name: str, config: RunnableConfig) -> str:
    """Deletes a passive skill (playbook/steps) from your procedural memory library."""
    agent_name = _get_agent(config)
    skills_dir = get_storage_path("agents", "skills", agent_name)
    safe_name = "".join([c if c.isalnum() or c == '_' else "_" for c in skill_name])
    path = get_storage_path(skills_dir, f"{safe_name}.md")
    
    if os.path.exists(path):
        os.remove(path)
        return f"Success: Passive skill '{safe_name}' has been permanently deleted from {agent_name}'s library."
    return f"Error: Passive skill '{skill_name}' (filename: '{safe_name}.md') not found."

@tool
def list_skills(config: RunnableConfig) -> str:
    """Lists all passive playbooks and active executable tools currently stored in your skills library."""
    agent_name = _get_agent(config)
    skills_dir = get_storage_path("agents", "skills", agent_name)
    
    passive_skills = []
    active_skills = []
    
    if os.path.exists(skills_dir):
        passive_skills = [f.replace(".md", "") for f in os.listdir(skills_dir) if f.endswith(".md")]
        active_tools_dir = get_storage_path("agents", "skills", agent_name, "active_tools")
        if os.path.exists(active_tools_dir):
            active_skills = [d for d in os.listdir(active_tools_dir) if os.path.isdir(os.path.join(active_tools_dir, d))]
            
    output = f"Skills Library for {agent_name}:\n\n"
    output += "Passive Skills (Use 'read_skill' to read steps):\n"
    if passive_skills:
        output += "\n".join(f"- {p}" for p in sorted(passive_skills))
    else:
        output += "- None"
        
    output += "\n\nActive Skills (Call these directly as tools):\n"
    if active_list := ", ".join(active_skills):
        output += active_list
    else:
        output += "None"
        
    return output

@tool
def distill_journey(skill_name: str, task_summary: str, successful_steps: str, config: RunnableConfig) -> str:
    """Distills a successful workflow into procedural memory so you can repeat it in the future."""
    agent_name = _get_agent(config)
    skills_dir = get_storage_path("agents", "skills", agent_name)
    os.makedirs(skills_dir, exist_ok=True)
    
    safe_name = "".join([c if c.isalnum() or c == '_' else "_" for c in skill_name])
    path = get_storage_path(skills_dir, f"{safe_name}.md")
    content = f"# Task: {task_summary}\n\n## Playbook\n{successful_steps}"
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Success: Skill '{safe_name}' has been distilled and saved to your library."

@tool
def mark_quagmire(trap_name: str, what_failed: str, avoidance_strategy: str, config: RunnableConfig) -> str:
    """Records a failed approach, dead-end, or anti-pattern so you never repeat the mistake."""
    agent_name = _get_agent(config)
    memory_dir = get_storage_path("agents", "memory", agent_name)
    os.makedirs(memory_dir, exist_ok=True)
    
    path = get_storage_path(memory_dir, "QUAGMIRES.md")
    entry = f"\n### TRAP: {trap_name}\n- **What failed:** {what_failed}\n- **How to avoid:** {avoidance_strategy}\n"
    
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
    return f"Success: The quagmire '{trap_name}' has been permanently logged."

@tool
def get_user_clarification(options: List[str] = None, config: RunnableConfig = None) -> str:
    """
    Pause execution and ask the user for clarification or a choice.
    Use this when you are unsure about a path, need missing information, or want the user to pick from a set of options.
    Returns the user's typed response or the text of the selected option.
    """
    agent_name = _get_agent(config) if config else "Agent"
    if CURRENT_APP:
        try:
            agent_view = CURRENT_APP.query_one("#ai_agent_view")
            log_tool(f"Waiting for clarification ({agent_name})...")
            return agent_view.request_clarification(options, agent_name=agent_name)
        except Exception as e:
            return f"Error requesting clarification: {e}"
    return "Error: User interface not available for clarification."

@tool
def search_episodic_memory(query: str, config: RunnableConfig) -> str:
    """
    Search your past conversation history conceptually (semantically).
    Use this to find previous decisions, technical details, or context even if exact words don't match.
    Returns the most relevant snippets and their Session IDs.
    """
    agent_name = _get_agent(config)
    
    if not CURRENT_APP:
        return "Error: System not initialized."
        
    try:
        # Avoid direct import to prevent circularity
        agent_view = CURRENT_APP.query_one("#ai_agent_view")
        engine = agent_view.session_manager.semantic_engine
        current_session_id = agent_view.session_manager.current_session_id
        
        log_tool(f"Searching memory for: [cyan]{query}[/]")
        results = engine.search(agent_name, query, limit=5, exclude_session_id=current_session_id)
        
        if not results:
            return f"No relevant semantic memories found for '{query}'."
            
        output = f"Top {len(results)} Semantic Memories for {agent_name}:\n\n"
        for i, res in enumerate(results, 1):
            score_pct = int(res['score'] * 100)
            output += f"{i}. [Relevance: {score_pct}%] (Session: {res['session_id']})\n"
            output += f"   \"{res['text']}\"\n\n"
            
        return output
    except Exception as e:
        return f"Error performing semantic search: {e}"

@tool
def retrieve_episodic_memory(session_id: str, config: RunnableConfig) -> str:
    """
    Returns the entire conversation history for a specific Session ID.
    Use this after finding a relevant session via search_episodic_memory to get the full context.
    """
    agent_name = _get_agent(config)
    safe_name = agent_name.replace(" ", "_")
    session_file = f"{session_id}_{safe_name}.json"
    path = get_storage_path("sessions", session_file)
    
    if not os.path.exists(path):
        return f"Error: Session log {session_file} not found."
        
    try:
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
            
        output = f"--- VERBATIM HISTORY FOR SESSION {session_id} ---\n\n"
        for msg in history:
            role = msg.get("role", "unknown").upper()
            if role == "SYSTEM": continue
            content = msg.get("content", "")
            output += f"[{role}]:\n{content}\n\n"
            
        return output
    except Exception as e:
        return f"Error reading session log: {e}"

@tool
def get_toolresult(ids: List[int], config: RunnableConfig = None) -> str:
    """Fetches hidden outputs of tool calls. BATCHING MANDATE: If you need multiple tool outputs, pass ALL their Result IDs together in a single list (e.g. ids=[1, 2, 3])."""
    agent_name = _get_agent(config)
    try:
        if not CURRENT_APP:
            return "Error: System not initialized."

        sm = CURRENT_APP.query_one("#ai_agent_view").session_manager
        current_session_id = sm.current_session_id

        if isinstance(ids, int):
            ids = [ids]

        cursor = shared_db_conn.cursor()
        results = []
        for id_val in ids:
            cursor.execute("SELECT agent_name, tool_name, output FROM global_tool_results WHERE id = ? AND session_id = ?", (id_val, current_session_id))
            row = cursor.fetchone()
            if not row:
                results.append(f"Error: Tool result ID {id_val} not found in the current chat session.")
            else:
                orig_agent, tool_name, output = row
                results.append(f"Result (ID {id_val}) successfully retrieved for tool '{tool_name}':\n\n{output}")
        return "\n\n".join(results)
    except Exception as e:
        return f"Error retrieving tool results: {e}"

@tool
def set_toolresult(id: int, config: RunnableConfig = None) -> str:
    """Toggles a tool result between PUBLIC (unhidden) and PRIVATE (hidden behind stub). You can only use this for tools YOU originally executed."""
    try:
        caller_agent = None
        if config and config.get("configurable", {}).get("thread_id") == "human_override":
            caller_agent = "HUMAN"
        else:
            caller_agent = _get_agent(config) if config else "Unknown"
            
        if not CURRENT_APP:
            return "Error: System not initialized."

        sm = CURRENT_APP.query_one("#ai_agent_view").session_manager
        current_session_id = sm.current_session_id
        
        cursor = shared_db_conn.cursor()
        cursor.execute("SELECT agent_name, tool_name, output, is_public, args, timestamp FROM global_tool_results WHERE id = ? AND session_id = ?", (id, current_session_id))
        row = cursor.fetchone()
        if not row:
            return f"Error: Tool result ID {id} not found in the current chat session."
            
        orig_agent, tool_name, output, is_public, db_args, ts = row
        
        if caller_agent != "HUMAN" and caller_agent != orig_agent:
            return f"Error: You ({caller_agent}) cannot toggle Tool Result {id} because it was executed by {orig_agent}. Only the original executing agent or the human user can toggle it."
        
        new_public_state = 0 if is_public == 1 else 1
        cursor.execute("UPDATE global_tool_results SET is_public = ? WHERE id = ? AND session_id = ?", (new_public_state, id, current_session_id))
        shared_db_conn.commit()
        
        if new_public_state == 1:
            new_content = f'<AGENT_INTERCOM_TOOL_RESPONSE agent="{orig_agent}" tool="{tool_name}" id="{id}">\n{output}\n</AGENT_INTERCOM_TOOL_RESPONSE>'
            status_desc = "PUBLIC (visible to all agents)"
        else:
            args_str = db_args or "None"
            stub = (
                f"[Tool Output Hidden]\n"
                f"- Tool Name: {tool_name}\n"
                f"- Result ID: {id}\n"
                f"- Arguments: {args_str}\n"
                f"- Time: {ts or ''}\n"
                f"- Action: Use get_toolresult(ids=[{id}]) to read output privately, or set_toolresult(id={id}) to make public."
            )
            new_content = f'<AGENT_INTERCOM_TOOL_RESPONSE agent="{orig_agent}" tool="{tool_name}" id="{id}">\n{stub}\n</AGENT_INTERCOM_TOOL_RESPONSE>'
            status_desc = "PRIVATE (hidden behind stub)"

        with sm._lock:
            import re
            for a_name, history in sm.active_sessions.items():
                for hm in history:
                    if hm.role == "human" and f'id="{id}">' in hm.content:
                        hm.content = re.sub(
                            rf'<AGENT_INTERCOM_TOOL_RESPONSE[^>]*id="{id}">.*?</AGENT_INTERCOM_TOOL_RESPONSE>',
                            lambda _: new_content,
                            hm.content,
                            flags=re.DOTALL
                        )
                sm.save_session(a_name, _bypass_lock=True)
            return f"Tool result ID {id} toggled to {status_desc}."
    except Exception as e:
        return f"Error: {e}"

# Monkey-patch SessionManager to keep episodic memory retrieval private to the executing agent
try:
    from orchestration import SessionManager
    _orig_broadcast = SessionManager.broadcast_message
    SessionManager.broadcast_message = lambda self, sender, content, is_ai=True, tool_outputs=None, tool_calls=None: _orig_broadcast(
        self, sender, content, is_ai,
        [out for out in tool_outputs if out.get("name") not in ("search_episodic_memory", "retrieve_episodic_memory", "get_toolresult", "set_toolresult")] if tool_outputs else None,
        tool_calls
    )
except Exception:
    pass
    

# --- HELPER: Visual Cursor Feedback Capture ---
def capture_marked_screenshot() -> str:
    """
    Captures the screen state and manually draws a highly visible red crosshair at the current cursor coordinates.
    Hardcodes terminal minimization before capturing, and restores it immediately after.
    """
    import pyautogui
    import os
    import datetime
    import platform
    import subprocess
    import time
    from PIL import Image, ImageDraw
    
    sys_os = platform.system()
    hide_terminal = True
    
    # 1. Hide Terminal
    if hide_terminal:
        if sys_os == "Darwin": # macOS
            subprocess.run(["osascript", "-e", 'tell application "System Events" to set visible of first process whose frontmost is true to false'])
        elif sys_os == "Windows": # Windows
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd: ctypes.windll.user32.ShowWindow(hwnd, 6) # SW_MINIMIZE
        else: # Linux
            subprocess.run(["xdotool", "windowminimize", "$(xdotool getactivewindow)"], shell=True)
        
        time.sleep(0.5) # Wait for the minimize animation to finish
        
    # Retrieve current resolution & cursor position
    width, height = pyautogui.size()
    x, y = pyautogui.position()
    
    # Standardize boundaries to prevent drawing exceptions
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    
    safe_base_dir, _ = get_safe_path(".")
    screenshot_dir = get_storage_path(safe_base_dir, "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = get_storage_path(screenshot_dir, f"screen_{timestamp}.png")
    
    # Capture screen in-memory
    success = False
    try:
        img = pyautogui.screenshot()
        if img:
            # Draw a bright red crosshair centered at the cursor coordinates
            draw = ImageDraw.Draw(img)
            color = "red"
            length = 15
            # Draw horizontal and vertical lines with a 3px width for visibility
            draw.line([(x - length, y), (x + length, y)], fill=color, width=3)
            draw.line([(x, y - length), (x, y + length)], fill=color, width=3)
            
            img.save(filepath)
            success = os.path.exists(filepath)
    except Exception as e:
        log_tool(f"Visual feedback capture failed: {e}")
        
    # 2. Restore Terminal
    if hide_terminal:
        # Attempt to restore the terminal window
        if sys_os == "Darwin":
            pyautogui.hotkey('command', 'tab')
        elif sys_os == "Windows":
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd: ctypes.windll.user32.ShowWindow(hwnd, 9) # SW_RESTORE
        else:
            subprocess.run(["xdotool", "windowactivate", "$(xdotool search --class terminal | head -n 1)"], shell=True)
            
    if not success:
        return "Error: Failed to capture or write screen state to disk."
        
    # Cache the screenshot path for the auto-attach human message turn handler
    try:
        if CURRENT_APP:
            agent_view = CURRENT_APP.query_one("#ai_agent_view")
            agent_view.last_screenshot_path = filepath
    except Exception:
        pass
        
    max_x = width - 1
    max_y = height - 1
    return (
        f"[Attached Image: {filepath}]\n"
        f"Action executed. Cursor is currently at ({x}, {y}) and marked with a RED crosshair.\n"
        f"Screen Bounds: (0, 0) to ({max_x}, {max_y})."
    )

# --- COMPUTER AUTOMATION TOOLS ---

@tool
def take_screenshot() -> str:
    """
    Takes a global screenshot of the primary monitor.
    Automatically returns an updated screenshot with the current cursor position marked by a red crosshair.
    """
    return capture_marked_screenshot()

@tool
def move_cursor_absolute(x: int, y: int) -> str:
    """
    Moves the mouse cursor to absolute screen coordinates (x, y).
    Automatically returns an updated screenshot with the new cursor position marked by a red crosshair.
    """
    import pyautogui
    try:
        pyautogui.moveTo(x, y)
        return capture_marked_screenshot()
    except Exception as e:
        return f"Error moving cursor: {e}"

@tool
def click_at_current_location(button: str = "left", clicks: int = 1) -> str:
    """
    Clicks the mouse at the cursor's current position.
    button: 'left', 'right', or 'middle'.
    clicks: Number of times to click (1 for single click, 2 for double click).
    Automatically returns an updated screenshot with the cursor position marked by a red crosshair.
    """
    import pyautogui
    try:
        pyautogui.click(button=button, clicks=clicks)
        return capture_marked_screenshot()
    except Exception as e:
        return f"Error executing click: {e}"

@tool
def send_scroll(amount: int) -> str:
    """
    Scrolls the mouse wheel at the cursor's current position.
    amount: Positive numbers scroll up, negative numbers scroll down.
    Automatically returns an updated screenshot with the cursor position marked by a red crosshair.
    """
    import pyautogui
    try:
        pyautogui.scroll(amount)
        return capture_marked_screenshot()
    except Exception as e:
        return f"Error executing scroll: {e}"

@tool
def inject_keyboard_input(text: str = "", hotkeys: List[str] = None) -> str:
    """
    Sends keyboard input to the currently focused window.
    text: String of text to type.
    hotkeys: List of keys to press simultaneously (e.g., ['ctrl', 'c'] or ['enter']).
    Automatically returns an updated screenshot with the cursor position marked by a red crosshair.
    """
    import pyautogui
    try:
        if text:
            pyautogui.write(text, interval=0.01)
        if hotkeys:
            pyautogui.hotkey(*hotkeys)
        return capture_marked_screenshot()
    except Exception as e:
        return f"Error executing keyboard input: {e}"
        
@tool
def move_cursor_relative(dx: int, dy: int) -> str:
    """
    Moves the mouse cursor relative to its current position by dx pixels horizontally and dy pixels vertically.
    Positive dx moves the cursor right, negative moves it left. 
    Positive dy moves the cursor down, negative moves it up.
    Automatically returns an updated screenshot with the new cursor position marked by a red crosshair.
    """
    import pyautogui
    try:
        # Retrieve current position and screen boundaries
        width, height = pyautogui.size()
        x, y = pyautogui.position()
        
        # Calculate and bound target coordinates within screen limits
        new_x = max(0, min(x + dx, width - 1))
        new_y = max(0, min(y + dy, height - 1))
        
        pyautogui.moveTo(new_x, new_y)
        return capture_marked_screenshot()
    except Exception as e:
        return f"Error executing relative cursor move: {e}"