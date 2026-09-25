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
from typing import Optional, List, Dict, Any

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
from orchestration import AgentConfig, HistoryMessage, normalize_msg_content
from subagents import dispatch_coding_subagent
import hashlib
import secrets

_BACKSTORY_LOCK = threading.Lock()

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
    
    is_nomem = getattr(agent_view.session_manager, "is_no_memory", lambda: False)()
    NOMEM_EXCLUDED_TOOLS = {"update_core_memory", "search_episodic_memory", "retrieve_episodic_memory", "mark_quagmire", "distill_journey"}

    if agent_config.disable_all_tools:
        tools = [
            toolbox.update_core_memory, toolbox.save_skill, toolbox.read_skill, toolbox.list_skills, 
            toolbox.distill_journey, toolbox.delete_passive_skill, toolbox.mark_quagmire, 
            toolbox.get_user_clarification, toolbox.search_episodic_memory, toolbox.retrieve_episodic_memory,
            toolbox.get_toolresult
        ]
        if is_nomem:
            tools = [t for t in tools if getattr(t, "name", "") not in NOMEM_EXCLUDED_TOOLS]
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
        if is_nomem:
            raw_tools = [t for t in raw_tools if getattr(t, "name", "") not in NOMEM_EXCLUDED_TOOLS]
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

        try:
            for mt in toolbox.load_mcp_tools():
                high_priv_map[mt.name] = mt
        except Exception:
            pass

        _SERIAL_TOOL_LOCK = threading.Lock()
        MEMORY_TOOL_NAMES = {
            "update_core_memory", "save_skill", "read_skill", "list_skills",
            "distill_journey", "delete_passive_skill", "mark_quagmire",
            "get_user_clarification", "search_episodic_memory", "retrieve_episodic_memory",
            "set_toolresult"
        }

        def make_wrapped_tool(t_obj, requires_confirmation=False):
            def wrapped_func(*args, config=None, **kwargs):
                agent_name = agent_config.name
                toolbox.thread_context.agent_name = agent_name
                
                # Acquire serial lock so simultaneous tool calls are evaluated strictly in series
                with _SERIAL_TOOL_LOCK:
                    # Check for abort before evaluating
                    if toolbox.ABORT_EVENT.is_set():
                        return "Error: Tool execution aborted by user."

                    if requires_confirmation:
                        # Pre-validate file operations before bothering the user with a confirmation popup
                        if t_obj.name == "edit_file":
                            from toolbox import get_safe_path
                            fp = kwargs.get("filepath") or kwargs.get("file_path") or kwargs.get("path") or ""
                            search = kwargs.get("search") or kwargs.get("old_str") or kwargs.get("old_content")
                            if not fp:
                                return "Error editing file: No filepath provided."
                            try:
                                safe_path, display_path = get_safe_path(fp)
                                if not os.path.exists(safe_path):
                                    return f"Error editing file: File '{display_path}' does not exist."
                                with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
                                    content = f.read()
                                if search is not None:
                                    norm_content = content.replace("\r\n", "\n")
                                    norm_search = search.replace("\r\n", "\n")
                                    count = norm_content.count(norm_search)
                                    if count == 0:
                                        return f"Error editing file: No match found for the search block in '{display_path}'. Please re-read the file with read_file to get exact indentation/content and retry."
                                    if count > 1:
                                        return f"Error editing file: Multiple matches ({count}) found for the search block in '{display_path}'. Please provide more surrounding context."
                            except Exception as pre_err:
                                return f"Error pre-validating edit: {pre_err}"

                        confirmed = agent_view.confirm_tool_execution(t_obj.name, kwargs, agent_name=agent_name)
                        if not confirmed:
                            return f"Error: Tool execution of '{t_obj.name}' was rejected by the user."

                    call_kwargs = dict(kwargs)
                    func_to_call = t_obj.func if hasattr(t_obj, "func") and callable(t_obj.func) else t_obj
                    if config is not None:
                        try:
                            import inspect
                            sig = inspect.signature(func_to_call)
                            if "config" in sig.parameters:
                                call_kwargs["config"] = config
                        except Exception:
                            pass

                    raw_res = func_to_call(*args, **call_kwargs)

                    if t_obj.name in MEMORY_TOOL_NAMES:
                        return raw_res

                    compressed_res, original_res = toolbox.apply_minicompress_if_needed(
                        tool_name=t_obj.name,
                        tool_args=kwargs,
                        result=raw_res,
                        agent_config=agent_config
                    )
                    if compressed_res != original_res:
                        toolbox.store_raw_tool_result(t_obj.name, kwargs, original_res)
                    return compressed_res

            return StructuredTool(
                name=t_obj.name,
                description=t_obj.description,
                args_schema=t_obj.args_schema,
                func=wrapped_func
            )

        if agent_view.agent_mode == "EXECUTE":
            for tname, tool_obj in high_priv_map.items():
                raw_tools.append(make_wrapped_tool(tool_obj, requires_confirmation=False))
        elif agent_view.agent_mode == "INTERMEDIATE":
            for tname, tool_obj in high_priv_map.items():
                raw_tools.append(make_wrapped_tool(tool_obj, requires_confirmation=True))
        else: # PLAN (SAFE) Mode
            for tname in agent_config.enabled_tools:
                if tname == "visual_computer_operation":
                    for ct in ["take_screenshot", "click_at_current_location", "move_cursor_absolute", "move_cursor_relative", "send_scroll", "inject_keyboard_input"]:
                        raw_tools.append(make_wrapped_tool(high_priv_map[ct], requires_confirmation=True))
                elif tname in high_priv_map:
                    raw_tools.append(make_wrapped_tool(high_priv_map[tname], requires_confirmation=True))
        
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
            elif t_obj.name in MEMORY_TOOL_NAMES:
                final_tools.append(t_obj)
                allowed_names.add(t_obj.name)
            else:
                wrapped = t_obj if getattr(t_obj.func, "__name__", "") == "wrapped_func" else make_wrapped_tool(t_obj, requires_confirmation=False)
                final_tools.append(wrapped)
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
    toolbox.thread_context.agent_name = agent.name    
    toolbox.thread_context.batch_id = batch_id
    try:
        try:
            translate_team_backstories_core(agent_view, agent, list(agent_view.agent_manager.agents.values()))
        except Exception:
            pass

        try:
            history = agent_view.session_manager.active_sessions.get(agent.name, [])
            if history and history[0].role == "system":
                history[0].content = agent.get_full_system_prompt(list(agent_view.agent_manager.agents.values()), is_no_memory=agent_view.session_manager.is_no_memory())
        except Exception:
            pass

        try:
            check_and_run_precompress(agent_view, agent, prompt)
        except Exception as pe:
            agent_view.log_to_ui(f"[dim red]Pre-compression error: {pe}[/dim red]")

        executor = agent_view.get_executor(agent)
        if not executor:
            agent_view.log_to_ui(f"[bold red]Agent {agent.name} not configured (Key missing).[/bold red]")
            return
        
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
                                tool_content = str(output.get("compressed_content") or output.get("content", ""))
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
                                                call_text = f"Calling Tool: {tc['name']} with args: {str(tc['args'])}"
                                                agent_view.write_message_block(f"[bold {agent.color}]{agent.name} (Tool Call):[/bold {agent.color}]", call_text, "#808080", is_markdown=False)

                                        if getattr(agent_view, "tts_enabled", False):
                                            agent_view.tts_manager.flush_stream(agent_name=agent.name, voice=agent.tts_voice)
                                        has_mounted_ai_box = False
                                        current_ai_text = ""

                                elif node_name == "tools":
                                    for msg in messages:
                                        tool_name = getattr(msg, 'name', 'tool')
                                        tool_call_id = getattr(msg, 'tool_call_id', None)
                                        
                                        display_content = msg.content
                                        if isinstance(display_content, list):
                                            reconstructed = ""
                                            for block in display_content:
                                                if block.get("type") == "text":
                                                    reconstructed += block.get("text", "")
                                                elif block.get("type") == "image_url":
                                                    reconstructed += "\n[ImageBase64: <data_transmitted>]\n"
                                            display_content = reconstructed

                                        content_to_save = display_content
                                        raw_popped = toolbox.pop_raw_tool_result(tool_name)
                                        if raw_popped is not None:
                                            content_to_save = raw_popped

                                        output_entry = {"name": tool_name, "content": content_to_save, "tool_call_id": tool_call_id}
                                        if raw_popped is not None:
                                            output_entry["compressed_content"] = display_content
                                        tool_outputs.append(output_entry)
                                        
                                        if "[Attached Image:" in str(content_to_save):
                                            img_match = re.search(r'\[Attached Image: (.*?)\]', str(content_to_save))
                                            if img_match:
                                                img_name = os.path.basename(img_match.group(1).strip())
                                                agent_view.log_to_ui(f"[#808080]Harness: Intercepted companion image `{img_name}` and queued for visual analysis.[/]")
                                        
                                        if tool_name in ["search_web", "SearchWeb"]:
                                            summary = "[Search results successfully parsed and delivered to active agent context]"
                                        else:
                                            # Render the compressed display_content on the UI
                                            summary_clean = str(display_content)
                                            summary_clean = re.sub(r'\[ImageBase64:\s*[^\]]+\]', '[ImageBase64: <data_transmitted>]', summary_clean)
                                            summary_clean = re.sub(r'data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=\s]{20,}', '<base64_data_omitted>', summary_clean)
                                            summary = (summary_clean + '...') if len(summary_clean) > 400 else summary_clean
                                        
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
                
                check_and_run_autocompress(agent_view, agent)
                
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

            # If this task is from an older superseded batch, discard quietly without touching the UI
            is_stale_task = (batch_id != 0 and batch_id != getattr(agent_view, "current_batch_id", 0))
            if is_stale_task:
                return

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
        # Only discard running agent state if this was the current active batch
        is_current_task = (batch_id == 0 or batch_id == getattr(agent_view, "current_batch_id", 0))
        if is_current_task:
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


