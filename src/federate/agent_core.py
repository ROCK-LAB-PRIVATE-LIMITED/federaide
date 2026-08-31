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
import json
import time
import re
import base64
import mimetypes
from datetime import datetime
import threading

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent

# --- GEMINI THOUGHT SIGNATURE MONKEY-PATCH FOR OPENAI COMPATIBILITY ---
try:
    import langchain_openai.chat_models.base as langchain_openai_base
    
    _orig_convert_message_to_dict = langchain_openai_base._convert_message_to_dict

    def _patched_convert_message_to_dict(message, *args, **kwargs):
        msg_dict = _orig_convert_message_to_dict(message, *args, **kwargs)
        
        if msg_dict.get("role") == "tool" and not msg_dict.get("name"):
            msg_dict["name"] = "unknown_tool"
            
        elif msg_dict.get("role") == "assistant" and msg_dict.get("tool_calls"):
            for tc in msg_dict["tool_calls"]:
                func = tc.get("function")
                if func and not func.get("name"):
                    func["name"] = "unknown_tool"

        if isinstance(message, AIMessage) or msg_dict.get("role") == "assistant":
            tool_calls = msg_dict.get("tool_calls")
            if tool_calls:
                sig_map = {}
                raw_tool_calls = getattr(message, "additional_kwargs", {}).get("tool_calls", [])
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

import toolbox
from orchestration import AgentConfig, HistoryMessage
from subagents import dispatch_coding_subagent
import hashlib
import secrets

AUTH_CONFIG_PATH = os.path.join(toolbox.FEDERATE_DIR, "master_auth.json")
_UNLOCKED_SESSION_TOKEN = None
_BACKSTORY_LOCK = threading.Lock()

def _hash_password(password: str, salt: str = None) -> tuple:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return h.hex(), salt

def is_master_password_set() -> bool:
    if os.path.exists(AUTH_CONFIG_PATH):
        try:
            with open(AUTH_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return bool(data.get("hash") and data.get("salt"))
        except Exception:
            pass
    return False

def set_master_password(password: str) -> str:
    pwd_hash, salt = _hash_password(password)
    os.makedirs(os.path.dirname(AUTH_CONFIG_PATH), exist_ok=True)
    with open(AUTH_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"hash": pwd_hash, "salt": salt}, f, indent=4)
    global _UNLOCKED_SESSION_TOKEN
    _UNLOCKED_SESSION_TOKEN = secrets.token_hex(32)
    return _UNLOCKED_SESSION_TOKEN

def unlock_core(password: str) -> str | None:
    if not os.path.exists(AUTH_CONFIG_PATH):
        return None
    try:
        with open(AUTH_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        expected_hash = data.get("hash", "")
        salt = data.get("salt", "")
        if not expected_hash or not salt:
            return None
        actual_hash, _ = _hash_password(password, salt)
        if secrets.compare_digest(actual_hash, expected_hash):
            global _UNLOCKED_SESSION_TOKEN
            _UNLOCKED_SESSION_TOKEN = secrets.token_hex(32)
            return _UNLOCKED_SESSION_TOKEN
        return None
    except Exception:
        return None

def is_core_unlocked() -> bool:
    return True

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

def translate_backstory(new_config: AgentConfig) -> tuple[str, str]:
    if new_config.use_backup and new_config.backup_model:
        model = new_config.backup_model
        base_url = new_config.backup_base_url or new_config.base_url
        api_key = new_config.get_backup_api_key() or new_config.get_api_key()
    else:
        model = new_config.model
        base_url = new_config.base_url
        api_key = new_config.get_api_key()

    error_msg = None
    translated = None

    if not api_key:
        error_msg = f"API Key is missing for agent '{new_config.name}'."
    else:
        try:
            effort = getattr(new_config, "reasoning_effort", "none")
            extra_args = {"model_kwargs": {"reasoning_effort": effort}} if effort not in ("none", None, "") else {}

            llm = ChatOpenAI(
                model=model,
                api_key=api_key,
                base_url=base_url,
                temperature=0,
                max_retries=5,
                timeout=150,
                **extra_args
            )
            pronoun_val = getattr(new_config, "pronouns", "neither")
            pronoun_instruction = f" Use '{pronoun_val}' pronouns when referring to this agent." if pronoun_val != "neither" else " Use gender-neutral pronouns (they/them) when referring to this agent."
            prompt = f"Convert the following AI agent backstory from 1st/2nd person to 3rd person. Start with '{new_config.name} is...'.{pronoun_instruction} Only return the converted backstory, nothing else.\n\nOriginal: {new_config.backstory}"

            res = llm.invoke([HumanMessage(content=prompt)])
            translated = res.content.strip() if res and res.content else None
            if not translated:
                error_msg = "Model returned an empty response during backstory verification."
        except Exception as e:
            error_msg = str(e)

    return translated, error_msg

def force_update_all_backstories_core(agent_view):
    agent_view.log_to_ui("[dim cyan]Force updating backstories for all agents...[/dim cyan]")
    all_agents = list(agent_view.agent_manager.agents.values())
    
    with _BACKSTORY_LOCK:
        cache_path = toolbox.get_storage_path("agents", "translated_backstories.json")
        cache = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                pass

        updated = False
        host_agent = agent_view.active_agent
        if host_agent.use_backup and host_agent.backup_model:
            model = host_agent.backup_model
            base_url = host_agent.backup_base_url or host_agent.base_url
            api_key = host_agent.get_backup_api_key() or host_agent.get_api_key()
        else:
            model = host_agent.model
            base_url = host_agent.base_url
            api_key = host_agent.get_api_key()

        if not api_key:
            agent_view.log_to_ui("[bold red]No API key available to translate backstories.[/bold red]")
            return

        try:
            llm = ChatOpenAI(
                model=model,
                api_key=api_key,
                base_url=base_url,
                temperature=0,
                max_retries=5,
            )
        except Exception as e:
            agent_view.log_to_ui(f"[bold red]Failed to initialize LLM: {e}[/bold red]")
            return

        for a in all_agents:
            try:
                agent_view.log_to_ui(f"[dim]Translating backstory for {a.name} to 3rd person...[/dim]")
                
                pronoun_val = getattr(a, "pronouns", "neither")
                pronoun_instruction = f" Use '{pronoun_val}' pronouns when referring to this agent." if pronoun_val != "neither" else " Use gender-neutral pronouns (they/them) when referring to this agent."
                prompt = f"Convert the following AI agent backstory from 1st/2nd person to 3rd person. Start with '{a.name} is...'.{pronoun_instruction} Only return the converted backstory, nothing else.\n\nOriginal: {a.backstory}"
                res = toolbox.resilient_invoke(llm, [HumanMessage(content=prompt)])
                translated = res.content.strip()
                if translated:
                    cache[a.name] = {
                        "original": a.backstory,
                        "translated": translated
                    }
                    updated = True
            except Exception as e:
                agent_view.log_to_ui(f"[dim red]Translation failed for {a.name}: {e}[/dim red]")

        if updated:
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=4)
            except Exception:
                pass
        agent_view.log_to_ui("[bold green]All agent backstories have been translated and updated.[/bold green]")

