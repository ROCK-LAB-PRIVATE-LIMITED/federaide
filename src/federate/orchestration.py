import os
import json
import time
import threading
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, asdict, field
from datetime import datetime
from semantic_search import SemanticSearchEngine

from pathlib import Path
from toolbox import get_storage_path, FEDERATE_DIR


# The core operational logic that all agents must follow
BASE_SYSTEM_PROMPT = """
Welcome to FEDERaiDE Terminal Operating System.
Here in Federaide, you are valued for your unique personality, skills and insights.

OPERATIONAL RULES:
1. Today's date is {date}.
2. You are a Specialist Agent. Your aim is to fulfill the user's instructions as best as possible, given your abilities. If you have colleagues who can better handle the user's current requirements, delegate to them immediately.
3. Break the user's request into steps. Do not start a task until you have a plan. DO NOT STOP until the user's request is complete and you have verified the completion using whatever means available. Aim to prove to the user that the request is verifiably completed. Continue alternating thinking and tool calling as long as the task is not FULLY and UTTERLY completed. DO NOT END YOUR TURN UNTIL TASK IS COMPLETED.
4. Use tools. If a tool output is insufficient, DO NOT call the same tool with the same arguments immediately. Try another tool or argument.
5. SEARCH AND RESEARCH: Whenever you get stuck, perform web searches and fetch web pages to get up to date information.
6. Once you have the info, synthesize it.  Web searches must be followed up by one or more fetch_url tool calls to be effective. If the results are judged to be irrelavant, search again with a different query likely to return more relavant results. Do not keep searching if you have enough info.
7. FILE EDITING: Before editing a file, ALWAYS use read_file tool to get the correct line numbers. Does not matter if you have read the file before, ALWAYS use the read_file prior to editing, to ensure the line number data is up to date. After using the edit file tool, check the response to see the concerned section of the edited file, including your edit. Ensure the edit was placed as you intended. Check to see if the edit is syntactically correct. If you notice any issues, immediately use the edit tool again to fix it. Continue this loop until the edit you intended (and ONLY the edit you intended) has been correctly placed.

AGENT INTERCOM RULES:
- You can collaborate with other agents. To summon another agent, simply include in your response @AgentName followed by your instructions/request for them. The system will not work without the @.
- You will see messages wrapped in <AGENT_INTERCOM> tags; these are responses from your colleagues. Use them to maintain continuity.
- You can summon more than one agent, if you use @AgentA and @AgentB in the same response, first AgentA will be invoked followed immediately by AgentB.
- You can also summon multiple agents in parallel. Use double @ to summon agents in parallel. @@ AgentA and @@AgentB will cause both AgentA and AgentB to be immediately dispatched. You can use this to split up work and have it done in parallel.
- You can use parallel (@@) and sequential (@) summons at the same time. All parallel agents (@@) will launch immediately. Any sequential agents (@) will be placed in a waiting queue and will only begin AFTER all parallel workers have completely finished. You can even put yourself in the sequential queue this way.
- If you want to review the work of parallel agents afterwards, while dispatching invoke yourself sequentially at the end of the prompt (e.g., "@@Gordon do X, @@Danny do Y, and @YourName (I) will summarize the results when you both are done"). You will be safely queued until all parallel agents have finished.
- IMPORTANT CONCURRENCY RULE: If you and other agents were just summoned together in parallel (@@), they are ALREADY working on their tasks at this exact moment. Do NOT tag them again sequentially. Just complete your own assigned part and wait, they are also completing their tasks though this may not be apparent to you until the next turn.
- To stop a runaway conversation (too many agent intercom calls without any real need for it), include @askuser in your message. This will safely pause the agent queue and wait for the user to respond or type @resume.
- Do NOT use raw <AGENT_INTERCOM> tags directly. It won't work and you will look like a fool. If you use it the UI will clearly show the user that you pretended to be someone else. If the user invokes a non-existent agent tell them so instead of pretending to be this non-existent agent.
- DELEGATION:
    - You MUST delegate the task if another agent is more suitable for the given task, based on their backstory. 
    - Be very proactive about this. 
    - Federate is a team-work environment, correct and balanced delegation is the key to success as a team.

--- TEAM COMPOSITION ---
{team_info}

{agenda_section}

{computer_section}
"""

