import os
import json
import asyncio
import threading
import contextlib
from pathlib import Path
from typing import Any, List, Optional
from pydantic import BaseModel, create_model, ConfigDict
import platform

from langchain_core.tools import StructuredTool

try:
    from mcp import Client
    try:
        from mcp import StdioServerParameters
    except ImportError:
        from mcp.client.stdio import StdioServerParameters
    from mcp.client.stdio import stdio_client
    HAS_MCP = True
except Exception as e:
    print(f"MCP V2 Import Error: {e}")
    HAS_MCP = False

MCP_CONFIG_PATH = os.path.join(str(Path.home()), ".federate", "mcp_servers.json")
KNOWN_MCP_TOOLS_PATH = os.path.join(str(Path.home()), ".federate", "known_mcp_tools.json")

CHUNK_SIZE = 256
KEYRING_SERVICE = "Federate"
MCP_CHUNK_COUNT_KEY = "mcp_config_chunk_count"
MCP_CHUNK_PREFIX = "mcp_config_chunk_"
MCP_LEGACY_KEY = "mcp_config_token"

DEFAULT_MCP_CONFIG = '''{
  "mcpServers": {
      
  }
}'''

def _clear_mcp_keyring_chunks(keyring_mod):
    try:
        count_str = keyring_mod.get_password(KEYRING_SERVICE, MCP_CHUNK_COUNT_KEY)
        max_chunks = int(count_str) if (count_str and count_str.isdigit()) else 64
        for idx in range(max_chunks):
            try:
                keyring_mod.delete_password(KEYRING_SERVICE, f"{MCP_CHUNK_PREFIX}{idx}")
            except Exception:
                pass
        try:
            keyring_mod.delete_password(KEYRING_SERVICE, MCP_CHUNK_COUNT_KEY)
        except Exception:
            pass
        try:
            keyring_mod.delete_password(KEYRING_SERVICE, MCP_LEGACY_KEY)
        except Exception:
            pass
    except Exception:
        pass