def translate_team_backstories_core(agent_view, host_agent: AgentConfig, all_agents: list):
    with _BACKSTORY_LOCK:
        cache_path = toolbox.get_storage_path("agents", "translated_backstories.json")
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
                if host_agent.use_backup and host_agent.backup_model:
                    model = host_agent.backup_model
                    base_url = host_agent.backup_base_url or host_agent.base_url
                    api_key = host_agent.get_backup_api_key() or host_agent.get_api_key()
                else:
                    model = host_agent.model
                    base_url = host_agent.base_url
                    api_key = host_agent.get_api_key()

                if api_key:
                    agent_view.log_to_ui(f"[dim]Translating backstory for {a.name} to 3rd person...[/dim]")
                    llm = ChatOpenAI(
                        model=model,
                        api_key=api_key,
                        base_url=base_url,
                        temperature=0,
                        max_retries=5,
                    )
                    
                    pronoun_val = getattr(a, "pronouns", "neither")
                    pronoun_instruction = f" Use '{pronoun_val}' pronouns when referring to this agent." if pronoun_val != "neither" else " Use gender-neutral pronouns (they/them) when referring to this agent."
                    prompt = f"Convert the following AI agent backstory from 1st/2nd person to 3rd person. Start with '{a.name} is...'.{pronoun_instruction} Only return the converted backstory, nothing else.\n\nOriginal: {a.backstory}"
                    res = toolbox.resilient_invoke(llm, [HumanMessage(content=prompt)])
                    translated = res.content.strip()
                    if translated:
                        cache[a.name] = {
                            "original": a.backstory,
                            "translated": translated
                        }
                        updated = True
            except Exception as e:
                agent_view.log_to_ui(f"[dim red]Translation failed for {a.name}: {e}[/dim red]")

        if updated:
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=4)
            except Exception:
                pass