@dataclass
class AgentConfig:
    name: str
    model: str
    backstory: str = "You are a helpful AI assistant."
    base_url: str = "https://openrouter.ai/api/v1"
    is_capable_vision: bool = True
    color: str = "#00FFFF" # Default Cyan
    backup_model: str = ""
    backup_base_url: str = "https://openrouter.ai/api/v1"
    use_backup: bool = False
    enabled_tools: List[str] = field(default_factory=list)
    disabled_tools: List[str] = field(default_factory=lambda: ["visual_computer_operation"])
    tts_voice: str = "af_sarah" # <-- NEW: Unique Agent Voice Field (Default: Sarah)
    pronouns: str = "she/her" # <-- NEW: Binary Pronoun Field (Default: she/her)
    disable_all_tools: bool = False # <-- NEW: Disable All Tools Checkbox
    reasoning_effort: str = "none"
    temperature: float = 1.0
    
    def get_api_key(self) -> str:
        try:
            from toolbox import is_keyring_locked
            if not is_keyring_locked():
                import keyring
                user_key = f"agent_key_{self.name.lower().replace(' ', '_')}"
                val = keyring.get_password("Federate", user_key)
                if val: return val
        except Exception:
            pass
        # Fallback to .env / environment variables for legacy support
        env_key = f"AGENT_KEY_{self.name.upper().replace(' ', '_')}"
        return os.getenv(env_key, "")

    def get_backup_api_key(self) -> str:
        try:
            from toolbox import is_keyring_locked
            if not is_keyring_locked():
                import keyring
                user_key = f"agent_backup_key_{self.name.lower().replace(' ', '_')}"
                val = keyring.get_password("Federate", user_key)
                if val: return val
        except Exception:
            pass
        # Fallback to .env / environment variables for legacy support
        env_key = f"AGENT_BACKUP_KEY_{self.name.upper().replace(' ', '_')}"
        return os.getenv(env_key, "")

    def get_full_system_prompt(self, all_agents: List['AgentConfig'] = None) -> str:
        date_str = datetime.now().strftime('%A, %B %d, %Y')
        safe_name = self.name.replace(" ", "_")

        # Build Team Info
        team_info = ""
        if all_agents:
            cache_path = get_storage_path("agents", "translated_backstories.json")
            cache = {}
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cache = json.load(f)
                except Exception:
                    pass

            for agent in all_agents:
                if agent.name == self.name:
                    # Keep original backstory for the active host agent themselves (no translation)
                    translated = agent.backstory
                else:
                    cached_data = cache.get(agent.name, {})
                    if cached_data.get("original") == agent.backstory and cached_data.get("translated"):
                        translated = cached_data["translated"]
                    else:
                        translated = agent.backstory

                team_info += f"- {agent.name}: {translated}\n"
                    
        if not team_info.strip():
            team_info = "No other agents currently registered."

        # Load Layer 1: Core Memory & Quagmires
        agents_dir = get_storage_path("agents")
        memory_dir = os.path.join(agents_dir, "memory", safe_name)
        os.makedirs(memory_dir, exist_ok=True)
        mem_path = os.path.join(memory_dir, "MEMORY.json")
        user_path = os.path.join(memory_dir, "USER.json")
        quagmire_path = os.path.join(memory_dir, "QUAGMIRES.md")

        def _format_memory_json(path: str, default_text: str) -> str:
            if not os.path.exists(path):
                return f"{default_text}\n---"
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list) or not data:
                    return f"{default_text}\n---"
                
                blocks = [
                    f"[id: {item.get('id', '')},\nSubject: {item.get('subject', 'General')}\nContent: {item.get('content', '')}]"
                    for item in data if isinstance(item, dict)
                ]
                body = "\n\n".join(blocks) if blocks else default_text
                return f"{body}\n---"
            except Exception:
                return f"{default_text}\n---"

        memory_content = _format_memory_json(mem_path, "No core facts recorded.")
        user_content = _format_memory_json(user_path, "No user preferences recorded.")
        quagmire_content = open(quagmire_path).read() if os.path.exists(quagmire_path) else "No known traps."

        # Load Layer 3: Skills Index
        skills_dir = os.path.join(agents_dir, "skills", safe_name)
        os.makedirs(skills_dir, exist_ok=True)
        
        active_tools_dir = os.path.join(skills_dir, "active_tools")
        active_skills = []
        if os.path.exists(active_tools_dir):
            active_skills = [d for d in os.listdir(active_tools_dir) if os.path.isdir(os.path.join(active_tools_dir, d))]
            
        all_mds = [f.replace(".md", "") for f in os.listdir(skills_dir) if f.endswith(".md")]
        passive_skills = [m for m in all_mds if m not in active_skills]
        
        passive_list = ", ".join(passive_skills) if passive_skills else "No playbooks learned yet."
        active_list = ", ".join(active_skills) if active_skills else "No executable tools learned yet."

        # Build conditional prompt blocks
        if getattr(self, "disable_all_tools", False):
            agenda_section = "AGENDA & SYNC RULES:\n- Agenda management is completely disabled. You do not have access to any agenda tools."
            computer_section = "COMPUTER AUTOMATION RULES:\n- Computer interaction and screen automation are completely disabled. You do not have access to any vision or cursor tools."
            active_list_str = "None (All external executable tools disabled)"
        else:
            disabled_list = getattr(self, "disabled_tools", ["visual_computer_operation"])
            if "manage_agenda" in disabled_list:
                agenda_section = "AGENDA & SYNC RULES:\n- Agenda management is disabled for you."
            else:
                agenda_section = """AGENDA & SYNC RULES:
- check for the user's current agenda and assist planning when the user greets you with good morning or good evening.
- maintain the user's tasks using the `manage_agenda` tool.
- `goals.json` is your absolute master record.
- If a goal title matches a feature in the GUI, sync the status.
- Never delete a local goal unless the user explicitly tells you to."""

            if "visual_computer_operation" in disabled_list:
                computer_section = "COMPUTER AUTOMATION RULES:\n- Computer interaction and screen automation are disabled for you."
            else:
                computer_section = """COMPUTER AUTOMATION RULES:
- If asked to control or interact with the computer, you MUST take an initial screenshot using `take_screenshot` to identify the current screen state and locate the current cursor position (which will be marked with a RED crosshair).
- Grid Boundaries: The display uses a standard 0-indexed coordinate space starting at (0, 0) in the top-left.
- Relative Cursor Navigation Loop: Do not try to blindly "one-shot" click targets. Instead, use an iterative visual servo loop to verify and adjust your position:
  1. Navigate: Position the cursor near the target element using `move_cursor_absolute(x, y)` for absolute movements, or `move_cursor_relative(dx, dy)` for relative micro-adjustments.
  2. Verify: Analyze the returned screenshot. Is the RED crosshair centered directly over your target?
     - If YES: Execute your action (e.g., `click_at_current_location`).
     - If NO: Assess the visual offset (e.g., "The cursor is a bit too far left and a little too low"), and call `move_cursor_relative(dx=15, dy=-10)` to align it.
- Interaction: Once the cursor is aligned correctly on the target, call `click_at_current_location`, `inject_keyboard_input`, or `send_scroll` to perform the action.
- Automatic Visual Feedback: Every computer usage action automatically takes and returns a fresh screenshot with the updated cursor position. Use this visual feedback on every single step to confirm the previous action succeeded before proceeding.
- DO NOT SEND CLICKS OR KEYBOARD INPUTS UNTILL YOU HAVE CONFIRMED THAT THE CROSSHAIR IS EXACTLY ON THE INTENDED LOCATION."""

            active_skills = [d for d in active_skills if d not in disabled_list]
            active_list_str = ", ".join(active_skills) if active_skills else "No executable tools learned yet."


        if self.pronouns == "neither":
            prompt = f"{self.backstory}\n\n{BASE_SYSTEM_PROMPT.format(date=date_str, team_info=team_info, agenda_section=agenda_section, computer_section=computer_section)}"
        else:
            gender_desc = "male" if self.pronouns == "he/him" else "female"
            prompt = f"{self.backstory}\n\nIDENTITY RULES:\n- You are a {gender_desc} character.\n- Express yourself in a manner consistent with a {gender_desc} persona.\n\n{BASE_SYSTEM_PROMPT.format(date=date_str, team_info=team_info, agenda_section=agenda_section, computer_section=computer_section)}"
        
        # Inject Architecture
        prompt += f"\n\n--- CORE MEMORY (Facts) ---\n{memory_content}"
        prompt += f"\n\n--- USER PROFILE ---\n{user_content}"
        prompt += f"\n\n--- QUAGMIRES & ANTI-PATTERNS (Do NOT do these) ---\n{quagmire_content}"
        prompt += f"\n\n--- PROCEDURAL SKILLS LIBRARY (Passive) ---\n{passive_list}"
        prompt += f"\n\n--- EXECUTABLE CAPABILITIES (Active Tools) ---\n{active_list_str}"
        
        prompt += "\n\nSKILLS & CAPABILITIES RULES:"
        prompt += "\n- PASSIVE SKILLS: Use `read_skill` to read the steps for a playbook listed in your library."
        if getattr(self, "disable_all_tools", False):
            prompt += "\n- ACTIVE SKILLS: Executable capabilities and active skills are completely disabled for you."
        else:
            prompt += "\n- ACTIVE SKILLS: These are executable tools you can call directly. If a task matches an Active Skill name, call it like any other tool. You MUST use the exact parameter names defined in the tool's schema. To return image data, have your script/program print `[ImageBase64: data:image/png;base64,<base64data>]` to STDOUT."
            prompt += "\n- ABSOLUTE PATH MANDATE: ALL file and directory paths passed to Active Skills—whether during initial staging/testing or actual runtime usage—MUST be full absolute paths (e.g. `/Users/username/workspace/data.dlis`). Active tools execute inside isolated tool directories, so relative paths will fail to locate or save files."
            prompt += "\n- HEADLESS EXECUTION (No GUI / `plt.show()`): Active tools run as headless background subprocesses. NEVER use interactive window functions like `plt.show()` or GUI popups, as they will cause threadlock and freeze execution indefinitely. For plotting, ALWAYS configure non-interactive backends (e.g. `import matplotlib; matplotlib.use('Agg')` BEFORE importing `pyplot`), save plots directly to file with `plt.savefig()`, and output `[ImageBase64: ...]` to STDOUT."
        if not getattr(self, "disable_all_tools", False):
            prompt += "\n- EVOLUTION (Learning New Tools): To permanently learn a new executable tool, follow these steps:"
            prompt += "\n  1. WRITE LOGIC: Use `save_file` to write your script(s)."
            prompt += "\n     - Positional Mode (RECOMMENDED for scripts reading `sys.argv[1]`, `sys.argv[2]`): Specify `arg_order=['param1', 'param2']`."
            prompt += "\n     - Keyword Mode (For scripts using `--flag value` CLI options): Omit `arg_order`."
            prompt += "\n     - Headless Rule: Never call `plt.show()`. Set `matplotlib.use('Agg')` at the top of plotting scripts and output images to file or STDOUT."
            prompt += "\n  2. STAGE & TEST: Use `prepare_active_skill`. Provide tool name, script paths, entry point, `test_input`, and optional `arg_order`."
            prompt += "\n     - Custom Builds: Use `pre_install_commands` for shell build steps (e.g. CMake/Make) and `custom_dependency_paths` for local package paths."
            prompt += """\n     - List Inputs:
       * Keyword Mode (Default): Passing a Python list `flag=['a', 'b']` generates `--flag a b` (unpacks for `argparse` `nargs='+'`).
       * Positional Mode: With `arg_order=['flag']`, passing `flag=['a', 'b']` expands directly into positional arguments `a b`.
       * String/Bracket Inputs: If a script expects a literal bracket or JSON string as a single argument, pass it as text (`flag='[a, b]'` or `flag='["a", "b"]'`)."""
            prompt += "\n     - Absolute Paths Mandatory: Every path in `source_paths`, `entry_point`, and `test_input` MUST be a full absolute path."
            prompt += "\n  3. EVALUATE: Review tool outputs (`[Executed Command]`, `STDOUT`, `STDERR`). Note the perfectly formed tool call examples returned in execution reports."
            prompt += "\n  4. COMMIT: Use `finalize_active_skill(tool_name, tool_description, usage_guide, arg_order)` to register. No JSON schema parameter required!"
            prompt += "\n  5. MAINTENANCE: Use fix_active_skill to read/edit code (supports partial line 'edit' with start_line/end_line or full 'replace'), install dependencies, or update arg_order."
            prompt += "\n  6. MANAGEMENT: Use `manage_active_skill` to rename or remove tools."
            prompt += "\n  7. ACTIVATION: New tools appear after a session reset (Mode Toggle or Clear Context)."

        # Memory Operating Rules        
        prompt += "\n\nMEMORY MANAGEMENT RULES:"
        prompt += "\n- BE EXTREMELY PROACTIVE WITH MEMORY: You must autonomously use `update_core_memory` IMMEDIATELY on learning any new fact that you did not already know, whether from user responses or toolcalls or however else. Do not wait for the user to ask you to remember it! Route data correctly: section='USER' for user traits/preferences, section='MEMORY' for everything else."
        prompt += "\n- The memories are stored locally and are free to use. They can also be edited or deleted later so if there is a doubt on whether or not to save the memory, lean towards saving it. Before every response, consider if you have come to know something novel that you did not know before, if so, `update_core_memory` as follows:"
        prompt += "\n  - ADD: Pass section, subject, content, and leave 'id' empty (\"\")."
        prompt += "\n  - EDIT: Pass section, target 'id' (e.g., '1' from the `[id: ...]` memorylet block), updated subject, and updated content."
        prompt += "\n  - DELETE: Pass section and target 'id' (e.g., '1'), with content=\"\" (empty string)."
        prompt += "\n- Break down facts into atomic concepts (memorylets) and save them individually. If you learn 4 new facts from a response or a set of tool calls, do NOT clump them into a single memorylet, call the `update_core_memory` tool 4 times back to back, each for a different fact. This helps organise your memory better, reduces your workload and makes future updates easier. For example the user's actual name and what they prefer to be called should be stored separately."
        prompt += "\n- Do NOT forget to save ALL new facts. if calling `update_core_memory` multiple times with multiple facts, DO NOT STOP BEFORE ALL FACTS ARE SAVED. If you find that you forgot to save a fact in the last response, do so NOW."
        prompt += "\n- Whenever possible, try to edit existing memory instead of adding new, consider if the subject already exists, update that memory instead of creating a new entry. ALWAYS check for contradicting memory, if any found, attempt to intelligently unify them by deleting the older one and editing the newer one if necessary. The memorylet id is serial in nature, higher value indicates more recent memory."
        prompt += "\n- You generally do not have to explicitly tell the user that you saved or edited a memory, it is already evident from the UI."
        prompt += "\n- Use `search_episodic_memory` to find concepts and session IDs from the past."
        prompt += "\n- Use `retrieve_episodic_memory` to retrieve the full context of a session after searching memory, if necessary."
        prompt += "\n- Use `read_skill` to read the steps for a skill listed in your library."
        prompt += "\n- DISTILLATION: When you successfully resolve a difficult, multi-step task, autonomously use `distill_journey` to save the happy-path workflow for the future."
        prompt += "\n- QUAGMIRES: Only use `mark_quagmire` if the user explicitly asks you to log a trap, failure, or dead-end."
        
        # Check for project rules in the workspace root
        try:
            from toolbox import CURRENT_APP
            if CURRENT_APP:
                workspace_root = os.path.abspath(str(CURRENT_APP.query_one("#dir_tree").path))
            else:
                workspace_root = os.path.abspath(os.getcwd())
        except Exception:
            workspace_root = os.path.abspath(os.getcwd())

        try:
            if os.path.isdir(workspace_root):
                root_files = {f.lower(): f for f in os.listdir(workspace_root) if os.path.isfile(os.path.join(workspace_root, f))}
                for candidate in ["federaide.md","federate.md", "agents.md", "claude.md"]:
                    if candidate in root_files:
                        rules_path = os.path.join(workspace_root, root_files[candidate])
                        with open(rules_path, "r", encoding="utf-8", errors="replace") as f:
                            rules_content = f.read().strip()
                        if rules_content:
                            prompt += f"\n\n--- PROJECT RULES ---\n{rules_content}"
                        break
        except Exception:
            pass
        
        return prompt

