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
import secrets
import logging
import hashlib
import base64
import threading
import urllib.parse
import http.server
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import langchain_openai

from textual.screen import ModalScreen
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Label, Input, Button, TextArea
from textual import on, work

logger = logging.getLogger(__name__)

# --- OPENAI CHATGPT OAUTH CONSTANTS ---
CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
CHATGPT_TOKEN_URL = "https://auth.openai.com/oauth/token"
CHATGPT_DEVICE_CODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
CHATGPT_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
CHATGPT_DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
CHATGPT_AUTH_CLAIMS_NAMESPACE = "https://api.openai.com/auth"
CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CHATGPT_CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"

DEFAULT_REDIRECT_HOST = "localhost"
DEFAULT_REDIRECT_PORT = 1455
DEFAULT_REDIRECT_PATH = "/auth/callback"

# Mandatory scopes required by OpenAI to authorize ChatGPT Plus/Pro subscriptions
DEFAULT_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
DEFAULT_STORE_PATH = Path.home() / ".federate" / "chatgpt-auth.json"

HTTP_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://chatgpt.com",
    "Referer": "https://chatgpt.com/"
}

@dataclass(frozen=True)
class _ChatGPTToken:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime
    account_id: Optional[str] = None
    plan_type: Optional[str] = None
    user_id: Optional[str] = None
    id_token: Optional[str] = field(default=None, repr=False)

    def is_expired(self, skew: timedelta = timedelta(minutes=5)) -> bool:
        return datetime.now(timezone.utc) >= (self.expires_at - skew)

def _decode_jwt_claims(token: str) -> dict:
    if not token or token.count(".") < 2: return {}
    try:
        _, payload, _ = token.split(".", 2)
        padding = "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload + padding))
    except Exception: return {}

def _extract_claims(id_token: Optional[str]) -> dict:
    out = {"account_id": None, "plan_type": None, "user_id": None}
    if not id_token: return out
    claims = _decode_jwt_claims(id_token)
    auth = claims.get(CHATGPT_AUTH_CLAIMS_NAMESPACE) or {}
    if isinstance(auth, dict):
        out["account_id"] = auth.get("chatgpt_account_id")
        out["plan_type"] = auth.get("chatgpt_plan_type")
        out["user_id"] = auth.get("chatgpt_user_id")
    return out

def _token_from_response(payload: dict, fallback_refresh: str = None, fallback_account_id: str = None) -> _ChatGPTToken:
    if not payload.get("access_token"):
        raise RuntimeError("OAuth response missing access_token.")
    id_token = payload.get("id_token")
    claims = _extract_claims(id_token)
    refresh_token = payload.get("refresh_token") or fallback_refresh
    if not refresh_token:
        raise RuntimeError("OAuth response missing refresh_token.")
    account_id = claims.get("account_id") or fallback_account_id
    expires_in = int(payload.get("expires_in", 3600))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return _ChatGPTToken(
        access_token=payload["access_token"],
        refresh_token=refresh_token,
        expires_at=expires_at,
        account_id=account_id,
        plan_type=claims.get("plan_type"),
        user_id=claims.get("user_id"),
        id_token=id_token
    )

CHUNK_SIZE = 256
KEYRING_SERVICE = "Federate"
CHUNK_COUNT_KEY = "chatgpt_token_chunk_count"
CHUNK_PREFIX = "chatgpt_token_chunk_"
LEGACY_KEY = "chatgpt_oauth_token"