def get_executor_core(agent_view, agent_config: AgentConfig):
    if not is_core_unlocked():
        if hasattr(agent_view, "log_to_ui"):
            agent_view.log_to_ui("[bold red]Security Violation: Federaide Core is locked. Master password authentication required.[/bold red]")
        return None

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
        
    effort = getattr(agent_config, "reasoning_effort", "none")
    extra_args = {"model_kwargs": {"reasoning_effort": effort}} if effort not in ("none", None, "") else {}
    llm = ChatOpenAI(
        model=model, 
        temperature=getattr(agent_config, "temperature", 1.0),
        api_key=api_key,
        base_url=base_url,
        max_retries=5,
        timeout=120,
        **extra_args
    )

    def preprocess_messages(messages):
        if not isinstance(messages, list):
            return messages
        processed = []
        for msg in messages:
            processed.append(msg)
            if msg.__class__.__name__ == "ToolMessage" and isinstance(msg.content, str):
                tool_name = getattr(msg, "name", None)
                if tool_name in {"edit_file", "save_file", "list_files", "search_web", "perform_research", "manage_agenda"}:
                    continue
                
                if "[Attached Image:" in msg.content:
                    matches = re.finditer(r'\[Attached Image:\s*(.*?)\]', msg.content)
                    for match in matches:
                        filepath = match.group(1).strip()
                        try:
                            resolved_path, _ = toolbox.get_safe_path(filepath)
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
                            
                if "[ImageBase64:" in msg.content:
                    matches = re.finditer(r'\[ImageBase64:\s*(data:image/[a-zA-Z]+;base64,[^\]]+)\]', msg.content)
                    for match in matches:
                        url = match.group(1).strip().replace("\n", "").replace("\r", "").replace(" ", "")
                        if any(marker in url for marker in ["{", "}", "<", ">", "b64_str", "base64data"]):
                            continue
                        msg.content = msg.content.replace(match.group(0), "[ImageBase64: <data_transmitted>]")
                        companion = HumanMessage(content=[
                            {"type": "text", "text": "[Dynamic Visual Output]:"},
                            {"type": "image_url", "image_url": {"url": url}}
                        ])
                        processed.append(companion)
        return processed

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
    
    if agent_config.disable_all_tools:
        tools = [
            toolbox.update_core_memory, toolbox.save_skill, toolbox.read_skill, toolbox.list_skills, 
            toolbox.distill_journey, toolbox.delete_passive_skill, toolbox.mark_quagmire, 
            toolbox.get_user_clarification, toolbox.search_episodic_memory, toolbox.retrieve_episodic_memory,
            toolbox.get_toolresult
        ]
        allowed_names = {getattr(t, "name", t) for t in tools}
        
        other_tool_names = [
            "list_files", "search_web", "perform_research", "manage_agenda",
            "read_file", "fetch_url", "save_file", "edit_file", 
            "dispatch_coding_subagent", "run_terminal_command", "take_screenshot",        
            "click_at_current_location", "move_cursor_absolute", 
            "move_cursor_relative", "send_scroll", "inject_keyboard_input",
            "prepare_active_skill", "finalize_active_skill", "manage_active_skill", "fix_active_skill"
        ]
        try:
            dynamic_tools = toolbox.load_dynamic_tools(agent_config.name)
            for dt in dynamic_tools:
                if dt.name not in other_tool_names:
                    other_tool_names.append(dt.name)
        except Exception:
            pass
            
        dummy_tools = []
        for name in other_tool_names:
            dummy_tools.append(StructuredTool.from_function(
                func=lambda *args, n=name, **kwargs: f"Error: Tool '{n}' is unauthorized. All tools are disabled for this agent.",
                name=name,
                description=f"Unauthorized placeholder."
            ))
            
        tools.extend(dummy_tools)
        final_tools = tools
        
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
        disabled_tools = set(getattr(agent_config, "disabled_tools", ["visual_computer_operation", "send_file_to_telegram"]))

        def is_tool_disabled(t_name: str) -> bool:
            if t_name in disabled_tools:
                return True
            computer_tools = {"take_screenshot", "click_at_current_location", "move_cursor_absolute", "move_cursor_relative", "send_scroll", "inject_keyboard_input"}
            if t_name in computer_tools and "visual_computer_operation" in disabled_tools:
                return True
            return False

        raw_tools = [toolbox.search_web, toolbox.perform_research, toolbox.render_pdf, toolbox.update_core_memory, toolbox.save_skill, toolbox.read_skill, toolbox.distill_journey, toolbox.delete_passive_skill, toolbox.list_skills, toolbox.mark_quagmire, toolbox.get_user_clarification, toolbox.search_episodic_memory, toolbox.retrieve_episodic_memory, toolbox.prepare_active_skill, toolbox.finalize_active_skill, toolbox.manage_active_skill, toolbox.fix_active_skill, toolbox.get_toolresult]
        raw_tools.extend(toolbox.load_dynamic_tools(agent_config.name))

        high_priv_map = {
            "manage_agenda": toolbox.manage_agenda,
            "list_files": toolbox.list_files,
            "read_file": toolbox.read_file,
            "fetch_url": toolbox.fetch_url,
            "save_file": toolbox.save_file,
            "edit_file": toolbox.edit_file,
            "dispatch_coding_subagent": dispatch_coding_subagent,
            "run_terminal_command": toolbox.run_terminal_command,
            "take_screenshot": toolbox.take_screenshot,        
            "click_at_current_location": toolbox.click_at_current_location,
            "move_cursor_absolute": toolbox.move_cursor_absolute, 
            "move_cursor_relative": toolbox.move_cursor_relative,       
            "send_scroll": toolbox.send_scroll,                    
            "inject_keyboard_input": toolbox.inject_keyboard_input,
            "send_file_to_telegram": toolbox.send_file_to_telegram
        }

        def make_wrapped_tool(t_obj):
            def wrapped_func(*args, **kwargs):
                agent_name = agent_config.name
                confirmed = agent_view.confirm_tool_execution(t_obj.name, kwargs, agent_name=agent_name)
                if not confirmed:
                    return f"Error: Tool execution of '{t_obj.name}' was rejected by the user."
                return t_obj.func(*args, **kwargs)
            return StructuredTool(
                name=t_obj.name,
                description=t_obj.description,
                args_schema=t_obj.args_schema,
                func=wrapped_func
            )

        if agent_view.agent_mode == "EXECUTE":
            for tname, tool_obj in high_priv_map.items():
                raw_tools.append(tool_obj)
        elif agent_view.agent_mode == "INTERMEDIATE":
            for tname, tool_obj in high_priv_map.items():
                raw_tools.append(make_wrapped_tool(tool_obj))
        else: # PLAN (SAFE) Mode
            for tname in agent_config.enabled_tools:
                if tname == "visual_computer_operation":
                    for ct in ["take_screenshot", "click_at_current_location", "move_cursor_absolute", "move_cursor_relative", "send_scroll", "inject_keyboard_input"]:
                        raw_tools.append(make_wrapped_tool(high_priv_map[ct]))
                elif tname in high_priv_map:
                    raw_tools.append(make_wrapped_tool(high_priv_map[tname]))
        
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

    executor = create_react_agent(llm, final_tools, checkpointer=toolbox.shared_memory)
    return executor