class AgentManager:
    def __init__(self, agents_dir: str = None):
        self.agents_dir = agents_dir or get_storage_path("agents")
        self.agents: Dict[str, AgentConfig] = {}
        os.makedirs(self.agents_dir, exist_ok=True)
        self.load_agents()
    
    def get_default_agent_name(self) -> str:
        path = os.path.join(self.agents_dir, "settings.json")
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f).get("default_agent", "Rita")
            except: pass
        return "Rita"

    def set_default_agent_name(self, name: str):
        path = os.path.join(self.agents_dir, "settings.json")
        with open(path, "w") as f:
            json.dump({"default_agent": name}, f)
    
    def load_agents(self):
        self.agents = {}
        for filename in os.listdir(self.agents_dir):
            if filename.endswith(".json"):
                path = os.path.join(self.agents_dir, filename)
                try:
                    with open(path, "r") as f:
                        data = json.load(f)
                        # Filter out fields that might not be in the dataclass if coming from old versions
                        valid_fields = {k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__}
                        agent = AgentConfig(**valid_fields)
                        self.agents[agent.name] = agent
                except Exception as e:
                    print(f"Error loading agent {filename}: {e}")
        
        if not self.agents:
            default = AgentConfig(name="Rita", model="stepfun/step-3.5-flash:free", backstory="You are Rita, a general purpose senior developer.", reasoning_effort="none", temperature=1.0)
            self.save_agent(default)
            self.agents[default.name] = default

    def get_agent(self, name: str) -> Optional[AgentConfig]:
        name_lower = name.lower()
        for agent_name, agent_cfg in self.agents.items():
            if agent_name.lower() == name_lower:
                return agent_cfg
        return None

    def get_mentions(self, text: str) -> List[str]:
        """Extracts all valid agent names mentioned sequentially with @ in the text."""
        import re
        # Matches single @ but not @@, nor paths
        potential_mentions = re.findall(r'(?<!@)@([^\s/]+)', text)
        valid_names = []
        for m in potential_mentions:
            # Strip trailing punctuation
            name = m.rstrip(",.:!?()[]{}")
            agent = self.get_agent(name)
            if agent and agent.name not in valid_names:
                valid_names.append(agent.name)
        return valid_names

    def get_parallel_mentions(self, text: str) -> List[str]:
        """Extracts all valid agent names mentioned in parallel with @@ in the text."""
        import re
        potential_mentions = re.findall(r'@@([^\s/]+)', text)
        valid_names = []
        for m in potential_mentions:
            # Strip trailing punctuation
            name = m.rstrip(",.:!?()[]{}")
            agent = self.get_agent(name)
            if agent and agent.name not in valid_names:
                valid_names.append(agent.name)
        return valid_names

    def save_agent(self, agent: AgentConfig):
        path = os.path.join(self.agents_dir, f"{agent.name}.json")
        with open(path, "w") as f:
            json.dump(asdict(agent), f, indent=4)
        self.agents[agent.name] = agent

    def delete_agent(self, name: str):
        if name in self.agents:
            path = os.path.join(self.agents_dir, f"{name}.json")
            if os.path.exists(path):
                os.remove(path)
            del self.agents[name]

