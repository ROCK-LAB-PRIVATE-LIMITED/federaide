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
import re
import json
import subprocess
import uuid
import time
import base64
import mimetypes
import inspect
import threading
from typing import List, Optional
from rich.markup import escape
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool, StructuredTool
from langgraph.prebuilt import create_react_agent
import toolbox
from toolbox import (
    read_file,
    save_file,
    edit_file,
    list_files,
    run_terminal_command,
    log_tool,
)

_SUBAGENT_TOOL_LOCK = threading.Lock()
_SUBAGENT_EXCLUDED_TOOLS = {"dispatch_subagent", "dispatch_coding_subagent", "perform_research", "update_inner_life"}
_SUBAGENT_MEMORY_TOOLS = {
    "update_core_memory", "save_skill", "read_skill", "list_skills",
    "distill_journey", "delete_passive_skill", "mark_quagmire",
    "get_user_clarification", "search_episodic_memory", "retrieve_episodic_memory",
    "set_toolresult",
}
_SUBAGENT_NOMEM_EXCLUDED = {
    "update_core_memory", "update_inner_life", "search_episodic_memory", "retrieve_episodic_memory",
    "mark_quagmire", "distill_journey",
}


def _resolve_dispatching_context():
    """Resolves (agent_view, agent_config, base_dir) for the actual dispatching agent."""
    agent_view = None
    agent_config = None
    base_dir = os.path.abspath(os.getcwd())

    if toolbox.CURRENT_APP:
        try:
            base_dir = os.path.abspath(str(toolbox.CURRENT_APP.query_one("#dir_tree").path))
        except Exception:
            pass
        try:
            agent_view = toolbox.CURRENT_APP.query_one("AIAgentView")
            caller_name = toolbox._get_agent()
            agent_config = agent_view.agent_manager.get_agent(caller_name) or agent_view.active_agent
        except Exception as e:
            log_tool(f"[dim red]Subagent config fetch error: {e}[/dim red]")

    return agent_view, agent_config, base_dir


def _build_subagent_llm(agent_config):
    """Builds a ChatOpenAI instance matching the dispatching agent's provider, effort, and multimodal hooks."""
    if agent_config.use_backup and agent_config.backup_model:
        model = agent_config.backup_model
        base_url = agent_config.backup_base_url or agent_config.base_url
        api_key = agent_config.get_backup_api_key() or agent_config.get_api_key()
    else:
        model = agent_config.model
        base_url = agent_config.base_url
        api_key = agent_config.get_api_key()

    if not api_key:
        return None

    effort = getattr(agent_config, "reasoning_effort", "none")
    extra_args = {"reasoning_effort": effort} if effort not in ("none", None, "") else {}
    llm = ChatOpenAI(
        model=model,
        temperature=getattr(agent_config, "temperature", 1.0),
        api_key=api_key,
        base_url=base_url,
        max_retries=5,
        timeout=120,
        disable_streaming=not getattr(agent_config, "enable_streaming", True),
        **extra_args,
    )

    def preprocess_messages(messages):
        if not isinstance(messages, list):
            return messages
        processed = []
        for msg in messages:
            if hasattr(msg, "content"):
                msg.content = toolbox.mask_secrets(msg.content)
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                msg.tool_calls = toolbox.mask_secrets(msg.tool_calls)
            processed.append(msg)
            if msg.__class__.__name__ == "ToolMessage" and isinstance(msg.content, str):
                tool_name = getattr(msg, "name", None)
                if tool_name in {"edit_file", "save_file", "list_files", "search_web", "perform_research", "manage_agenda"}:
                    continue
                if getattr(agent_config, "is_capable_vision", True) and "[Attached Image:" in msg.content:
                    for match in re.finditer(r'\[Attached Image:\s*(.*?)\]', msg.content):
                        filepath = match.group(1).strip()
                        try:
                            resolved_path, _ = toolbox.get_safe_path(filepath)
                            if os.path.exists(resolved_path):
                                mime = mimetypes.guess_type(resolved_path)[0] or "image/png"
                                with open(resolved_path, "rb") as f:
                                    b64 = base64.b64encode(f.read()).decode("utf-8")
                                processed.append(HumanMessage(content=[
                                    {"type": "text", "text": f"[Attached Image: {filepath}]"},
                                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                                ]))
                        except Exception:
                            pass
                if getattr(agent_config, "is_capable_vision", True) and "[ImageBase64:" in msg.content:
                    for match in re.finditer(r'\[ImageBase64:\s*(data:image/[a-zA-Z]+;base64,[^\]]+)\]', msg.content):
                        url = match.group(1).strip().replace("\n", "").replace("\r", "").replace(" ", "")
                        if any(marker in url for marker in ["{", "}", "<", ">", "b64_str", "base64data"]):
                            continue
                        msg.content = msg.content.replace(match.group(0), "[ImageBase64: <data_transmitted>]")
                        processed.append(HumanMessage(content=[
                            {"type": "text", "text": "[Dynamic Visual Output]:"},
                            {"type": "image_url", "image_url": {"url": url}},
                        ]))
        return processed

    orig_generate = llm._generate
    llm._generate = lambda messages, stop=None, run_manager=None, **kw: orig_generate(preprocess_messages(messages), stop=stop, run_manager=run_manager, **kw)
    orig_stream = llm._stream
    llm._stream = lambda messages, stop=None, run_manager=None, **kw: orig_stream(preprocess_messages(messages), stop=stop, run_manager=run_manager, **kw)
    return llm