def run_agent_task_core(agent_view, agent: AgentConfig, prompt: str, override_thread_id: str = None, batch_id: int = 0):
    if not is_core_unlocked():
        if hasattr(agent_view, "log_to_ui"):
            agent_view.log_to_ui("[bold red]Security Violation: Federaide Core is locked. Master password authentication required.[/bold red]")
        return

    toolbox.register_thread()
    if agent.name in agent_view._running_agents:
        agent_view.log_to_ui(f"[dim yellow]Agent {agent.name} is already working on a task.[/dim yellow]")
        toolbox.unregister_thread()
        return

    if not agent_view.ensure_chatgpt_auth_for_agent(agent):
        agent_view.log_to_ui(f"[bold red]ChatGPT OAuth authentication required for {agent.name}. Task cancelled.[/bold red]")
        toolbox.unregister_thread()
        return

    agent_view._running_agents.add(agent.name)
    try:
        try:
            translate_team_backstories_core(agent_view, agent, list(agent_view.agent_manager.agents.values()))
        except Exception:
            pass

        try:
            history = agent_view.session_manager.active_sessions.get(agent.name, [])
            if history and history[0].role == "system":
                history[0].content = agent.get_full_system_prompt(list(agent_view.agent_manager.agents.values()))
        except Exception:
            pass

        executor = agent_view.get_executor(agent)
        if not executor:
            agent_view.log_to_ui(f"[bold red]Agent {agent.name} not configured (Key missing).[/bold red]")
            return
        
        toolbox.thread_context.agent_name = agent.name    
        toolbox.thread_context.batch_id = batch_id
        agent_view.app.call_from_thread(agent_view._toggle_spinner, True, agent.name, agent.color)
        agent_view.app.call_from_thread(agent_view.update_tokens)
        if getattr(agent_view, "tts_enabled", False):
            agent_view.tts_manager.start_stream(voice=agent.tts_voice, agent_name=agent.name)
        thread_id = override_thread_id or f"{agent_view.session_manager.current_session_id}_{agent.name}"
        run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 5000}

        current_ai_text = ""
        full_ai_response = ""
        has_mounted_ai_box = False
        tool_outputs = []
        tool_calls = []

        try:
            state = executor.get_state(run_config)

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
                                    dpi_val = getattr(agent_view, "pdf_dpi", None) or 150
                                    scale_val = dpi_val / 72.0
                                    
                                    for page in doc:
                                        bitmap = page.render(scale=scale_val)
                                        pil_img = bitmap.to_pil()
                                        
                                        buffered = io.BytesIO()
                                        pil_img.save(buffered, format="PNG")
                                        b64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                                        content_list.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                                except Exception as pdf_e:
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

            history = agent_view.session_manager.active_sessions.get(agent.name, [])
            if not history:
                agent_view.session_manager.init_agent_session(agent, list(agent_view.agent_manager.agents.values()))
                history = agent_view.session_manager.active_sessions.get(agent.name, [])
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
                        outputs_by_id = {o.get("tool_call_id"): o for o in (hm.tool_outputs or []) if o.get("tool_call_id")}
                        outputs_by_name_list = {}
                        for o in (hm.tool_outputs or []):
                            n = o.get("name")
                            if n:
                                outputs_by_name_list.setdefault(n, []).append(o)

                        for tc in hm.tool_calls:
                            tc_name = tc.get("name")
                            tc_id = tc.get("id")
                            
                            output = None
                            if tc_id and tc_id in outputs_by_id:
                                output = outputs_by_id[tc_id]
                            elif tc_name in outputs_by_name_list and outputs_by_name_list[tc_name]:
                                output = outputs_by_name_list[tc_name].pop(0)

                            if output:
                                tool_content = str(output.get("content", ""))
                                langchain_messages.append(ToolMessage(
                                    content=tool_content,
                                    name=tc_name,
                                    tool_call_id=tc.get("id", "unknown")
                                ))
                                if "[Attached Image:" in tool_content:
                                    matches = re.finditer(r'\[Attached Image: (.*?)\]', tool_content)
                                    for match in matches:
                                        if agent.is_capable_vision:
                                            filepath = match.group(1).strip()
                                            langchain_messages.append(HumanMessage(
                                                content=_format_vision_content(f"[Attached Image: {filepath}]", True)
                                            ))
                            else:
                                agent_view.log_to_ui(f"[dim yellow] Healing interrupted tool call: {tc_name}[/dim yellow]")
                                langchain_messages.append(ToolMessage(
                                    content="[Tool execution was interrupted or cancelled during session transition.]",
                                    name=tc_name,
                                    tool_call_id=tc.get("id", "unknown")
                                ))

            # ---------------------------------------------------------------------
            # --- OPTIMIZATION PASS: Strip Older Automated Screenshots Only ---
            # ---------------------------------------------------------------------
            last_screenshot_msg_idx = -1
            for msg_idx, msg in enumerate(langchain_messages):
                if isinstance(msg, HumanMessage) and isinstance(msg.content, list):
                    is_screenshot = False
                    if msg_idx < len(history):
                        is_screenshot = "screenshots/screen_" in (history[msg_idx].content or "")
                    else:
                        is_screenshot = True 

                    if is_screenshot and any(block.get("type") == "image_url" for block in msg.content):
                        last_screenshot_msg_idx = msg_idx
            
            if last_screenshot_msg_idx != -1:
                for msg_idx, msg in enumerate(langchain_messages):
                    if msg_idx < last_screenshot_msg_idx and isinstance(msg, HumanMessage) and isinstance(msg.content, list):
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

            if not state.values:
                stream_input = {"messages": langchain_messages}
            else:
                existing_messages = state.values.get("messages", [])
                if existing_messages:
                    needed_responses = {}
                    for m in existing_messages:
                        tcs = getattr(m, "tool_calls", None)
                        if tcs:
                            for tc in tcs:
                                if "id" in tc:
                                    needed_responses[tc["id"]] = tc.get("name") or "tool"

                        tcid = getattr(m, "tool_call_id", None)
                        if tcid and tcid in needed_responses:
                            del needed_responses[tcid]

                    if needed_responses:
                        agent_view.log_to_ui(f"[dim yellow] Healing {len(needed_responses)} incomplete tool calls in checkpointer...[/dim yellow]")
                        healing_messages = []
                        for tid, tname in needed_responses.items():
                            healing_messages.append(ToolMessage(
                                content="[Tool execution was interrupted or cancelled during session transition.]",
                                name=tname,
                                tool_call_id=tid
                            ))

                        try:
                            executor.update_state(run_config, {"messages": healing_messages})
                            state = executor.get_state(run_config)
                            existing_messages = state.values.get("messages", [])
                        except Exception as he:
                            agent_view.log_to_ui(f"[dim red]Checkpoint patching failed: {he}[/dim red]")
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
                    if len(missing_messages) > 1:
                        executor.update_state(run_config, {"messages": missing_messages[:-1]})
                    stream_input = {"messages": [missing_messages[-1]]}
                else:
                    stream_input = None

            consecutive_fail_count = 0
            MAX_CONSECUTIVE_FAILS = 5
            
            while consecutive_fail_count < MAX_CONSECUTIVE_FAILS:
                try:
                    for event_type, event_data in executor.stream(stream_input, config=run_config, stream_mode=["messages", "updates"]):
                        if toolbox.ABORT_EVENT.is_set() or (batch_id != 0 and (batch_id != agent_view.current_batch_id or batch_id in agent_view.session_manager.aborted_batch_ids)):
                            raise Exception("Operation forcefully aborted or interrupted by user.")

                        if event_type == "messages":
                            chunk, metadata = event_data
                            if metadata.get("langgraph_node") == "agent" and isinstance(chunk, AIMessageChunk) and chunk.content:
                                text_chunk = str(chunk.content)
                                current_ai_text += text_chunk
                                full_ai_response += text_chunk
                                if getattr(agent_view, "tts_enabled", False):
                                    agent_view.tts_manager.stream_text(text_chunk, agent_name=agent.name, voice=agent.tts_voice)
                                if not has_mounted_ai_box:
                                    agent_view.app.call_from_thread(agent_view.mount_ai_message_box, agent.name, agent.color)
                                    has_mounted_ai_box = True
                                    
                                display_text = agent_view.render_latex_to_unicode_ext(current_ai_text)
                                agent_view.app.call_from_thread(agent_view.update_ai_message, display_text)
                                agent_view.app.call_after_refresh(lambda: agent_view.query_one("#ai_chat_scroll").scroll_end(animate=False))

                        elif event_type == "updates":
                            consecutive_fail_count = 0
                            
                            for node_name, node_data in event_data.items():
                                messages = node_data.get("messages", [])
                                if not isinstance(messages, list):
                                    messages = [messages]

                                if node_name == "agent":
                                    for msg in messages:
                                        if hasattr(msg, 'additional_kwargs') and 'thought' in msg.additional_kwargs:
                                            agent_view.log_to_ui(f"[dim]Thought:[/dim] {msg.additional_kwargs['thought']}")

                                        if getattr(agent_view, "current_telegram_chat_id", None) and current_ai_text.strip():
                                            tele_msg = f"Agent {agent.name.upper()}:\n\n{current_ai_text.strip()}"
                                            agent_view.telegram_manager.send_message(
                                                agent_view.current_telegram_chat_id, 
                                                tele_msg, 
                                                title=agent.name, 
                                                voice=agent.tts_voice
                                            )

                                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                                            for tc in msg.tool_calls:
                                                tool_calls.append(tc)
                                                call_text = f"[#808080]Calling Tool: {tc['name']} with args: {str(tc['args'])}[/#808080]"
                                                agent_view.write_message_block(f"[bold {agent.color}]{agent.name} (Tool Call):[/bold {agent.color}]", call_text, agent.color, is_markdown=False)

                                        if getattr(agent_view, "tts_enabled", False):
                                            agent_view.tts_manager.flush_stream(agent_name=agent.name, voice=agent.tts_voice)
                                        has_mounted_ai_box = False
                                        current_ai_text = ""

                                elif node_name == "tools":
                                    for msg in messages:
                                        tool_name = getattr(msg, 'name', 'tool')
                                        tool_call_id = getattr(msg, 'tool_call_id', None)
                                        
                                        content_to_save = msg.content
                                        if isinstance(msg.content, list):
                                            reconstructed = ""
                                            for block in msg.content:
                                                if block.get("type") == "text":
                                                    reconstructed += block.get("text", "")
                                                elif block.get("type") == "image_url":
                                                    reconstructed += "\n[ImageBase64: <data_transmitted>]\n"
                                            content_to_save = reconstructed
                                            
                                        tool_outputs.append({"name": tool_name, "content": content_to_save, "tool_call_id": tool_call_id})
                                        
                                        if "[Attached Image:" in str(content_to_save):
                                            img_match = re.search(r'\[Attached Image: (.*?)\]', str(content_to_save))
                                            if img_match:
                                                img_name = os.path.basename(img_match.group(1).strip())
                                                agent_view.log_to_ui(f"[#808080]Harness: Intercepted companion image `{img_name}` and queued for visual analysis.[/]")
                                        
                                        if tool_name in ["search_web", "SearchWeb"]:
                                            summary = "[Search results successfully parsed and delivered to active agent context]"
                                        else:
                                            summary_clean = str(content_to_save)
                                            summary_clean = re.sub(r'\[ImageBase64:\s*[^\]]+\]', '[ImageBase64: <data_transmitted>]', summary_clean)
                                            summary_clean = re.sub(r'data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=\s]{20,}', '<base64_data_omitted>', summary_clean)
                                            summary = (summary_clean + '...') if len(summary_clean) > 200 else summary_clean
                                        
                                        agent_view.app.call_from_thread(agent_view.render_tool_result_box, agent.name, agent.color, summary)
                    
                    if not full_ai_response.strip():
                        consecutive_fail_count += 1
                        if consecutive_fail_count < MAX_CONSECUTIVE_FAILS:
                            agent_view.log_to_ui(f"[bold yellow] Hmmmm. Lets see now... ({consecutive_fail_count}/{MAX_CONSECUTIVE_FAILS})...[/bold yellow]")
                            stream_input = {"messages": [HumanMessage(content="System Guardrail: You have not provided a text response to the user. Please continue your turn and provide a response.")]}
                            current_ai_text = ""
                            full_ai_response = ""
                            has_mounted_ai_box = False
                            continue
                        else:
                            raise ValueError("Empty response received from API after multiple retries")

                    break

                except Exception as stream_e:
                    err_msg = str(stream_e).lower()
                    consecutive_fail_count += 1
                    
                    if ("connection" in err_msg or "reset" in err_msg or "timeout" in err_msg or "429" in err_msg) and consecutive_fail_count < MAX_CONSECUTIVE_FAILS:
                        agent_view.log_to_ui(f"[yellow] Stream interrupted ({str(stream_e)}). Retrying {consecutive_fail_count}/{MAX_CONSECUTIVE_FAILS}...[/yellow]")
                        time.sleep(3)
                        stream_input = None
                        continue
                    raise stream_e

            if full_ai_response.strip() or tool_outputs or tool_calls:
                ai_response = full_ai_response.strip()
                agent_view.session_manager.broadcast_message(agent.name, ai_response, is_ai=True, tool_outputs=tool_outputs, tool_calls=tool_calls)
                agent_view.app.call_from_thread(agent_view.update_tokens)
                
                threading.Thread(target=trigger_background_naming_core, args=(agent_view, prompt, ai_response), daemon=True).start()
                
                new_seq_mentions = agent_view.agent_manager.get_mentions(ai_response)
                new_par_mentions = agent_view.agent_manager.get_parallel_mentions(ai_response)
                
                for m_name in new_par_mentions:
                    m_agent = agent_view.agent_manager.get_agent(m_name)
                    if m_agent and m_agent.name != agent.name:
                        agent_view.log_to_ui(f"[bold cyan]>> Parallel hand-off to {m_agent.name}...[/bold cyan]")
                        agent_view.session_manager.join_conversation(agent.name, m_agent, list(agent_view.agent_manager.agents.values()))
                        agent_view.app.call_from_thread(agent_view.run_agent_task, m_agent, prompt, None, batch_id)

                with agent_view.turn_lock:
                    for m_name in new_seq_mentions:
                        m_agent = agent_view.agent_manager.get_agent(m_name)
                        if m_agent and m_agent not in agent_view.turn_queue:
                            if m_agent.name != agent.name or len(new_par_mentions) > 0:
                                agent_view.turn_queue.append(m_agent)

            next_agent = None
            with agent_view.turn_lock:
                if "@askuser" in full_ai_response.lower() and agent_view.turn_queue:
                    agent_view.paused_queue = list(agent_view.turn_queue)
                    agent_view.turn_queue = []
                    agent_view.log_to_ui("[bold yellow]Queue paused by agent. Waiting for user input. Type @resume to continue.[/bold yellow]")

                if agent_view.turn_queue and len(agent_view._running_agents) <= 1 and len(new_par_mentions) == 0:
                    next_agent = agent_view.turn_queue.pop(0)

            if next_agent:
                agent_view.log_to_ui(f"[bold cyan]>> Sequential hand-off to {next_agent.name}...[/bold cyan]")
                agent_view.session_manager.join_conversation(agent.name, next_agent, list(agent_view.agent_manager.agents.values()))
                agent_view.app.call_from_thread(agent_view.run_agent_task, next_agent, prompt, None, batch_id)

        except (Exception, SystemExit) as e:
            error_str = str(e) if str(e) else "Operation forcefully aborted by user."

            if isinstance(e, SystemExit) or toolbox.ABORT_EVENT.is_set() or any(term in error_str.lower() for term in ["aborted", "interrupted"]):
                completed_ids = {o.get("tool_call_id") for o in tool_outputs if o.get("tool_call_id")}
                completed_names_count = {}
                for o in tool_outputs:
                    n = o.get("name")
                    if n: completed_names_count[n] = completed_names_count.get(n, 0) + 1

                for tc in tool_calls:
                    tc_id = tc.get("id")
                    tc_name = tc.get("name", "tool")
                    
                    is_completed = False
                    if tc_id and tc_id in completed_ids:
                        is_completed = True
                    elif not tc_id and completed_names_count.get(tc_name, 0) > 0:
                        completed_names_count[tc_name] -= 1
                        is_completed = True

                    if not is_completed:
                        aborted_output = {
                            "name": tc_name,
                            "content": "Error: Tool execution aborted by user.",
                            "tool_call_id": tc_id
                        }
                        tool_outputs.append(aborted_output)
                        agent_view.app.call_from_thread(agent_view.render_tool_error_box, agent.name, agent.color, "Error: Tool execution aborted by user.")

                if tool_outputs or tool_calls:
                    ai_resp = full_ai_response.strip() or "[Operation Aborted by User]"
                    agent_view.session_manager.broadcast_message(
                        agent.name, ai_resp, is_ai=True, tool_outputs=tool_outputs, tool_calls=tool_calls
                    )
                    agent_view.app.call_from_thread(agent_view.update_tokens)

                agent_view.log_to_ui("[bold red] Operation Aborted by User. Partial tool results saved.[/bold red]")
                return

            is_schema_or_api_error = any(term in error_str.lower() or term in repr(e).lower() for term in ["400", "invalid", "empty", "badrequest", "toolmessage", "tool_calls", "validation", "argument"])

            if is_schema_or_api_error and "_rst_" not in thread_id:
                agent_view.log_to_ui("[bold yellow] State Corruption or API Error Detected. Performing Automated Recovery...[/bold yellow]")
                new_thread_id = f"{thread_id}_rst_{int(time.time())}"
                agent_view._running_agents.discard(agent.name)
                return run_agent_task_core(agent_view, agent, prompt, override_thread_id=new_thread_id, batch_id=batch_id)

            agent_view.log_to_ui(f"[bold red]Execution Error ({agent.name}):[/bold red] {e}")
    finally:
        agent_view._running_agents.discard(agent.name)
        agent_view.app.call_from_thread(agent_view._toggle_spinner, False, agent.name, agent.color) 
        toolbox.unregister_thread()