def estimate_history_tokens(history: list, agent_config: AgentConfig) -> float:
    token_char_num = float(getattr(agent_config, "token_equivalent_char_number", 3.9) or 3.9)
    if token_char_num <= 0:
        token_char_num = 3.9
    total_chars = 0
    for hm in history:
        content = getattr(hm, "content", None) if isinstance(hm, HistoryMessage) else hm.get("content")
        if content:
            total_chars += len(content) if isinstance(content, str) else len(str(content))
        t_calls = getattr(hm, "tool_calls", None) if isinstance(hm, HistoryMessage) else hm.get("tool_calls")
        if t_calls:
            total_chars += len(str(t_calls))
        t_outs = getattr(hm, "tool_outputs", None) if isinstance(hm, HistoryMessage) else hm.get("tool_outputs")
        if t_outs:
            for out in t_outs:
                if isinstance(out, dict):
                    c = out.get("compressed_content") or out.get("content") or ""
                    total_chars += len(c) if isinstance(c, str) else len(str(c))
    return total_chars / token_char_num


def get_last_responder_agent(agent_view, current_agent_name: str) -> Optional[AgentConfig]:
    """Finds the most recent colleague who responded before the current turn and has valid API credentials."""
    history = agent_view.session_manager.active_sessions.get(current_agent_name, [])
    for msg in reversed(history):
        if msg.role == "human" and msg.content:
            m = re.search(r'<AGENT_INTERCOM\s+sender="([^"]+)">', msg.content)
            if m:
                s_name = m.group(1).strip()
                if s_name != current_agent_name:
                    s_agent = agent_view.agent_manager.get_agent(s_name)
                    if s_agent:
                        k = s_agent.get_backup_api_key() if (s_agent.use_backup and s_agent.backup_model) else s_agent.get_api_key()
                        if k:
                            return s_agent
            m_tool = re.search(r'<AGENT_INTERCOM_TOOL_RESPONSE\s+agent="([^"]+)"', msg.content)
            if m_tool:
                s_name = m_tool.group(1).strip()
                if s_name != current_agent_name:
                    s_agent = agent_view.agent_manager.get_agent(s_name)
                    if s_agent:
                        k = s_agent.get_backup_api_key() if (s_agent.use_backup and s_agent.backup_model) else s_agent.get_api_key()
                        if k:
                            return s_agent
        elif msg.role == "ai":
            break

    for o_name, o_hist in agent_view.session_manager.active_sessions.items():
        if o_name != current_agent_name and o_hist:
            if o_hist[-1].role == "ai":
                s_agent = agent_view.agent_manager.get_agent(o_name)
                if s_agent:
                    k = s_agent.get_backup_api_key() if (s_agent.use_backup and s_agent.backup_model) else s_agent.get_api_key()
                    if k:
                        return s_agent
    return None