def _build_subagent_tools(agent_view, agent_config, allowed_subset: Optional[set] = None):
    """Mirrors the dispatching agent's current tool permissions (PLAN / INTERMEDIATE / EXECUTE)."""
    agent_mode = getattr(agent_view, "agent_mode", "PLAN")
    is_nomem = getattr(agent_view.session_manager, "is_no_memory", lambda: False)()
    disabled_tools = set(getattr(agent_config, "disabled_tools", ["visual_computer_operation", "send_file_to_telegram"]))

    def is_tool_disabled(t_name: str) -> bool:
        if t_name in _SUBAGENT_EXCLUDED_TOOLS or t_name in disabled_tools:
            return True
        if allowed_subset is not None and t_name not in allowed_subset:
            return True
        computer_tools = {
            "take_screenshot", "click_at_current_location", "move_cursor_absolute",
            "move_cursor_relative", "send_scroll", "inject_keyboard_input",
        }
        if t_name in computer_tools and "visual_computer_operation" in disabled_tools:
            return True
        return False

    def make_wrapped_tool(t_obj, requires_confirmation=False):
        def wrapped_func(*args, config=None, **kwargs):
            toolbox.thread_context.agent_name = agent_config.name
            with _SUBAGENT_TOOL_LOCK:
                if toolbox.ABORT_EVENT.is_set():
                    return "Error: Tool execution aborted by user."
                toolbox.check_abort()

                if requires_confirmation:
                    if t_obj.name == "edit_file":
                        fp = kwargs.get("filepath") or kwargs.get("file_path") or kwargs.get("path") or ""
                        search = kwargs.get("search") or kwargs.get("old_str") or kwargs.get("old_content")
                        if not fp:
                            return "Error editing file: No filepath provided."
                        try:
                            safe_path, display_path = toolbox.get_safe_path(fp)
                            if not os.path.exists(safe_path):
                                return f"Error editing file: File '{display_path}' does not exist."
                            with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
                                content = f.read()
                            if search is not None:
                                norm_content = content.replace("\r\n", "\n")
                                norm_search = search.replace("\r\n", "\n")
                                count = norm_content.count(norm_search)
                                if count == 0:
                                    return f"Error editing file: No match found in '{display_path}'. Re-read and retry."
                                if count > 1:
                                    return f"Error editing file: Multiple matches ({count}) found in '{display_path}'."
                        except Exception as pre_err:
                            return f"Error pre-validating edit: {pre_err}"

                    confirmed, reject_comment = agent_view.confirm_tool_execution(
                        t_obj.name, kwargs, agent_name=f"{agent_config.name} (Subagent)"
                    )
                    if not confirmed:
                        msg = f"Error: Tool execution of '{t_obj.name}' was rejected by the user."
                        if reject_comment:
                            msg += f" User comment: {reject_comment}"
                        return msg

                call_kwargs = dict(kwargs)
                func_to_call = t_obj.func if hasattr(t_obj, "func") and callable(t_obj.func) else t_obj
                if config is not None:
                    try:
                        if "config" in inspect.signature(func_to_call).parameters:
                            call_kwargs["config"] = config
                    except Exception:
                        pass

                raw_res = func_to_call(*args, **call_kwargs)
                if t_obj.name in _SUBAGENT_MEMORY_TOOLS:
                    return raw_res
                compressed_res, _ = toolbox.apply_minicompress_if_needed(
                    tool_name=t_obj.name, tool_args=kwargs, result=raw_res, agent_config=agent_config
                )
                return compressed_res

        return StructuredTool(
            name=t_obj.name, description=t_obj.description, args_schema=t_obj.args_schema, func=wrapped_func
        )

    if getattr(agent_config, "disable_all_tools", False):
        tools = [
            toolbox.update_core_memory, toolbox.save_skill, toolbox.read_skill, toolbox.list_skills,
            toolbox.distill_journey, toolbox.delete_passive_skill, toolbox.mark_quagmire,
            toolbox.get_user_clarification, toolbox.search_episodic_memory, toolbox.retrieve_episodic_memory,
            toolbox.get_toolresult,
        ]
        if is_nomem:
            tools = [t for t in tools if getattr(t, "name", "") not in _SUBAGENT_NOMEM_EXCLUDED]
        if allowed_subset is not None:
            tools = [t for t in tools if getattr(t, "name", "") in allowed_subset]
        wrapped_tools = [make_wrapped_tool(t, requires_confirmation=False) for t in tools]
        return wrapped_tools, {t.name for t in wrapped_tools}

    raw_tools = [
        toolbox.search_web, toolbox.render_pdf, toolbox.update_core_memory, toolbox.save_skill,
        toolbox.read_skill, toolbox.distill_journey, toolbox.delete_passive_skill, toolbox.list_skills,
        toolbox.mark_quagmire, toolbox.get_user_clarification, toolbox.search_episodic_memory,
        toolbox.retrieve_episodic_memory, toolbox.prepare_active_skill, toolbox.finalize_active_skill,
        toolbox.manage_active_skill, toolbox.fix_active_skill, toolbox.get_toolresult, toolbox.send_team_dispatch,
    ]
    if is_nomem:
        raw_tools = [t for t in raw_tools if getattr(t, "name", "") not in _SUBAGENT_NOMEM_EXCLUDED]
    raw_tools.extend(toolbox.load_dynamic_tools(agent_config.name))

    high_priv_map = {
        "manage_agenda": toolbox.manage_agenda,
        "list_files": list_files,
        "read_file": read_file,
        "fetch_url": toolbox.fetch_url,
        "save_file": save_file,
        "edit_file": edit_file,
        "run_terminal_command": run_terminal_command,
        "take_screenshot": toolbox.take_screenshot,
        "click_at_current_location": toolbox.click_at_current_location,
        "move_cursor_absolute": toolbox.move_cursor_absolute,
        "move_cursor_relative": toolbox.move_cursor_relative,
        "send_scroll": toolbox.send_scroll,
        "inject_keyboard_input": toolbox.inject_keyboard_input,
        "send_file_to_telegram": toolbox.send_file_to_telegram,
    }
    try:
        for mt in toolbox.load_mcp_tools():
            high_priv_map[mt.name] = mt
    except Exception:
        pass

    if agent_mode == "EXECUTE":
        for _, tool_obj in high_priv_map.items():
            raw_tools.append(make_wrapped_tool(tool_obj, requires_confirmation=False))
    elif agent_mode == "INTERMEDIATE":
        for _, tool_obj in high_priv_map.items():
            raw_tools.append(make_wrapped_tool(tool_obj, requires_confirmation=True))
    else:  # PLAN (SAFE) mode
        for tname in getattr(agent_config, "enabled_tools", []):
            if tname == "visual_computer_operation":
                for ct in [
                    "take_screenshot", "click_at_current_location", "move_cursor_absolute",
                    "move_cursor_relative", "send_scroll", "inject_keyboard_input",
                ]:
                    raw_tools.append(make_wrapped_tool(high_priv_map[ct], requires_confirmation=True))
            elif tname in high_priv_map:
                raw_tools.append(make_wrapped_tool(high_priv_map[tname], requires_confirmation=True))

    final_tools = []
    allowed_names = set()
    for t_obj in raw_tools:
        if is_tool_disabled(t_obj.name):
            continue
        wrapped = t_obj if getattr(t_obj.func, "__name__", "") == "wrapped_func" else make_wrapped_tool(t_obj, requires_confirmation=False)
        final_tools.append(wrapped)
        allowed_names.add(t_obj.name)

    return final_tools, allowed_names