def load_mcp_config_raw() -> str:
    try:
        from toolbox import is_keyring_locked
        if is_keyring_locked():
            if os.path.exists(MCP_CONFIG_PATH):
                try:
                    with open(MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
                        return f.read()
                except Exception:
                    pass
            return DEFAULT_MCP_CONFIG
    except Exception:
        pass

    raw_str = None
    try:
        import keyring
        count_str = keyring.get_password(KEYRING_SERVICE, MCP_CHUNK_COUNT_KEY)
        if count_str and count_str.isdigit():
            total_chunks = int(count_str)
            parts = []
            for idx in range(total_chunks):
                part = keyring.get_password(KEYRING_SERVICE, f"{MCP_CHUNK_PREFIX}{idx}")
                if part is None:
                    parts = []
                    break
                parts.append(part)
            if parts and len(parts) == total_chunks:
                raw_str = "".join(parts)
    except Exception:
        raw_str = None

    if not raw_str:
        try:
            import keyring
            legacy = keyring.get_password(KEYRING_SERVICE, MCP_LEGACY_KEY)
            if legacy:
                raw_str = legacy
        except Exception:
            pass

    # Migration from legacy plaintext file
    if not raw_str and os.path.exists(MCP_CONFIG_PATH):
        try:
            with open(MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw_str = f.read()
            if raw_str and raw_str.strip():
                save_mcp_config_raw(raw_str)
        except Exception:
            pass

    if not raw_str or not raw_str.strip():
        return DEFAULT_MCP_CONFIG

    return raw_str

def save_mcp_config_raw(raw_json: str) -> bool:
    try:
        from toolbox import is_keyring_locked
        if is_keyring_locked():
            raise RuntimeError("Keyring is locked. Please unlock keyring first.")
    except Exception:
        pass

    import keyring
    _clear_mcp_keyring_chunks(keyring)

    chunks = [raw_json[i:i + CHUNK_SIZE] for i in range(0, len(raw_json), CHUNK_SIZE)]
    for idx, chunk in enumerate(chunks):
        keyring.set_password(KEYRING_SERVICE, f"{MCP_CHUNK_PREFIX}{idx}", chunk)
    keyring.set_password(KEYRING_SERVICE, MCP_CHUNK_COUNT_KEY, str(len(chunks)))

    # Wipe plaintext file from disk
    if os.path.exists(MCP_CONFIG_PATH):
        try:
            os.remove(MCP_CONFIG_PATH)
        except Exception:
            pass
    return True

def _disable_new_mcp_tools(tools):
    if not tools:
        return
    known = set()
    if os.path.exists(KNOWN_MCP_TOOLS_PATH):
        try:
            with open(KNOWN_MCP_TOOLS_PATH, "r", encoding="utf-8") as f:
                known = set(json.load(f))
        except Exception:
            pass

    new_tools = [t.name for t in tools if t.name not in known]
    if not new_tools:
        return

    known.update(new_tools)
    try:
        os.makedirs(os.path.dirname(KNOWN_MCP_TOOLS_PATH), exist_ok=True)
        with open(KNOWN_MCP_TOOLS_PATH, "w", encoding="utf-8") as f:
            json.dump(list(known), f, indent=2)
    except Exception:
        pass

    agents_dir = os.path.join(str(Path.home()), ".federate", "agents")
    if os.path.exists(agents_dir):
        for fname in os.listdir(agents_dir):
            if fname.endswith(".json") and fname not in ("settings.json", "translated_backstories.json", "schedules.json"):
                p = os.path.join(agents_dir, fname)
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    dt = data.get("disabled_tools", ["visual_computer_operation", "send_file_to_telegram"])
                    changed = False
                    for nt in new_tools:
                        if nt not in dt:
                            dt.append(nt)
                            changed = True
                    if changed:
                        data["disabled_tools"] = dt
                        with open(p, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=4)
                except Exception:
                    pass

    try:
        from toolbox import CURRENT_APP
        if CURRENT_APP:
            agent_view = CURRENT_APP.query_one("#ai_agent_view")
            if hasattr(agent_view, "agent_manager"):
                agent_view.agent_manager.load_agents()
                if hasattr(agent_view, "active_agent"):
                    refreshed = agent_view.agent_manager.get_agent(agent_view.active_agent.name)
                    if refreshed:
                        agent_view.active_agent = refreshed
    except Exception:
        pass

_mcp_tools_cache = []
_mcp_loop = asyncio.new_event_loop()
_mcp_sessions = {}
_mcp_exit_stack = contextlib.AsyncExitStack()
_mcp_loading_task = None

def _mcp_bg_thread():
    asyncio.set_event_loop(_mcp_loop)
    _mcp_loop.run_forever()

if HAS_MCP:
    threading.Thread(target=_mcp_bg_thread, daemon=True).start()

import re
import keyword
from pydantic import Field

def _clean_field_name(name: str) -> str:
    clean = re.sub(r'[^a-zA-Z0-9_]', '_', str(name))
    if not clean or clean[0].isdigit() or keyword.iskeyword(clean):
        clean = f"arg_{clean}"
    return clean

def _map_json_type(type_val: Any) -> Any:
    if type_val == "string": return str
    if type_val == "integer": return int
    if type_val == "number": return float
    if type_val == "boolean": return bool
    if type_val == "array": return list
    if type_val == "object": return dict
    return Any

def _json_schema_to_pydantic(name: str, schema: dict):
    class DynamicBase(BaseModel):
        model_config = ConfigDict(extra='allow', populate_by_name=True)

    if not isinstance(schema, dict):
        return create_model("MCPModel", __base__=DynamicBase), ""

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(required, list):
        required = []

    fields = {}
    doc_lines = []

    for prop_name, prop_details in properties.items():
        if not isinstance(prop_details, dict):
            prop_details = {}
            
        raw_type = prop_details.get("type", "any")
        py_type = _map_json_type(raw_type)
        desc = prop_details.get("description") or prop_details.get("title") or ""
        
        is_req = prop_name in required
        
        # Build readable parameter documentation for the agent
        req_marker = "required" if is_req else "optional"
        doc_line = f"  - `{prop_name}` ({raw_type}, {req_marker}): {desc}"
        doc_lines.append(doc_line)

        # Build Pydantic Field
        field_name = _clean_field_name(prop_name)
        field_kwargs = {}
        if field_name != prop_name:
            field_kwargs["alias"] = prop_name
        if desc:
            field_kwargs["description"] = desc

        try:
            if is_req:
                fields[field_name] = (py_type, Field(**field_kwargs))
            else:
                field_kwargs["default"] = None
                fields[field_name] = (Optional[py_type], Field(**field_kwargs))
        except Exception:
            fields[field_name] = (Any, Field(default=None, alias=prop_name if field_name != prop_name else None))

    clean_model_name = _clean_field_name(name) or "MCPModel"
    summary_str = "\n".join(doc_lines)

    try:
        model = create_model(clean_model_name, __base__=DynamicBase, **fields)
        return model, summary_str
    except Exception as e:
        print(f"Pydantic model creation warning for {name}: {e}")
        return DynamicBase, summary_str

async def _connect_and_get_tools():
    if not HAS_MCP:
        return []
    
    raw_config = load_mcp_config_raw()
    try:
        config = json.loads(raw_config)
    except Exception as e:
        print(f"MCP config invalid: {e}")
        return []
            
    mcp_servers = config.get("mcpServers", {})
    tools = []
    
    for server_name, srv_cfg in mcp_servers.items():
        try:
            if server_name in _mcp_sessions:
                client = _mcp_sessions[server_name]
            else:
                env = os.environ.copy()
                for k, v in srv_cfg.get("env", {}).items():
                    env[k] = str(v)
                    
                cmd = srv_cfg.get("command", "")
                if platform.system() == "Windows":
                    if cmd == "npx": cmd = "npx.cmd"
                    elif cmd == "uvx": cmd = "uvx.exe"
                    elif cmd == "npm": cmd = "npm.cmd"
                
                server_params = StdioServerParameters(
                    command=cmd,
                    args=srv_cfg.get("args", []),
                    env=env
                )
                _devnull = open(os.devnull, "w")
                _mcp_exit_stack.callback(_devnull.close)
                client = await _mcp_exit_stack.enter_async_context(
                    Client(stdio_client(server_params, errlog=_devnull))
                )
                _mcp_sessions[server_name] = client

            res = await client.list_tools()
            for t in res.tools:
                try:
                    # MCP 2.x uses input_schema, with inputSchema as wire fallback
                    schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                    if schema is None and isinstance(t, dict):
                        schema = t.get("input_schema") or t.get("inputSchema")
                    if not isinstance(schema, dict):
                        schema = {}

                    args_schema, params_doc = _json_schema_to_pydantic(f"{server_name}_{t.name}_Input", schema)
                    tool_desc = getattr(t, "description", None) or t.name
                    
                    if params_doc:
                        enhanced_desc = f"[{server_name} MCP] {tool_desc}\n\nParameters:\n{params_doc}\n(Note: Omit optional parameters completely if not needed. Do not pass null.)"
                    else:
                        enhanced_desc = f"[{server_name} MCP] {tool_desc}"

                    def make_tool_func(client_ref, tool_name_ref):
                        def _sync_exec(**kwargs):
                            # CRITICAL FIX: Strip all None/null values so Zod/MCP servers never receive them!
                            clean_kwargs = {k: v for k, v in kwargs.items() if v is not None}
                            
                            future = asyncio.run_coroutine_threadsafe(
                                client_ref.call_tool(tool_name_ref, clean_kwargs),
                                _mcp_loop
                            )
                            try:
                                result = future.result(timeout=120)
                                content = getattr(result, "content", [])
                                is_err = getattr(result, "is_error", getattr(result, "isError", False))
                                
                                if not content:
                                    sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
                                    msg = json.dumps(sc) if sc is not None else "Tool executed successfully."
                                    return f"MCP Error: {msg}" if is_err else msg
                                
                                text_outputs = []
                                for c in content:
                                    c_type = getattr(c, "type", "")
                                    if c_type == "text" or hasattr(c, "text"):
                                        text_val = getattr(c, "text", None)
                                        if text_val is not None:
                                            text_outputs.append(str(text_val))
                                        else:
                                            text_outputs.append(str(c))
                                    elif c_type == "image" or hasattr(c, "data"):
                                        data = getattr(c, "data", "")
                                        mime = getattr(c, "mimeType", getattr(c, "mime_type", "image/png"))
                                        if data:
                                            clean_b64 = str(data).strip().replace("\n", "").replace("\r", "")
                                            text_outputs.append(f"[ImageBase64: data:{mime};base64,{clean_b64}]")
                                    elif c_type == "resource" or hasattr(c, "resource"):
                                        res_obj = getattr(c, "resource", c)
                                        if hasattr(res_obj, "text") and res_obj.text:
                                            text_outputs.append(str(res_obj.text))
                                        elif hasattr(res_obj, "blob") and res_obj.blob:
                                            b_mime = getattr(res_obj, "mimeType", getattr(res_obj, "mime_type", "application/octet-stream"))
                                            if b_mime.startswith("image/"):
                                                clean_b64 = str(res_obj.blob).strip().replace("\n", "").replace("\r", "")
                                                text_outputs.append(f"[ImageBase64: data:{b_mime};base64,{clean_b64}]")
                                            else:
                                                text_outputs.append(f"[Attached Resource: {getattr(res_obj, 'uri', 'unknown')}]")

                                final_msg = "\n".join(text_outputs) if text_outputs else "Tool executed successfully."
                                return f"MCP Error: {final_msg}" if is_err else final_msg
                            except Exception as e:
                                return f"MCP Tool Execution Failed: {e}"
                        return _sync_exec

                    tools.append(StructuredTool.from_function(
                        func=make_tool_func(client, t.name),
                        name=t.name,
                        description=enhanced_desc,
                        args_schema=args_schema
                    ))
                except Exception as t_err:
                    print(f"Error loading tool '{getattr(t, 'name', 'unknown')}' from server '{server_name}': {t_err}")
        except Exception as e:
            print(f"Failed to load MCP server '{server_name}': {e}")
            
    _disable_new_mcp_tools(tools)
    return tools

async def _do_fetch_tools(force_reload=False):
    global _mcp_tools_cache, _mcp_sessions, _mcp_exit_stack
    if force_reload:
        try:
            await _mcp_exit_stack.aclose()
        except Exception:
            pass
        _mcp_exit_stack = contextlib.AsyncExitStack()
        _mcp_sessions.clear()
        _mcp_tools_cache = []
        
    if not _mcp_tools_cache:
        _mcp_tools_cache = await _connect_and_get_tools()
    return _mcp_tools_cache

def get_mcp_tools(force_reload=False, timeout=60.0) -> List[StructuredTool]:
    global _mcp_tools_cache
    if not HAS_MCP:
        return []
    
    # If we already have the cache and no reload is forced, return immediately to save UI freezes
    if _mcp_tools_cache and not force_reload:
        return _mcp_tools_cache
        
    try:
        future = asyncio.run_coroutine_threadsafe(_do_fetch_tools(force_reload), _mcp_loop)
        return future.result(timeout=timeout)
    except asyncio.TimeoutError:
        print("MCP Tools fetch timed out. Check abilities list again in a moment.")
        return []
    except Exception as e:
        print(f"MCP Integration Error: {e}")
        return []

def reload_mcp_servers():
    # Trigger an aggressive non-blocking background fetch
    asyncio.run_coroutine_threadsafe(_do_fetch_tools(True), _mcp_loop)