def check_and_run_precompress(agent_view, agent: AgentConfig, incoming_prompt: str = ""):
    """Pre-compression gate: checks if history + incoming turn exceeds context before LLM invocation."""
    history = agent_view.session_manager.active_sessions.get(agent.name, [])
    if len(history) <= 1:
        return
    max_tokens = int(getattr(agent, "max_tokens", 256000) or 256000)
    token_char_num = float(getattr(agent, "token_equivalent_char_number", 3.9) or 3.9)
    if token_char_num <= 0:
        token_char_num = 3.9
    est_tokens = estimate_history_tokens(history, agent) + (len(incoming_prompt) / token_char_num)
    if est_tokens > max_tokens:
        mode = toolbox.load_global_settings().get("precompress_mode", "self")
        borrowed = get_last_responder_agent(agent_view, agent.name) if mode == "borrow" else None
        borrow_msg = f" (delegating to {borrowed.name}'s model)" if borrowed else " (using self fallback)"
        agent_view.log_to_ui(
            f"[bold yellow]Pre-compress: {agent.name}'s context ({est_tokens:.0f} tokens) exceeds limit ({max_tokens}){borrow_msg}. Compressing history before execution...[/bold yellow]"
        )
        compress_history_core(agent_view, target_agent=agent, is_autocompress=True, incoming_query=incoming_prompt, borrowed_agent=borrowed)