def _run_subagent_react_loop(llm, tools, allowed_names, system_prompt: str, task_description: str, thread_prefix: str, label: str) -> str:
    """Executes an isolated LangGraph ReAct subagent with abort safety, retry limits, and checkpoint cleanup."""
    class _RestrictedWrapper:
        def __init__(self, model, allowed):
            self.model = model
            self.allowed = allowed
        def bind_tools(self, t_list, **kw):
            return self.model.bind_tools([t for t in t_list if getattr(t, "name", t) in self.allowed], **kw)
        def __getattr__(self, name):
            return getattr(self.model, name)

    wrapped_llm = _RestrictedWrapper(llm, allowed_names)
    sub_agent = create_react_agent(wrapped_llm, tools, checkpointer=toolbox.shared_memory)
    thread_id = f"{thread_prefix}_{uuid.uuid4().hex[:8]}"
    used_thread_ids = [thread_id]
    run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 5000}

    max_retries = min(10, int(toolbox.load_global_settings().get("max_api_retries", 5) or 5))
    retry_delay = float(toolbox.load_global_settings().get("api_retry_delay", 10.0) or 10.0)
    final_text = ""

    try:
        for attempt in range(max_retries):
            toolbox.check_abort()
            try:
                state = sub_agent.get_state(run_config)
                if not state.values:
                    stream_input = {"messages": [SystemMessage(content=system_prompt), HumanMessage(content=task_description)]}
                else:
                    stream_input = None
                    log_tool(f"[dim yellow]Resuming {label} from checkpoint...[/dim yellow]")

                for event in sub_agent.stream(stream_input, config=run_config, stream_mode="updates"):
                    toolbox.check_abort()
                    for node_name, node_data in event.items():
                        messages = node_data.get("messages", [])
                        for msg in (messages if isinstance(messages, list) else [messages]):
                            if node_name == "agent" and msg.content:
                                raw_c = msg.content
                                text_c = raw_c if isinstance(raw_c, str) else "\n".join(
                                    b.get("text", "") for b in raw_c if isinstance(b, dict) and b.get("type") == "text"
                                )
                                if text_c.strip():
                                    final_text = text_c.strip()
                                    log_tool(f"[dim cyan]{label}:[/dim cyan] {escape(toolbox.mask_secrets(final_text))}")
                            elif hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    log_tool(f"[#808080]{label} Tool: {escape(str(tc.get('name', 'tool')))}[/#808080]")
                break
            except Exception as e:
                err_str = str(e)
                err_lower = err_str.lower()
                if "aborted" in err_lower or "interrupted" in err_lower or toolbox.ABORT_EVENT.is_set():
                    raise e
                # Fail fast on non-recoverable errors; reset once on state corruption
                if any(k in err_lower for k in ("401", "403", "404", "invalid_api_key", "not found")):
                    return f"Error in {label}: {err_str}"
                if any(k in err_lower for k in ("400", "tool_calls", "toolmessage")) and attempt == 0:
                    thread_id = f"{thread_prefix}_rst_{uuid.uuid4().hex[:6]}"
                    used_thread_ids.append(thread_id)
                    run_config["configurable"]["thread_id"] = thread_id
                    continue
                if attempt >= max_retries - 1:
                    return f"Error in {label} after {max_retries} attempts: {err_str}"
                log_tool(f"[bold red]{label} interrupted. Retrying in {int(retry_delay)}s... ({escape(err_str[:160])})[/bold red]")
                for _ in range(int(retry_delay * 10)):
                    toolbox.check_abort()
                    time.sleep(0.1)
    finally:
        try:
            cur = toolbox.shared_db_conn.cursor()
            for tid in used_thread_ids:
                cur.execute("DELETE FROM checkpoints WHERE thread_id = ?", (tid,))
                cur.execute("DELETE FROM writes WHERE thread_id = ?", (tid,))
            toolbox.shared_db_conn.commit()
        except Exception:
            pass

    return final_text or f"{label} completed without producing a text summary."

