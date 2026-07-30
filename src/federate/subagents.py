import os
import json
import subprocess
import uuid
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent
from langchain_core.tools import tool
import time
# Import the shared tools from toolbox.py 
# (dispatch_subagent is explicitly NOT imported by the subagent to prevent inception/looping)
from toolbox import (
    shared_memory,
    read_file, 
    save_file, 
    edit_file, 
    list_files, 
    run_terminal_command, 
    log_tool
)

@tool
def dispatch_subagent(task_description: str) -> str:
    """
    Dispatches an autonomous subagent to work on a coding task in an isolated, local git worktree.
    The subagent will create a new local branch, write/edit code, run tests, and commit its changes locally.
    Use this for multi-step coding tasks that you want to delegate without blocking the main workflow.
    """
    
    # 1. Determine base directory (where the root .git should be)
    try:
        from toolbox import CURRENT_APP
        if CURRENT_APP:
            base_dir = os.path.abspath(str(CURRENT_APP.query_one("#dir_tree").path))
        else:
            base_dir = os.path.abspath(os.getcwd())
    except ImportError:
        base_dir = os.path.abspath(os.getcwd())

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
    exclude_path = os.path.join(base_dir, ".git", "info", "exclude")
    if os.path.exists(exclude_path):
        with open(exclude_path, "r+") as f:
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
        # 4. Load AI Configurations dynamically from the Active Agent in UI
        # Default fallback (Switched from stepfun to gemini)
        api_key, base_url, model = "", "https://openrouter.ai/api/v1", "google/gemini-2.0-flash:free"
        
        try:
            from toolbox import CURRENT_APP
            if CURRENT_APP:
                agent_view = CURRENT_APP.query_one("AIAgentView")
                # Pull from the currently active agent persona
                agent = agent_view.active_agent
                api_key = agent.get_api_key()
                base_url = agent.base_url
                model = agent.model
        except Exception as e:
            log_tool(f"[dim red]Subagent config fetch error: {e}[/dim red]")

        if not api_key:
            return "Error: API Key not configured for the active agent. Cannot launch subagent."

        llm = ChatOpenAI(model=model, temperature=0, api_key=api_key, base_url=base_url, model_kwargs={"reasoning_effort": "high"})
        
        # Inject the generic tools from toolbox.py
        sub_tools =[read_file, save_file, edit_file, list_files, run_terminal_command]
        sub_agent = create_react_agent(llm, sub_tools, checkpointer=shared_memory)
        # --- NEW: Create a unique Thread ID for this branch ---
        thread_id = f"swe_{branch_name.replace('/', '_')}"
        run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 5000}
        
        
        log_tool(f"[bold magenta]Subagent Dispatched![/bold magenta] Working offline on branch: [green]{branch_name}[/green]")

        # 5. Execute Subagent
        # We enforce strict workspace isolation via the system prompt
        system_prompt = SystemMessage(content=f"""
        You are an autonomous subagent. You are working in a strictly isolated local git worktree.
        Your goal is to accomplish the coding task provided.
        
        CRITICAL DIRECTORY INSTRUCTIONS:
        Your isolated worktree path is: {worktree_path}
        You MUST use this absolute path for ALL file operations and terminal commands. 
        - If you read, edit, or save a file, use the absolute path: {worktree_path}/<filename>
        - If you list files, point the tool to: {worktree_path}
        - If you run terminal commands (like tests or scripts), ALWAYS prepend 'cd {worktree_path} && ' to your command to ensure it runs inside your isolated worktree.
        
        FILE EDITING INSTRUCTION:
        After using the edit file tool, check the response to see the concerned section of the edited file, including your edit. Ensure the edit was placed as you intended. Check to see if the edit is syntactically correct. If you notice any issues, immediately use the edit tool again to fix it. Continue this loop until the edit you intended (and ONLY the edit you intended) has been correctly placed.
        
        1. Break down the task.
        2. Read, save, or edit code inside your worktree.
        3. Use the terminal tool to run tests and verify your changes.
        4. Iterate until you are satisfied that the code is correct and tested.
        5. When satisfied, formulate a final summary of changes and stop iterating.
        """)
        
        chat_history = [system_prompt, HumanMessage(content=task_description)]
        
        # --- NEW: Resume Loop ---
        for attempt in range(1000):
            try:
                state = sub_agent.get_state(run_config)
                
                if not state.values:
                    # Start fresh
                    chat_history =[system_prompt, HumanMessage(content=task_description)]
                    stream_input = {"messages": chat_history}
                else:
                    # Resume from DB
                    stream_input = None
                    log_tool(f"[dim yellow] Resuming SWE Subagent from checkpoint...[/dim yellow]")
                
                for event in sub_agent.stream(stream_input, config=run_config, stream_mode="updates"):
                    for node_name, node_data in event.items():
                        messages = node_data.get("messages", [])
                        for msg in (messages if isinstance(messages, list) else[messages]):
                            if node_name == "agent" and msg.content:
                                log_tool(f"[dim cyan]Subagent:[/dim cyan] {msg.content}")
                            elif hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    log_tool(f"[#808080]Subagent Tool: {tc['name']}[/#808080]")
                
                break # Successfully completed the stream without network crashing
                
            except Exception as e:
                if "aborted" in str(e).lower() or "interrupted" in str(e).lower():
                    raise e
                log_tool(f"[bold red]SWE Subagent network interrupted. Retrying in 15s... ({e})[/bold red]")
                time.sleep(15)

        log_tool("[bold green]Subagent finished task. Committing changes locally...[/bold green]")

        # 6. Commit Changes Locally
        subprocess.run(["git", "add", "."], cwd=worktree_path)
        subprocess.run(["git", "commit", "-m", f"Subagent completed task:\n\n{task_description}"], cwd=worktree_path)

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
            f"Diff of changes made by Subagent:\n"
            f"```diff\n"
            f"{diff_output}\n"
            f"```\n"
            f"Note: The branch is available locally. No remote push was performed."
        )
        return final_report

    finally:
        # 8. Cleanup Worktree 
        # (This removes the physical worktree folder, but keeps the branch safely stored in the local repository)
        log_tool("Cleaning up local worktree folder...")
        subprocess.run(["git", "worktree", "remove", worktree_path, "--force"], cwd=base_dir)