def check_and_run_autocompress(agent_view, agent: AgentConfig):
    history = agent_view.session_manager.active_sessions.get(agent.name, [])
    if len(history) <= 1:
        return
    max_tokens = int(getattr(agent, "max_tokens", 256000) or 256000)
    est_tokens = estimate_history_tokens(history, agent)
    if est_tokens > max_tokens:
        agent_view.log_to_ui(
            f"[bold yellow]Post-compress: {agent.name}'s context ({est_tokens:.0f} tokens) exceeded max_tokens ({max_tokens}). Running autocompress...[/bold yellow]"
        )
        compress_history_core(agent_view, target_agent=agent, is_autocompress=True)


def compress_history_core(agent_view, target_agent: AgentConfig = None, is_autocompress: bool = False, incoming_query: str = "", borrowed_agent: AgentConfig = None):
    agent = target_agent or agent_view.active_agent
    if is_autocompress:
        agent_view.log_to_ui(f"[dim yellow]Post-compress: Analyzing chat history for {agent.name}...[/dim yellow]", is_markdown=False)
    else:
        agent_view.log_to_ui(f"Analyzing chat history for {agent.name}...", is_markdown=False)

    history = agent_view.session_manager.active_sessions.get(agent.name, [])
    initial_tokens = estimate_history_tokens(history, agent)
    
    # --- DELTA COMPRESSION UPGRADE ---
    existing_summary_text = ""
    start_idx = 1
    if len(history) > 1 and history[1].role == "ai" and "[SYSTEM HISTORICAL RECALL SUMMARY]" in (history[1].content or ""):
        existing_summary_text = history[1].content
        # Clean it up so we don't nest headers and watermarks
        existing_summary_text = re.sub(r'^(?:#+\s*)?\[?SYSTEM HISTORICAL RECALL SUMMARY\]?:?\s*', '', existing_summary_text, flags=re.IGNORECASE).strip()
        existing_summary_text = re.sub(r'<!--\s*WATERMARK:[^>]*-->\s*', '', existing_summary_text).strip()
        start_idx = 2

    if is_autocompress:
        if len(history) <= start_idx:
            return
        to_summarize = history[start_idx:]
        verbatim_suffix = []
    else:
        global_config = toolbox.load_global_settings()
        keep_verbatim_count = int(global_config.get("keep_verbatim_count", 2))
        comp_threshold = start_idx + keep_verbatim_count

        if len(history) <= comp_threshold:
            agent_view.log_to_ui(f"Chat history is too short to compress safely (requires > {comp_threshold} turns).", is_markdown=False)
            return

        to_summarize = history[start_idx:-keep_verbatim_count]
        verbatim_suffix = history[-keep_verbatim_count:]
    
    formatted_history = []
    for msg in to_summarize:
        role_disp = "User" if msg.role == "human" else "Agent"
        msg_text = msg.content or ""
        if getattr(msg, "tool_outputs", None):
            for out in msg.tool_outputs:
                c = out.get("compressed_content") or out.get("content", "")
                msg_text += f"\n[Tool {out.get('name', 'Unknown')} Output]: {c}"
        formatted_history.append(f"[{role_disp}]: {msg_text}")
    history_text = "\n".join(formatted_history)

    # Preserve all tool call stubs from the compressed region so other agents can fetch them via get_toolresult
    verbatim_ids = set()
    for msg in verbatim_suffix:
        if getattr(msg, "tool_outputs", None):
            for out in msg.tool_outputs:
                if isinstance(out, dict) and out.get("global_id"):
                    verbatim_ids.add(str(out["global_id"]))
        if msg.content:
            for m in re.finditer(r'id="(\d+)"', msg.content):
                verbatim_ids.add(m.group(1))

    preserved_stubs = []
    seen_stub_ids = set(verbatim_ids)

    for msg in to_summarize:
        if getattr(msg, "tool_outputs", None):
            for out in msg.tool_outputs:
                if isinstance(out, dict):
                    gid = out.get("global_id")
                    if gid and str(gid) not in seen_stub_ids:
                        seen_stub_ids.add(str(gid))
                        t_name = out.get("name", "tool")
                        args_str = str(out.get("args") or "None")
                        ts = out.get("timestamp", "")
                        stub = (
                            f"[Tool Output Hidden]\n"
                            f"- Tool Name: {t_name}\n"
                            f"- Result ID: {gid}\n"
                            f"- Arguments: {args_str}\n"
                            f"- Time: {ts}\n"
                            f"- Action: Use get_toolresult(ids=[{gid}]) to read output. (Combine multiple IDs into one list e.g. ids=[{gid}, ...])"
                        )
                        stub_tag = f'<AGENT_INTERCOM_TOOL_RESPONSE agent="{agent.name}" tool="{t_name}" id="{gid}">\n{stub}\n</AGENT_INTERCOM_TOOL_RESPONSE>'
                        preserved_stubs.append(stub_tag)

        if msg.content and "<AGENT_INTERCOM_TOOL_RESPONSE" in msg.content:
            for m in re.finditer(r'<AGENT_INTERCOM_TOOL_RESPONSE[^>]*id="(\d+)"[^>]*>[\s\S]*?</AGENT_INTERCOM_TOOL_RESPONSE>', msg.content):
                gid_str = m.group(1)
                if gid_str not in seen_stub_ids:
                    seen_stub_ids.add(gid_str)
                    preserved_stubs.append(m.group(0))

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

    def _build_comp_prompt(text_chunk: str, is_rolling: bool = False, existing_sum: str = "") -> str:
        query_sec = ""
        if incoming_query and incoming_query.strip():
            query_sec = f"""
TARGET INCOMING USER QUERY / DIRECTIVE (Retain all context required to answer this):
\"\"\"
{incoming_query.strip()}
\"\"\"
"""
        if is_rolling and existing_sum:
            return f"""You are an autonomous AI summarization worker compressing conversation history on behalf of {agent.name}.
Write from {agent.name}'s first-person perspective ("I", "my") so {agent.name} seamlessly retains its memory.
{query_sec}
EXISTING SUMMARY OF PRIOR CONVERSATION:
\"\"\"
{existing_sum}
\"\"\"

SUBSEQUENT CONVERSATION CHUNK:
\"\"\"
{text_chunk}
\"\"\"

TASK:
Synthesize the existing summary and the subsequent conversation chunk into an updated, dense Markdown state summary from {agent.name}'s perspective.
Preserve all user requirements, key actions, code decisions, file paths, variables, and context needed to address the incoming directive."""
        else:
            return f"""You are an autonomous AI summarization worker compressing conversation history on behalf of {agent.name}.
Write the summary in the first-person perspective ("I", "my") from {agent.name}'s perspective so {agent.name} seamlessly remembers its actions and dialogue.
{query_sec}
Analyze the conversation history below. Generate a dense, technical, and precise Markdown state summary.

The summary MUST capture:
1. User Directives & Intent: What the user asked for, constraints, and requirements.
2. Actions & Tool Outcomes: Actions/tools executed and findings.
3. Active project paths, files edited, and workspace parameters.
4. Hard technical decisions, architectures, and discovered issues.
5. Pending tasks and established facts.

If you see a message starting with [SYSTEM HISTORICAL RECALL SUMMARY], integrate its details while updating with new progress.
Do not lose technical specificity (filenames, code snippets, functions, paths).

CONVERSATION TO SUMMARIZE:
{text_chunk}"""

    raw_text = None

    # --- Strategy 1: Delegate to Last Responder's Model ---
    if borrowed_agent:
        try:
            if borrowed_agent.use_backup and borrowed_agent.backup_model:
                b_model = borrowed_agent.backup_model
                b_base_url = borrowed_agent.backup_base_url or borrowed_agent.base_url
                b_api_key = borrowed_agent.get_backup_api_key() or borrowed_agent.get_api_key()
            else:
                b_model = borrowed_agent.model
                b_base_url = borrowed_agent.base_url
                b_api_key = borrowed_agent.get_api_key()

            if b_api_key:
                b_effort = getattr(borrowed_agent, "reasoning_effort", "none")
                b_extra = {"model_kwargs": {"reasoning_effort": b_effort}} if b_effort not in ("none", None, "") else {}
                b_llm = ChatOpenAI(model=b_model, api_key=b_api_key, base_url=b_base_url, temperature=0, timeout=120, **b_extra)
                
                comp_prompt = _build_comp_prompt(history_text, is_rolling=bool(existing_summary_text), existing_sum=existing_summary_text)
                if agent.is_capable_vision and vision_payload:
                    msg_to_send = HumanMessage(content=[{"type": "text", "text": comp_prompt}] + vision_payload)
                else:
                    msg_to_send = HumanMessage(content=comp_prompt)

                res = toolbox.resilient_invoke(b_llm, [msg_to_send])
                if res and res.content:
                    raw_text = res.content.strip()
        except Exception as be:
            agent_view.log_to_ui(f"[dim yellow]Delegated compression via {borrowed_agent.name} failed ({be}). Falling back to chunked compression...[/dim yellow]")

    # --- Strategy 2: Fallback to Agent's Own Model (Chunked if needed) ---
    if not raw_text:
        if agent.use_backup and agent.backup_model:
            model = agent.backup_model
            base_url = agent.backup_base_url or agent.base_url
            api_key = agent.get_backup_api_key() or agent.get_api_key()
        else:
            model = agent.model
            base_url = agent.base_url
            api_key = agent.get_api_key()
            
        if not api_key:
            agent_view.log_to_ui(f"[bold red]Error: {agent.name} API key is missing. Compression aborted.[/bold red]")
            return
            
        try:
            effort = getattr(agent, "reasoning_effort", "none")
            extra_args = {"model_kwargs": {"reasoning_effort": effort}} if effort not in ("none", None, "") else {}
            llm = ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=0, timeout=120, **extra_args)

            token_char_num = float(getattr(agent, "token_equivalent_char_number", 3.9) or 3.9)
            if token_char_num <= 0:
                token_char_num = 3.9
            max_chunk_chars = int((getattr(agent, "max_tokens", 256000) or 256000) * token_char_num) - 6000
            max_chunk_chars = max(2000, max_chunk_chars)

            if len(history_text) <= max_chunk_chars:
                comp_prompt = _build_comp_prompt(history_text, is_rolling=bool(existing_summary_text), existing_sum=existing_summary_text)
                if agent.is_capable_vision and vision_payload:
                    msg_to_send = HumanMessage(content=[{"type": "text", "text": comp_prompt}] + vision_payload)
                else:
                    msg_to_send = HumanMessage(content=comp_prompt)
                res = toolbox.resilient_invoke(llm, [msg_to_send])
                raw_text = res.content.strip() if res and res.content else ""
            else:
                # Chunked rolling minicompress
                chunks = [history_text[i:i + max_chunk_chars] for i in range(0, len(history_text), max_chunk_chars)]
                rolling_summary = existing_summary_text
                for idx, ch in enumerate(chunks, 1):
                    toolbox.check_abort()
                    agent_view.log_to_ui(f"[dim cyan]Chunked compress ({agent.name}): Pass {idx}/{len(chunks)}...[/dim cyan]")
                    p = _build_comp_prompt(ch, is_rolling=(idx > 1 or bool(existing_summary_text)), existing_sum=rolling_summary)
                    res = toolbox.resilient_invoke(llm, [HumanMessage(content=p)])
                    rolling_summary = res.content.strip() if res and res.content else rolling_summary
                raw_text = rolling_summary
        except Exception as e:
            agent_view.log_to_ui(f"[bold red]Inference compression error ({agent.name}): {e}[/bold red]")
            return

    clean_text = re.sub(r'^(?:#+\s*)?\[?SYSTEM HISTORICAL RECALL SUMMARY\]?:?\s*', '', raw_text or "", flags=re.IGNORECASE).strip()
    clean_text = re.sub(r'<!--\s*WATERMARK:[^>]*-->\s*', '', clean_text).strip()

    cutoff_hash = ""
    if to_summarize:
        for m in reversed(to_summarize):
            if m.content and m.content.strip():
                norm = normalize_msg_content(m.content)
                if norm:
                    cutoff_hash = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
                    break

    if cutoff_hash:
        summary_content = f"### [SYSTEM HISTORICAL RECALL SUMMARY]\n<!-- WATERMARK: cutoff_hash=\"{cutoff_hash}\" -->\n{clean_text}"
    else:
        summary_content = f"### [SYSTEM HISTORICAL RECALL SUMMARY]\n{clean_text}"
    
    summary_message = HistoryMessage(role="ai", content=summary_content)
    stub_messages = [HistoryMessage(role="human", content=st) for st in preserved_stubs]
    
    if extracted_image_tags:
        image_message = HistoryMessage(
            role="human", 
            content="### [Images preserved from compressed history]\n" + "\n".join(extracted_image_tags)
        )
        new_history = [history[0], summary_message, image_message] + stub_messages + verbatim_suffix
    else:
        new_history = [history[0], summary_message] + stub_messages + verbatim_suffix
    
    agent_view.session_manager.active_sessions[agent.name] = new_history
    agent_view.session_manager.save_session(agent.name)
    final_tokens = estimate_history_tokens(new_history, agent)
    
    try:
        thread_id = f"{agent_view.session_manager.current_session_id}_{agent.name}"
        from toolbox import shared_db_conn
        cursor = shared_db_conn.cursor()
        cursor.execute("DELETE FROM checkpoints WHERE thread_id = ? OR thread_id LIKE ?", (thread_id, f"{thread_id}%"))
        cursor.execute("DELETE FROM writes WHERE thread_id = ? OR thread_id LIKE ?", (thread_id, f"{thread_id}%"))
        shared_db_conn.commit()
    except Exception as e:
        agent_view.log_to_ui(f"[dim red]Checkpointer sync error: {e}[/dim red]")
        
    agent_view.agent_executors.pop(agent.name, None)
    msg_status = "automatically" if is_autocompress else "semantically"
    token_diff_str = f" ({initial_tokens:.0f} ➔ {final_tokens:.0f} tokens)" if initial_tokens > 0 else f" ({final_tokens:.0f} tokens)"
    agent_view.log_to_ui(f"[bold green]Chat context for {agent.name} successfully compressed {msg_status}{token_diff_str}.[/bold green]")
    agent_view.app.call_from_thread(agent_view.update_tokens)