@tool
def dispatch_coding_subagent(task_description: str) -> str:
    """
    Dispatches an autonomous subagent to work on a coding task in an isolated, local git worktree.
    The subagent will create a new local branch, write/edit code, run tests, and commit its changes locally.
    Use this for multi-step coding tasks that you want to delegate without blocking the main workflow.
    """
    
    # 1. Resolve dispatching agent, permissions, and base directory
    agent_view, agent_config, base_dir = _resolve_dispatching_context()
    if not agent_view or not agent_config:
        return "Error: Could not resolve active agent context."
    if not agent_view.ensure_chatgpt_auth_for_agent(agent_config):
        return f"Error: ChatGPT OAuth authentication required for {agent_config.name}."

    # 2. Verify we are in an Offline Git Repository
    git_check = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], 
        cwd=base_dir, capture_output=True, text=True
    )
    if git_check.returncode != 0:
        return "Error: The target directory is not a Git repository. Subagent requires a local Git initialization."

    # Capture the starting commit so we can diff against it later
    base_commit_cmd = subprocess.run(
        ["git", "rev-parse", "HEAD"], 
        cwd=base_dir, capture_output=True, text=True
    )
    if base_commit_cmd.returncode != 0:
        return "Error: Git repository has no commits yet. Please make an initial commit first."
    base_commit = base_commit_cmd.stdout.strip()

    # 3. Setup safe local Branch and Worktree Path
    branch_name = f"feature/subagent-{uuid.uuid4().hex[:8]}"
    
    # Place worktrees inside a hidden folder in the base dir 
    # (This ensures the tools in toolbox.py still pass their security path constraints)
    wt_root = os.path.join(base_dir, ".federate_worktrees")
    os.makedirs(wt_root, exist_ok=True)
    
    # Exclude worktrees from the main git tracking natively
    excl_res = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=base_dir, capture_output=True, text=True)
    if excl_res.returncode == 0 and excl_res.stdout.strip():
        exclude_path = excl_res.stdout.strip()
        if not os.path.isabs(exclude_path):
            exclude_path = os.path.join(base_dir, exclude_path)
        if os.path.exists(exclude_path):
            with open(exclude_path, "r+", encoding="utf-8") as f:
                content = f.read()
                if ".federate_worktrees" not in content:
                    f.write("\n.federate_worktrees/\n")

    folder_name = branch_name.replace("/", "_")
    worktree_path = os.path.abspath(os.path.join(wt_root, folder_name))

    log_tool(f"Creating local git worktree at [cyan]{folder_name}[/cyan] on branch [cyan]{branch_name}[/cyan]...")
    wt_cmd = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, worktree_path], 
        cwd=base_dir, capture_output=True, text=True
    )
    
    if wt_cmd.returncode != 0:
        return f"Error creating worktree: {wt_cmd.stderr}"

    try:
        # 4. Build LLM and coding tools honoring the dispatching agent's current permissions & mode
        llm = _build_subagent_llm(agent_config)
        if not llm:
            return f"Error: API Key not configured for {agent_config.name}. Cannot launch subagent."

        coding_subset = {"read_file", "save_file", "edit_file", "list_files", "run_terminal_command"}
        sub_tools, allowed_names = _build_subagent_tools(agent_view, agent_config, allowed_subset=coding_subset)
        if not sub_tools:
            return f"Error: No permitted coding tools available for {agent_config.name} in {agent_view.agent_mode} mode."

        log_tool(f"[bold magenta]Coding Subagent Dispatched![/bold magenta] Working offline on branch: [green]{branch_name}[/green]")

        # 5. Execute Subagent
        system_prompt = f"""You are an autonomous coding subagent working in an isolated local git worktree.
Your goal is to accomplish the coding task provided and return a clear final summary.

CRITICAL DIRECTORY INSTRUCTIONS:
Your isolated worktree path is: {worktree_path}
You MUST use this absolute path for ALL file operations and terminal commands.
- Read, edit, or save files using: {worktree_path}/<filename>
- List files using: {worktree_path}
- Prepend 'cd "{worktree_path}" && ' to terminal commands.

FILE EDITING INSTRUCTION:
After calling edit_file, inspect the returned snippet to verify placement and syntax before continuing.

1. Break down the task and inspect relevant files.
2. Implement changes and run tests inside the worktree.
3. Return a concise final summary of changes and test outcomes."""

        summary = _run_subagent_react_loop(
            llm, sub_tools, allowed_names, system_prompt, task_description,
            thread_prefix=f"swe_{folder_name}", label="Coding Subagent"
        )

        log_tool("[bold green]Subagent finished task. Committing changes locally...[/bold green]")

        # 6. Commit Changes Locally
        subprocess.run(["git", "add", "."], cwd=worktree_path)
        subprocess.run(
            ["git", "-c", "user.name=FEDERaiDE", "-c", "user.email=harness@local",
             "commit", "-m", f"Subagent completed task:\n\n{task_description}"],
            cwd=worktree_path
        )

        # 7. Capture the Diff
        diff_cmd = subprocess.run(
            ["git", "diff", f"{base_commit}...HEAD"], 
            cwd=worktree_path, capture_output=True, text=True
        )
        diff_output = diff_cmd.stdout.strip()
        if not diff_output:
            diff_output = "No code modifications were made."

        # Return the summary to the MAIN AGENT
        final_report = (
            f"Subagent execution complete.\n"
            f"Local Branch Created: {branch_name}\n\n"
            f"Subagent Summary:\n{summary}\n\n"
            f"Diff of changes made by Subagent:\n"
            f"```diff\n"
            f"{diff_output}\n"
            f"```\n"
            f"Note: The branch is available locally. No remote push was performed."
        )
        try:
            import affect_engine
            if getattr(agent_config, "enable_emotions", True):
                ev = "subagent_error" if str(summary).startswith("Error in ") else "subagent_success"
                affect_engine.notify_telemetry_for_agent(agent_config.name, ev)
        except Exception:
            pass
        return final_report

    finally:
        # 8. Cleanup Worktree 
        # (This removes the physical worktree folder, but keeps the branch safely stored in the local repository)
        log_tool("Cleaning up local worktree folder...")
        subprocess.run(["git", "worktree", "remove", worktree_path, "--force"], cwd=base_dir)