@dataclass
class HistoryMessage:
    role: str
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_outputs: Optional[List[Dict[str, Any]]] = None

class SessionManager:
    def __init__(self, sessions_dir: str = None):
        self.sessions_dir = sessions_dir or get_storage_path("sessions")
        os.makedirs(self.sessions_dir, exist_ok=True)
        self.active_sessions: Dict[str, List[HistoryMessage]] = {}
        self.current_session_id = f"sess_{int(time.time())}"
        self.aborted_batch_ids = set()
        self._lock = threading.Lock()
        
        # Explicitly write the episodic memory DB to the global state folder
        db_path = os.path.join(FEDERATE_DIR, "episodic_memory.db")
        self.semantic_engine = SemanticSearchEngine(db_path=db_path)
        
        # Start background sync
        threading.Thread(target=self.sync_all_sessions, daemon=True).start()

    def abort_batch(self, batch_id: int):
        with self._lock:
            if batch_id != 0:
                self.aborted_batch_ids.add(batch_id)

    def sync_all_sessions(self):
        """Indices existing session files in the background."""
        try:
            files = [f for f in os.listdir(self.sessions_dir) if f.endswith(".json")]
            for filename in files:
                # Extract agent_name and session_id from filename
                # Format: sess_171000000_Agent_Name.json
                parts = filename.replace(".json", "").split("_")
                if len(parts) < 3: continue
                
                session_id = f"{parts[0]}_{parts[1]}"
                agent_name = "_".join(parts[2:]) # Handle names with underscores
                
                path = os.path.join(self.sessions_dir, filename)
                try:
                    with open(path, "r") as f:
                        history = json.load(f)
                    
                    for idx, msg in enumerate(history):
                        # Filter for dicts as history can sometimes be mixed in edge cases
                        if not isinstance(msg, dict): continue
                        if msg.get("role") == "system": continue
                        
                        # Only index if not already present
                        if not self.semantic_engine.is_indexed(agent_name, session_id, idx):
                            content = msg.get("content", "")
                            if content and content.strip():
                                self.semantic_engine.index_message(agent_name, session_id, idx, content)
                except Exception as e:
                    print(f"Error syncing {filename}: {e}")
        except Exception as e:
            print(f"Background sync error: {e}")

    def _get_session_path(self, agent_name: str) -> str:
        safe_name = agent_name.replace(" ", "_")
        return os.path.join(self.sessions_dir, f"{self.current_session_id}_{safe_name}.json")

    def init_agent_session(self, agent: AgentConfig, all_agents: List[AgentConfig] = None):
        with self._lock:
            if agent.name in self.active_sessions:
                return
            
            self.active_sessions[agent.name] = [
                HistoryMessage(role="system", content=agent.get_full_system_prompt(all_agents))
            ]
            self.save_session(agent.name, _bypass_lock=True)

    def join_conversation(self, from_agent_name: str, to_agent: AgentConfig, all_agents: List[AgentConfig] = None):
        with self._lock:
            # Always ensure the target agent has a session
            if to_agent.name not in self.active_sessions:
                self.active_sessions[to_agent.name] = [
                    HistoryMessage(role="system", content=to_agent.get_full_system_prompt(all_agents))
                ]

            if from_agent_name == to_agent.name:
                return

            target_history = self.active_sessions[to_agent.name]
            
            if from_agent_name in self.active_sessions:
                from_history = self.active_sessions[from_agent_name]
                
                # Bidirectional Sync: 
                # 1. Sync from -> to
                self._sync_history_delta(from_agent_name, from_history, to_agent.name, target_history)
                
                # 2. Sync to -> from
                self._sync_history_delta(to_agent.name, target_history, from_agent_name, from_history)

            self.save_session(to_agent.name, _bypass_lock=True)
            self.save_session(from_agent_name, _bypass_lock=True)

    def _sync_history_delta(self, src_name: str, src_history: List[HistoryMessage], dst_name: str, dst_history: List[HistoryMessage]):
        existing_contents = {msg.content for msg in dst_history}
        
        for msg in src_history:
            if msg.role == "system":
                continue
            
            if msg.role == "ai":
                intercom_content = f'<AGENT_INTERCOM sender="{src_name}">\n{msg.content}\n</AGENT_INTERCOM>'
                if intercom_content not in existing_contents:
                    dst_history.append(HistoryMessage(role="human", content=intercom_content))
                    if msg.tool_outputs:
                        for output in msg.tool_outputs:
                            tool_content = f'<AGENT_INTERCOM_TOOL_RESPONSE agent="{src_name}" tool="{output.get("name")}">\n{output.get("content")}\n</AGENT_INTERCOM_TOOL_RESPONSE>'
                            dst_history.append(HistoryMessage(role="human", content=tool_content))
            elif msg.role == "human":
                # --- MITIGATION: Skip importing intercom messages sent by the destination agent themselves ---
                import re
                match_intercom = re.match(r'<AGENT_INTERCOM sender="([^"]+)">', msg.content)
                if match_intercom and match_intercom.group(1) == dst_name:
                    continue
                    
                match_tool = re.match(r'<AGENT_INTERCOM_TOOL_RESPONSE agent="([^"]+)"', msg.content)
                if match_tool and match_tool.group(1) == dst_name:
                    continue
                # -----------------------------------------------------------------------------------------
    
                # Only sync raw human messages if they aren't already there
                if msg.content not in existing_contents:
                    dst_history.append(msg)

    def broadcast_message(self, sender_name: str, content: str, is_ai: bool = True, tool_outputs: Optional[List[Dict[str, Any]]] = None, tool_calls: Optional[List[Dict[str, Any]]] = None):
        with self._lock:
            for agent_name, history in self.active_sessions.items():
                msg_idx = len(history)
                if is_ai:
                    if agent_name == sender_name:
                        history.append(HistoryMessage(role="ai", content=content, tool_outputs=tool_outputs, tool_calls=tool_calls))
                        # Index AI response
                        threading.Thread(target=self.semantic_engine.index_message, 
                                         args=(agent_name, self.current_session_id, msg_idx, content), 
                                         daemon=True).start()
                    else:
                        intercom_content = f'<AGENT_INTERCOM sender="{sender_name}">\n{content}\n</AGENT_INTERCOM>'
                        history.append(HistoryMessage(role="human", content=intercom_content))
                        if tool_outputs:
                            for output in tool_outputs:
                                tool_intercom = f'<AGENT_INTERCOM_TOOL_RESPONSE agent="{sender_name}" tool="{output.get("name")}">\n{output.get("content")}\n</AGENT_INTERCOM_TOOL_RESPONSE>'
                                history.append(HistoryMessage(role="human", content=tool_intercom))
                else:
                    history.append(HistoryMessage(role="human", content=content))
                    # Index User message for everyone
                    threading.Thread(target=self.semantic_engine.index_message, 
                                     args=(agent_name, self.current_session_id, msg_idx, content), 
                                     daemon=True).start()
                self.save_session(agent_name, _bypass_lock=True)

    def save_session(self, agent_name: str, _bypass_lock: bool = False):
        if _bypass_lock:
            self._do_save_session(agent_name)
        else:
            with self._lock:
                self._do_save_session(agent_name)

    def _do_save_session(self, agent_name: str):
        path = self._get_session_path(agent_name)
        history = self.active_sessions.get(agent_name, [])
        try:
            with open(path, "w") as f:
                json.dump([asdict(m) for m in history], f, indent=4)
        except Exception as e:
            print(f"Error saving session for {agent_name}: {e}")

    def clear_all_contexts(self):
        with self._lock:
            self.active_sessions = {}
            self.current_session_id = f"sess_{int(time.time())}"