class _FileChatGPTOAuthTokenProvider:
    def __init__(self, path: Path = DEFAULT_STORE_PATH):
        self.path = path
        self._lock = threading.Lock()

    def _clear_chunks_unlocked(self, keyring_mod):
        try:
            count_str = keyring_mod.get_password(KEYRING_SERVICE, CHUNK_COUNT_KEY)
            max_chunks = int(count_str) if (count_str and count_str.isdigit()) else 32
            for idx in range(max_chunks):
                try:
                    keyring_mod.delete_password(KEYRING_SERVICE, f"{CHUNK_PREFIX}{idx}")
                except Exception:
                    pass
            try:
                keyring_mod.delete_password(KEYRING_SERVICE, CHUNK_COUNT_KEY)
            except Exception:
                pass
            try:
                keyring_mod.delete_password(KEYRING_SERVICE, LEGACY_KEY)
            except Exception:
                pass
        except Exception:
            pass

    def get_token(self) -> _ChatGPTToken:
        with self._lock:
            from toolbox import is_keyring_locked
            if is_keyring_locked():
                raise RuntimeError("Keyring is locked. Please unlock keyring first.")

            import keyring
            data = None

            # 1. Try reading chunked token (256-byte chunks)
            try:
                count_str = keyring.get_password(KEYRING_SERVICE, CHUNK_COUNT_KEY)
                if count_str and count_str.isdigit():
                    total_chunks = int(count_str)
                    parts = []
                    for idx in range(total_chunks):
                        part = keyring.get_password(KEYRING_SERVICE, f"{CHUNK_PREFIX}{idx}")
                        if part is None:
                            parts = []
                            break
                        parts.append(part)
                    if parts and len(parts) == total_chunks:
                        data = json.loads("".join(parts))
            except Exception:
                data = None

            # 2. Backward-compatible check for legacy single entry
            if not data:
                try:
                    raw = keyring.get_password(KEYRING_SERVICE, LEGACY_KEY)
                    if raw:
                        data = json.loads(raw)
                except Exception:
                    data = None

            if not data:
                raise FileNotFoundError("No ChatGPT OAuth token found in keyring.")

            expires_at = datetime.fromisoformat(data["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            
            token = _ChatGPTToken(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=expires_at,
                account_id=data.get("account_id"),
                plan_type=data.get("plan_type"),
                user_id=data.get("user_id"),
                id_token=data.get("id_token")
            )
            
            if token.is_expired():
                try:
                    resp = httpx.post(CHATGPT_TOKEN_URL, data={
                        "grant_type": "refresh_token",
                        "refresh_token": token.refresh_token,
                        "client_id": CHATGPT_CLIENT_ID
                    }, headers={"Accept": "application/json", "User-Agent": HTTP_HEADERS["User-Agent"]}, timeout=30.0)
                    resp.raise_for_status()
                    token = _token_from_response(resp.json(), fallback_refresh=token.refresh_token, fallback_account_id=token.account_id)
                    self.save(token)
                except Exception as e:
                    self.clear()
                    raise RuntimeError(f"Failed to refresh ChatGPT OAuth token: {e}")
            return token

    def save(self, token: _ChatGPTToken):
        with self._lock:
            data = {
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "expires_at": token.expires_at.astimezone(timezone.utc).isoformat(),
                "account_id": token.account_id,
                "plan_type": token.plan_type,
                "user_id": token.user_id
            }
            raw_json = json.dumps(data, separators=(',', ':'))

            from toolbox import is_keyring_locked
            if is_keyring_locked():
                raise RuntimeError("Keyring is locked. Please unlock keyring before saving credentials.")

            import keyring
            # Clear old chunks first
            self._clear_chunks_unlocked(keyring)

            # Save in 256-character chunks
            chunks = [raw_json[i:i + CHUNK_SIZE] for i in range(0, len(raw_json), CHUNK_SIZE)]
            for idx, chunk in enumerate(chunks):
                keyring.set_password(KEYRING_SERVICE, f"{CHUNK_PREFIX}{idx}", chunk)
            keyring.set_password(KEYRING_SERVICE, CHUNK_COUNT_KEY, str(len(chunks)))

    def clear(self):
        with self._lock:
            from toolbox import is_keyring_locked
            if not is_keyring_locked():
                import keyring
                self._clear_chunks_unlocked(keyring)

def is_chatgpt_oauth_agent(agent: Any) -> bool:
    if not agent: return False
    base_url = str(getattr(agent, "base_url", "") or "").lower()
    api_key = str(getattr(agent, "get_api_key", lambda: "")() or "").strip()
    if "chatgpt.com" in base_url or "oauth" in base_url or api_key == "CHATGPT_OAUTH_ACTIVE":
        return True
    if getattr(agent, "use_backup", False):
        b_base_url = str(getattr(agent, "backup_base_url", "") or "").lower()
        b_api_key = str(getattr(agent, "get_backup_api_key", lambda: "")() or "").strip()
        if "chatgpt.com" in b_base_url or "oauth" in b_base_url or b_api_key == "CHATGPT_OAUTH_ACTIVE":
            return True
    return False

def has_valid_chatgpt_token() -> bool:
    try:
        provider = _FileChatGPTOAuthTokenProvider()
        token = provider.get_token()
        return bool(token and token.access_token)
    except Exception:
        return False

# --- LOCAL HTTP CALLBACK SERVER ---

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_result = {}
    callback_path = DEFAULT_REDIRECT_PATH
    authorize_url = ""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        
        # Local 302 HTTP redirect shortcut
        if parsed.path == "/login" and self.authorize_url:
            self.send_response(302)
            self.send_header("Location", self.authorize_url)
            self.end_headers()
            return

        if parsed.path != self.callback_path:
            self.send_response(404)
            self.end_headers()
            return
        
        query = urllib.parse.parse_qs(parsed.query)
        for key in ("code", "state", "error", "error_description"):
            val = query.get(key)
            if val:
                self.server_result[key] = val[0]
                
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        
        if "error" in self.server_result:
            err = self.server_result.get("error_description") or self.server_result["error"]
            html = f"<!doctype html><html><body style='font-family:sans-serif;text-align:center;padding:50px;'><h2>Sign-in failed</h2><p>{err}</p></body></html>"
        else:
            html = "<!doctype html><html><body style='font-family:sans-serif;text-align:center;padding:50px;'><h2 style='color:#137333;'>ChatGPT sign-in complete!</h2><p>You can close this tab and return to FEDERaiDE.</p></body></html>"
        
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        pass

# --- INTERACTIVE TEXTUAL MODAL SCREEN ---

class ChatGPTAuthModal(ModalScreen[bool]):
    DEFAULT_CSS = """
    ChatGPTAuthModal { align: center middle; background: $background 60%; }
    #chatgpt_auth_dialog { width: 75; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    #url_display { margin-bottom: 1; }
    .auth_status { margin: 1 0; text-align: center; color: $accent; text-style: bold; }
    .auth_buttons { layout: horizontal; height: auto; align: right middle; margin-top: 1; }
    .auth_buttons Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="chatgpt_auth_dialog"):
            yield Label(" ChatGPT OAuth Authorization", classes="pane_title")
            yield Label("Opening sign-in page in your default browser...", classes="field_label")
            yield Label("Short Redirect Link:", classes="field_label")
            yield Input(f"http://{DEFAULT_REDIRECT_HOST}:{DEFAULT_REDIRECT_PORT}/login", id="url_display")
            yield Label("Connecting to OpenAI...", id="status_label", classes="auth_status")
            
            with Horizontal(classes="auth_buttons"):
                yield Button("Purge Local", id="purge_btn", variant="warning")
                yield Button("Re-open Browser", id="open_browser_btn", variant="primary")
                yield Button("Cancel", id="cancel_btn", variant="error")

    def on_mount(self):
        self.start_browser_flow()

    @work(thread=True)
    def start_browser_flow(self):
        import secrets, base64, hashlib, urllib.parse, webbrowser, time
        
        host = DEFAULT_REDIRECT_HOST
        port = DEFAULT_REDIRECT_PORT
        callback_path = DEFAULT_REDIRECT_PATH
        redirect_uri = f"http://{host}:{port}{callback_path}"
        state = secrets.token_urlsafe(32)
        
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")

        params = {
            "client_id": CHATGPT_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": DEFAULT_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        authorize_url = f"{CHATGPT_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
        self.authorize_url = authorize_url

        class Handler(_CallbackHandler):
            server_result = {}
            callback_path = DEFAULT_REDIRECT_PATH

        Handler.authorize_url = authorize_url

        class _ReusableHTTPServer(http.server.HTTPServer):
            allow_reuse_address = True

        try:
            server = _ReusableHTTPServer((host, port), Handler)
            server.timeout = 1.0
        except Exception:
            self.start_device_flow()
            return

        def update_ui():
            short_url = f"http://{host}:{port}/login"
            self.query_one("#url_display", Input).value = short_url
            self.query_one("#status_label", Label).update(f"[bold cyan]Open [bold yellow]{short_url}[/bold yellow] in browser[/bold cyan]")
            try:
                webbrowser.open(authorize_url)
            except Exception: pass

        self.app.call_from_thread(update_ui)

        deadline = time.monotonic() + 300.0
        result = {}
        while time.monotonic() < deadline:
            if not self.is_mounted:
                server.server_close()
                return
            server.handle_request()
            if Handler.server_result.get("code") or Handler.server_result.get("error"):
                result = dict(Handler.server_result)
                break
        server.server_close()

        if result.get("state") != state:
            self.app.call_from_thread(self.query_one("#status_label", Label).update, "[bold red]Auth Error: CSRF State mismatch.[/bold red]")
            return

        code = result.get("code")
        if not code:
            err = result.get("error_description") or result.get("error") or "Timed out waiting for callback"
            self.app.call_from_thread(self.query_one("#status_label", Label).update, f"[bold red]Auth Error: {err}[/bold red]")
            return

        tok_resp = httpx.post(CHATGPT_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": CHATGPT_CLIENT_ID,
            "code_verifier": verifier
        }, headers={"Accept": "application/json", "User-Agent": HTTP_HEADERS["User-Agent"]}, timeout=30.0)

        tok_resp.raise_for_status()
        token = _token_from_response(tok_resp.json())
        provider = _FileChatGPTOAuthTokenProvider()
        provider.save(token)

        def success_and_close():
            self.notify("ChatGPT Subscription authorized successfully!", severity="information")
            self.safe_dismiss(True)

        self.app.call_from_thread(success_and_close)

    @work(thread=True)
    def start_device_flow(self):
        try:
            verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")

            resp = httpx.post(
                CHATGPT_DEVICE_CODE_URL, 
                json={
                    "client_id": CHATGPT_CLIENT_ID,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256"
                }, 
                headers=HTTP_HEADERS, 
                timeout=30.0
            )

            if resp.status_code >= 400:
                err_text = resp.text[:120]
                self.app.call_from_thread(
                    self.query_one("#status_label", Label).update, 
                    f"[bold red]OpenAI Error ({resp.status_code}): {err_text}[/bold red]"
                )
                return

            start = resp.json()
            device_auth_id = start.get("device_auth_id") or start.get("device_code")
            user_code = start.get("user_code", "ERROR")
            verification_uri = start.get("verification_url") or start.get("verification_uri") or start.get("verification_uri_complete") or "https://auth.openai.com/codex/device"

            if not device_auth_id:
                err_msg = start.get("error_description") or start.get("error") or "No device_auth_id returned"
                self.app.call_from_thread(
                    self.query_one("#status_label", Label).update, 
                    f"[bold red]Failed: {err_msg}[/bold red]"
                )
                return

            def update_ui():
                self.query_one("#url_display", Input).value = f"{verification_uri} (Code: {user_code})"
                self.query_one("#status_label", Label).update(f"[bold cyan]Code: {user_code} | Waiting for approval in browser...[/bold cyan]") 
            
            self.app.call_from_thread(update_ui)
            self.authorize_url = verification_uri

            deadline = time.monotonic() + 600.0
            auth_code = None
            poll_interval = float(start.get("interval") or start.get("intervalSeconds") or 5.0)
            
            while time.monotonic() < deadline:
                if not self.is_mounted:
                    return
                
                poll_resp = httpx.post(
                    CHATGPT_DEVICE_TOKEN_URL, 
                    json={
                        "client_id": CHATGPT_CLIENT_ID,
                        "device_auth_id": device_auth_id,
                        "user_code": user_code
                    }, 
                    headers=HTTP_HEADERS, 
                    timeout=30.0
                )

                if poll_resp.status_code < 400:
                    poll = poll_resp.json()
                    if poll.get("authorization_code"):
                        auth_code = poll["authorization_code"]
                        break
                    
                    err = poll.get("error")
                    if err == "slow_down":
                        poll_interval += 5.0
                    elif err and err != "authorization_pending":
                        self.app.call_from_thread(
                            self.query_one("#status_label", Label).update, 
                            f"[bold red]Auth Error: {err}[/bold red]"
                        )
                        return

                time.sleep(poll_interval)

            if not auth_code:
                self.app.call_from_thread(
                    self.query_one("#status_label", Label).update, 
                    "[bold red]Timed out waiting for authorization.[/bold red]"
                )
                return

            tok_resp = httpx.post(
                CHATGPT_TOKEN_URL, 
                data={
                    "grant_type": "authorization_code",
                    "code": auth_code,
                    "redirect_uri": CHATGPT_DEVICE_REDIRECT_URI,
                    "client_id": CHATGPT_CLIENT_ID,
                    "code_verifier": verifier
                }, 
                headers={"Accept": "application/json", "User-Agent": HTTP_HEADERS["User-Agent"]}, 
                timeout=30.0
            )

            tok_resp.raise_for_status()
            token = _token_from_response(tok_resp.json())
            provider = _FileChatGPTOAuthTokenProvider()
            provider.save(token)

            def success_and_close():
                self.notify("ChatGPT Subscription authorized successfully!", severity="information")
                self.safe_dismiss(True)

            self.app.call_from_thread(success_and_close)

        except Exception as e:
            def show_err():
                self.query_one("#status_label", Label).update(f"[bold red]Error: {e}[/bold red]")
            self.app.call_from_thread(show_err)

    @on(Button.Pressed, "#purge_btn")
    def purge_token(self):
        provider = _FileChatGPTOAuthTokenProvider()
        provider.clear()
        self.notify("Local ChatGPT OAuth token purged from keyring.", severity="warning")
        self.query_one("#status_label", Label).update("[bold yellow]Token purged. Re-authenticating...[/bold yellow]")
        is_termux = os.path.exists("/data/data/com.termux") or ("TERMUX_VERSION" in os.environ)
        if is_termux:
            self.start_device_flow()
        else:
            self.start_browser_flow()

    @on(Button.Pressed, "#open_browser_btn")
    def open_browser(self):
        import webbrowser
        uri = getattr(self, "authorize_url", "https://auth.openai.com/oauth/authorize")
        try:
            webbrowser.open(uri)
            self.notify("Opened sign-in link in browser.", severity="information")
        except Exception as e:
            self.notify(f"Could not open browser: {e}", severity="error")

    def safe_dismiss(self, result: bool = False):
        try:
            self.dismiss(result)
        except Exception:
            try:
                if self.is_mounted:
                    self.app.pop_screen()
            except Exception:
                pass

    @on(Button.Pressed, "#cancel_btn")
    def cancel(self):
        self.safe_dismiss(False)

# --- HTTP TRANSPORT PROTOCOL TRANSLATOR ---

_provider = _FileChatGPTOAuthTokenProvider()

import platform

def _translate_chat_completions_to_responses(request: httpx.Request) -> httpx.Request:
    """Translates Chat Completions payload (/chat/completions) to Responses API payload (/responses)."""
    try:
        token = _provider.get_token()
    except FileNotFoundError as fnf_err:
        raise RuntimeError("ChatGPT OAuth token not found on disk. Please click 'Authenticate with ChatGPT OAuth' first.") from fnf_err
    except Exception as token_err:
        raise RuntimeError(f"ChatGPT OAuth token error: {token_err}") from token_err

    # CRITICAL FIX: Create perfectly clean headers mimicking `tau` exactly.
    # We do NOT copy request.headers to ensure no Host/Content-Length mismatches reach Cloudflare.
    headers = {
        "Authorization": f"Bearer {token.access_token}",
        "originator": "federaide",
        "User-Agent": f"tau ({platform.system()} {platform.release()}; {platform.machine()})",
        "OpenAI-Beta": "responses=experimental",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    if token.account_id:
        headers["chatgpt-account-id"] = token.account_id

    target_url = CHATGPT_CODEX_RESPONSES_URL

    if request.content:
        try:
            body = json.loads(request.content.decode("utf-8"))
            messages = body.get("messages", [])
            
            instructions = "You are a helpful AI assistant."
            input_msgs = []
            
            assistant_index = 0
            for m in messages:
                role = m.get("role")
                content = m.get("content", "")
                
                if role == "system":
                    instructions = content if isinstance(content, str) else str(content)
                elif role == "user":
                    user_content = []
                    if isinstance(content, str):
                        if content:
                            user_content.append({"type": "input_text", "text": content})
                    elif isinstance(content, list):
                        for block in content:
                            if block.get("type") == "text":
                                user_content.append({"type": "input_text", "text": block.get("text", "")})
                            elif block.get("type") == "image_url":
                                user_content.append({
                                    "type": "input_image",
                                    "detail": "auto",
                                    "image_url": block.get("image_url", {}).get("url", "")
                                })
                    if not user_content:
                        user_content.append({"type": "input_text", "text": " "})
                    input_msgs.append({"role": "user", "content": user_content})
                elif role == "assistant":
                    tool_calls = m.get("tool_calls", [])
                    if isinstance(content, str) and content:
                        input_msgs.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": content, "annotations": []}],
                            "status": "completed",
                            "id": f"msg_{assistant_index}"
                        })
                        assistant_index += 1
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        input_msgs.append({
                            "type": "function_call",
                            "call_id": tc.get("id", f"call_{func.get('name')}"),
                            "name": func.get("name", "unknown"),
                            "arguments": func.get("arguments", "{}")
                        })
                elif role == "tool":
                    output = content if content else "(no tool output)"
                    input_msgs.append({
                        "type": "function_call_output",
                        "call_id": m.get("tool_call_id"),
                        "output": output
                    })

            model_name = body.get("model", "gpt-5.6-luna")

            responses_body = {
                "model": model_name,
                "instructions": instructions,
                "input": input_msgs if input_msgs else [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
                "store": False,
                "stream": True,  # FORCE TRUE: The backend Codex API rejects stream: False
                "text": {"verbosity": "low"},
                "include": ["reasoning.encrypted_content"]
            }
            
            reasoning_effort = body.get("reasoning_effort")
            if reasoning_effort:
                responses_body["reasoning"] = {
                    "effort": reasoning_effort,
                    "summary": "auto"
                }

            tools = body.get("tools", [])
            if tools:
                safe_tools = []
                for t in tools:
                    # LangChain nests the tool data, but the Responses API requires it flattened
                    func = t.get("function", {})
                    safe_tools.append({
                        "type": "function",
                        "name": func.get("name", "tool"),
                        "description": func.get("description", ""),
                        "parameters": func.get("parameters", {}),
                        "strict": None
                    })
                responses_body["tools"] = safe_tools
            
            new_content = json.dumps(responses_body).encode("utf-8")
            
            # Create a completely fresh request object using our safe headers
            return httpx.Request("POST", target_url, headers=headers, content=new_content)
        except Exception as body_err:
            raise RuntimeError(f"Failed to format Responses API body: {body_err}") from body_err

    return httpx.Request("POST", target_url, headers=headers, content=request.content)

def _translate_responses_to_chat_completions(response: httpx.Response, is_stream: bool) -> httpx.Response:
    """Consumes the SSE output from /responses and translates it back into a Chat Completion stream or flat JSON."""
    if response.status_code >= 400:
        return response

    content_str = response.content.decode("utf-8", errors="replace")
    
    text_out = ""
    tool_calls = {}
    translated_sse = ""
    tool_call_indices = {}
    next_tool_idx = 0
    
    base_chunk = {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "gpt-5.6-luna",
    }

    # 1. Parse the forced SSE stream from the provider
    if "data: " in content_str:
        for line in content_str.splitlines():
            line = line.strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[6:])
                    c_type = chunk.get("type")
                    delta_payload = None
                    
                    if c_type == "response.output_text.delta":
                        delta = chunk.get("delta", "")
                        text_out += delta
                        delta_payload = {"content": delta}
                        
                    elif c_type == "response.output_item.added":
                        item = chunk.get("item", {})
                        if item.get("type") == "function_call":
                            call_id = item.get("call_id") or item.get("id") or f"call_{item.get('name')}"
                            if call_id not in tool_call_indices:
                                tool_call_indices[call_id] = next_tool_idx
                                next_tool_idx += 1
                                tool_calls[call_id] = {"name": item.get("name"), "arguments": ""}
                            
                            idx = tool_call_indices[call_id]
                            delta_payload = {
                                "tool_calls": [{
                                    "index": idx,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": item.get("name")}
                                }]
                            }
                            
                    elif c_type == "response.function_call_arguments.delta":
                        call_id = chunk.get("call_id") or chunk.get("item_id")
                        delta = chunk.get("delta", "")
                        if call_id in tool_calls:
                            tool_calls[call_id]["arguments"] += delta
                            
                        idx = tool_call_indices.get(call_id, 0)
                        delta_payload = {
                            "tool_calls": [{
                                "index": idx,
                                "function": {"arguments": delta}
                            }]
                        }
                        
                    if delta_payload and is_stream:
                        out_chunk = dict(base_chunk)
                        out_chunk["choices"] = [{"index": 0, "delta": delta_payload, "finish_reason": None}]
                        translated_sse += f"data: {json.dumps(out_chunk)}\n\n"
                except Exception:
                    pass
    else:
        # Fallback if they ever re-enable flat JSON responses natively
        try:
            resp_json = json.loads(content_str)
            for item in resp_json.get("output", []):
                if item.get("type") == "message" and "content" in item:
                    for c in item["content"]:
                        if c.get("type") == "output_text":
                            text_out += c.get("text", "")
                elif item.get("type") == "function_call":
                    t_id = item.get("id") or f"call_{item.get('name')}"
                    tool_calls[t_id] = {
                        "name": item.get("name"),
                        "arguments": item.get("arguments", "{}")
                    }
        except Exception:
            pass

    # 2. Return SSE Stream to Langchain
    if is_stream:
        stop_chunk = dict(base_chunk)
        stop_chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool_calls else "stop"}]
        translated_sse += f"data: {json.dumps(stop_chunk)}\n\n"
        translated_sse += "data: [DONE]\n\n"
        
        headers = dict(response.headers)
        headers["content-type"] = "text/event-stream"
        headers.pop("content-length", None)
        headers.pop("Content-Length", None)
        
        return httpx.Response(
            status_code=200,
            headers=headers,
            content=translated_sse.encode("utf-8")
        )
        
    # 3. Return Aggregated Flat JSON to Langchain (for non-streaming calls)
    else:
        formatted_tool_calls = []
        for t_id, t in tool_calls.items():
            formatted_tool_calls.append({
                "id": t_id,
                "type": "function",
                "function": {
                    "name": t.get("name", "unknown"),
                    "arguments": t.get("arguments", "{}")
                }
            })

        chat_completion_resp = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gpt-5.6-luna",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": text_out
                    },
                    "finish_reason": "tool_calls" if formatted_tool_calls else "stop"
                }
            ]
        }
        if formatted_tool_calls:
            chat_completion_resp["choices"][0]["message"]["tool_calls"] = formatted_tool_calls

        headers = dict(response.headers)
        headers["content-type"] = "application/json"
        headers.pop("content-length", None)
        headers.pop("Content-Length", None)
        
        return httpx.Response(
            status_code=200,
            headers=headers,
            content=json.dumps(chat_completion_resp).encode("utf-8")
        )

class ChatGPTAuthTransport(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "chatgpt.com" in url_str or "codex" in url_str or "oauth" in url_str:
            try:
                is_stream = False
                if request.content:
                    try:
                        is_stream = json.loads(request.content.decode("utf-8")).get("stream", False)
                    except Exception:
                        pass
                        
                translated_req = _translate_chat_completions_to_responses(request)
                with httpx.Client(timeout=120.0, follow_redirects=True) as client:
                    resp = client.send(translated_req)
                return _translate_responses_to_chat_completions(resp, is_stream)
            except Exception as e:
                err_resp = {
                    "error": {
                        "message": f"ChatGPT OAuth Transport Error: {e}",
                        "type": "chatgpt_oauth_error"
                    }
                }
                return httpx.Response(status_code=500, content=json.dumps(err_resp).encode("utf-8"))
        return super().handle_request(request)

class AsyncChatGPTAuthTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "chatgpt.com" in url_str or "codex" in url_str or "oauth" in url_str:
            try:
                is_stream = False
                if request.content:
                    try:
                        is_stream = json.loads(request.content.decode("utf-8")).get("stream", False)
                    except Exception:
                        pass
                        
                translated_req = _translate_chat_completions_to_responses(request)
                async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                    resp = await client.send(translated_req)
                return _translate_responses_to_chat_completions(resp, is_stream)
            except Exception as e:
                err_resp = {
                    "error": {
                        "message": f"ChatGPT OAuth Transport Error: {e}",
                        "type": "chatgpt_oauth_error"
                    }
                }
                return httpx.Response(status_code=500, content=json.dumps(err_resp).encode("utf-8"))
        return await super().handle_async_request(request)

_orig_init = langchain_openai.ChatOpenAI.__init__

def _patched_chat_openai_init(self, *args, **kwargs):
    base_url = str(kwargs.get("base_url") or "")
    api_key = str(kwargs.get("api_key") or "")
    
    if "oauth" in base_url or "chatgpt" in base_url or api_key == "CHATGPT_OAUTH_ACTIVE":
        if not kwargs.get("http_client"):
            kwargs["http_client"] = httpx.Client(transport=ChatGPTAuthTransport(), timeout=120.0)
        if not kwargs.get("async_client"):
            kwargs["async_client"] = httpx.AsyncClient(transport=AsyncChatGPTAuthTransport(), timeout=120.0)
        
        kwargs["api_key"] = "CHATGPT_OAUTH_ACTIVE"
        kwargs["base_url"] = CHATGPT_CODEX_BASE_URL
            
    _orig_init(self, *args, **kwargs)

langchain_openai.ChatOpenAI.__init__ = _patched_chat_openai_init