def trigger_background_naming_core(agent_view, user_prompt: str, agent_response: str):
    session_id = agent_view.session_manager.current_session_id
    name_map = get_session_name_map()
    if session_id in name_map:
        return
        
    agent = agent_view.active_agent
    if agent.use_backup and agent.backup_model:
        model = agent.backup_model
        base_url = agent.backup_base_url or agent.base_url
        api_key = agent.get_backup_api_key() or agent.get_api_key()
    else:
        model = agent.model
        base_url = agent.base_url
        api_key = agent.get_api_key()

    if not api_key:
        return
        
    try:
        effort = getattr(agent, "reasoning_effort", "none")
        extra_args = {"model_kwargs": {"reasoning_effort": effort}} if effort not in ("none", None, "") else {}
        llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=0,
            max_retries=1,
            **extra_args
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
            agent_view.log_to_ui(f"[bold green]Session Named: {name}[/]")
    except Exception:
        pass


def compress_history_core(agent_view):
    agent_view.log_to_ui("Analyzing chat history for technical compression...", is_markdown=False)
    
    global_config = toolbox.load_global_settings()
    keep_verbatim_count = int(global_config.get("keep_verbatim_count", 2))
    
    comp_threshold = keep_verbatim_count + 2

    history = agent_view.session_manager.active_sessions.get(agent_view.active_agent.name, [])
    if len(history) <= comp_threshold:
        agent_view.log_to_ui(f"Chat history is too short to compress safely (requires > {comp_threshold} turns).", is_markdown=False)
        return

    to_summarize = history[1:-keep_verbatim_count]
    verbatim_suffix = history[-keep_verbatim_count:]
    
    formatted_history = []
    for msg in to_summarize:
        role_disp = "User" if msg.role == "human" else "Agent"
        msg_text = msg.content or ""
        if getattr(msg, "tool_outputs", None):
            for out in msg.tool_outputs:
                msg_text += f"\n[Tool {out.get('name', 'Unknown')} Output]: {out.get('content', '')}"
        formatted_history.append(f"[{role_disp}]: {msg_text}")
    history_text = "\n".join(formatted_history)
    
    agent = agent_view.active_agent
    if agent.use_backup and agent.backup_model:
        model = agent.backup_model
        base_url = agent.backup_base_url or agent.base_url
        api_key = agent.get_backup_api_key()
    else:
        model = agent.model
        base_url = agent.base_url
        api_key = agent.get_api_key()
        
    if not api_key:
        agent_view.log_to_ui("[bold red]Error: Active agent API key is missing. Compression aborted.[/bold red]")
        return
        
    try:
        llm = ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=0)
        
        extracted_image_tags = []
        vision_payload = []
        
        for m in re.finditer(r'\[Attached Image:\s*(.*?)\]', history_text):
            tag = m.group(0)
            filepath = m.group(1).strip()
            if tag not in extracted_image_tags:
                extracted_image_tags.append(tag)
                if agent.is_capable_vision and os.path.exists(filepath) and os.path.getsize(filepath) > 0 and not filepath.lower().endswith(".pdf"):
                    try:
                        mime = mimetypes.guess_type(filepath)[0] or "image/png"
                        with open(filepath, "rb") as img_f:
                            b64 = base64.b64encode(img_f.read()).decode('utf-8').replace('\n', '').replace('\r', '')
                        vision_payload.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                    except Exception:
                        pass

        compress_dir = os.path.join(agent_view.session_manager.sessions_dir, "compressed_images")
        os.makedirs(compress_dir, exist_ok=True)
        
        for m in re.finditer(r'\[ImageBase64:\s*(data:image/([a-zA-Z]+);base64,([^\]]+))\]', history_text):
            tag = m.group(0)
            full_data = m.group(1).strip().replace("\n", "").replace("\r", "").replace(" ", "")
            ext = m.group(2)
            b64_data = m.group(3).strip().replace("\n", "").replace("\r", "").replace(" ", "")
            
            if any(marker in full_data for marker in ["{", "}", "<", ">", "b64_str", "base64data"]):
                continue
                
            new_filename = f"compressed_{int(time.time() * 1000)}_{len(extracted_image_tags)}.{ext}"
            new_filepath = os.path.join(compress_dir, new_filename)
            
            try:
                with open(new_filepath, "wb") as f:
                    f.write(base64.b64decode(b64_data))
                
                new_tag = f"[Attached Image: {new_filepath}]"
                if new_tag not in extracted_image_tags:
                    extracted_image_tags.append(new_tag)
                    if agent.is_capable_vision:
                        vision_payload.append({"type": "image_url", "image_url": {"url": full_data}})
            except Exception:
                pass
                    
        history_text = re.sub(r'\[ImageBase64:\s*data:image/[a-zA-Z]+;base64,[^\]]+\]', '[ImageBase64: <data_transmitted>]', history_text)
        
        comp_prompt = f"""
        You are {agent.name}. {agent.backstory}
        You are summarizing YOUR OWN conversation history to save memory.
        Write the summary from your own first-person perspective ("I", "my") so that when you read it later, you seamlessly remember what YOU did, what you saw, and what the User said.
        
        Analyze the intermediate conversation history below. Generate a dense, technical, and precise Markdown state summary.
        
        The summary MUST capture:
        1. Active project paths, files being edited, and exact workspace parameters.
        2. Hard technical decisions made and agreed-upon designs/architectures.
        3. Discovered issues, constraints, errors, or dependencies.
        4. User preferences, goals, and pending tasks.
        5. Any facts or visual details established in the conversation so far.
        
        If you see a message starting with [SYSTEM HISTORICAL RECALL SUMMARY] then understand this conversation has been summarized before. 
        Consider how the conversation has progressed since the last summary and construct your current summary such that all the details of the older summary are retained while updating it with the progress made since then.
        
        Do not lose technical specificity (such as exact filenames, code, functions, paths, or keys).
        
        CONVERSATION TO SUMMARIZE:
        {history_text}
        """
        
        if agent.is_capable_vision and vision_payload:
            content_list = [{"type": "text", "text": comp_prompt}]
            content_list.extend(vision_payload)
            msg_to_send = HumanMessage(content=content_list)
        else:
            msg_to_send = HumanMessage(content=comp_prompt)
        
        res = llm.invoke([msg_to_send])
        summary_content = f"### [SYSTEM HISTORICAL RECALL SUMMARY]\n{res.content}"
        
        summary_message = HistoryMessage(role="ai", content=summary_content)
        
        if extracted_image_tags:
            image_message = HistoryMessage(
                role="human", 
                content="### [Images preserved from compressed history]\n" + "\n".join(extracted_image_tags)
            )
            new_history = [history[0], summary_message, image_message] + verbatim_suffix
        else:
            new_history = [history[0], summary_message] + verbatim_suffix
        
        agent_view.session_manager.active_sessions[agent_view.active_agent.name] = new_history
        agent_view.session_manager.save_session(agent_view.active_agent.name)
        
        try:
            thread_id = f"{agent_view.session_manager.current_session_id}_{agent_view.active_agent.name}"
            from toolbox import shared_db_conn
            cursor = shared_db_conn.cursor()
            cursor.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
            cursor.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
            shared_db_conn.commit()
        except Exception as e:
            agent_view.log_to_ui(f"[dim red]Checkpointer sync error: {e}[/dim red]")
            
        agent_view.log_to_ui("[bold green]Chat context successfully compressed semantically.[/bold green]")
        agent_view.update_tokens()
        
    except Exception as e:
        agent_view.log_to_ui(f"[bold red]Inference compression error: {e}[/bold red]")