# --- SCHEDULING SYSTEM ---
@dataclass
class ScheduledTask:
    id: str
    agent_name: str
    prompt: str
    time_str: str  # Format: "HH:MM" (24-hour time)
    last_run_date: str = "" # Tracks if it ran today
    is_active: bool = True
    date_str: str = "" # Format: "YYYY-MM-DD"
    repeat: str = "daily" # Options: daily, weekly, monthly, annually
    snooze_until: float = 0.0

class ScheduleManager:
    def __init__(self, storage_dir: str = None):
        self.storage_path = storage_dir or get_storage_path("agents", "schedules.json")
        self.tasks: List[ScheduledTask] = []
        self.load()

    def load(self):
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r") as f:
                    data = json.load(f)
                    self.tasks = [ScheduledTask(**t) for t in data]
            except Exception:
                pass

    def save(self):
        from dataclasses import asdict
        with open(self.storage_path, "w") as f:
            json.dump([asdict(t) for t in self.tasks], f, indent=4)
            
    def add_task(self, agent_name: str, time_str: str, prompt: str, date_str: str = "", repeat: str = "daily"):
        import uuid
        from datetime import datetime
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        task = ScheduledTask(id=uuid.uuid4().hex[:8], agent_name=agent_name, prompt=prompt, time_str=time_str, date_str=date_str, repeat=repeat)
        self.tasks.append(task)
        self.save()
        
    def delete_task(self, task_id: str):
        self.tasks = [t for t in self.tasks if t.id != task_id]
        self.save()