@tool
def dispatch_subagent(task_description: str, context: str = "") -> str:
    """
    Dispatches an autonomous general-purpose subagent with an isolated context window to execute a multi-step task
    and return only the final synthesized results, preserving the main agent's context window.
    Inherits the dispatching agent's current tool permissions (PLAN / INTERMEDIATE / EXECUTE).
    """
    agent_view, agent_config, base_dir = _resolve_dispatching_context()
    if not agent_view or not agent_config:
        return "Error: Could not resolve active agent context."
    if not agent_view.ensure_chatgpt_auth_for_agent(agent_config):
        return f"Error: ChatGPT OAuth authentication required for {agent_config.name}."

    llm = _build_subagent_llm(agent_config)
    if not llm:
        return f"Error: API Key not configured for {agent_config.name}. Cannot launch subagent."

    sub_tools, allowed_names = _build_subagent_tools(agent_view, agent_config)
    if not sub_tools:
        return f"Error: No tools available for {agent_config.name} in {agent_view.agent_mode} mode."

    log_tool(f"[bold magenta]Subagent Dispatched ({agent_config.name})![/bold magenta] Mode: [cyan]{agent_view.agent_mode}[/cyan]")

    system_prompt = f"""You are an autonomous general-purpose subagent working on behalf of {agent_config.name}.
Workspace root directory: {base_dir}

RULES:
- Execute the delegated task thoroughly using your available tools.
- Do not fabricate file paths, tool outputs, or results.
- Return ONLY a dense, accurate, well-structured final report containing the findings, artifacts/paths created or modified, and outcomes so the parent agent does not need to re-run your steps."""

    full_task = task_description if not context.strip() else f"{task_description}\n\nAdditional Context:\n{context}"
    res = _run_subagent_react_loop(
        llm, sub_tools, allowed_names, system_prompt, full_task,
        thread_prefix=f"sub_{agent_config.name.replace(' ', '_')}", label="Subagent"
    )
    try:
        import affect_engine
        if getattr(agent_config, "enable_emotions", True):
            ev = "subagent_error" if str(res).startswith("Error in ") else "subagent_success"
            affect_engine.notify_telemetry_for_agent(agent_config.name, ev)
    except Exception:
        pass
    return res