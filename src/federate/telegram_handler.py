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
import json
import time
import threading
import requests
import re
import wave
import numpy as np

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Label, Input, Button, Checkbox
from textual.screen import ModalScreen
from textual import on

from audio_handler import clean_markdown_for_speech, load_audio_config

FEDERATE_DIR = os.path.join(os.path.expanduser("~"), ".federate")
TELEGRAM_CONFIG_FILE = os.path.join(FEDERATE_DIR, "telegram_config.json")
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

def load_telegram_config():
    default_config = {
        "bot_token": "",
        "is_active": False,
        "tts_enabled": False,
        "allow_file_reception": False,
        "allow_file_sending": False,
        "allowed_users": "" 
    }
    if os.path.exists(TELEGRAM_CONFIG_FILE):
        try:
            with open(TELEGRAM_CONFIG_FILE, "r", encoding="utf-8") as f:
                default_config.update(json.load(f))
        except Exception:
            pass

    try:
        from toolbox import is_keyring_locked
        if not is_keyring_locked():
            import keyring
            token = keyring.get_password("Federate", "telegram_bot_token")
            if token:
                default_config["bot_token"] = token
    except Exception:
        pass

    return default_config

def save_telegram_config(config):
    bot_token = config.get("bot_token", "").strip()

    try:
        from toolbox import is_keyring_locked
        if not is_keyring_locked():
            import keyring
            if bot_token:
                keyring.set_password("Federate", "telegram_bot_token", bot_token)
            else:
                try:
                    keyring.delete_password("Federate", "telegram_bot_token")
                except Exception:
                    pass
    except Exception:
        pass

    config_to_save = config.copy()
    config_to_save.pop("bot_token", None)

    try:
        os.makedirs(os.path.dirname(TELEGRAM_CONFIG_FILE), exist_ok=True)
        with open(TELEGRAM_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_to_save, f, indent=4)
    except Exception:
        pass

# --- TEXTUAL UI MODAL ---

class TelegramConfigModal(ModalScreen[str]):
    DEFAULT_CSS = """
    TelegramConfigModal { align: center middle; background: $background 60%; }
    #tele_config_dialog { width: 64; height: auto; border: round $primary; background: $surface; padding: 1 2; }
    .tele_row { layout: horizontal; height: auto; margin-top: 1; align: right middle; }
    .tele_row Button { margin-left: 1; }
    #tele_details_box { border: round $primary; padding: 0 1; margin-bottom: 0; height: auto; background: $surface; }
    """
    
    def compose(self) -> ComposeResult:
        with Vertical(id="tele_config_dialog"):
            yield Label(" Telegram Bot Configuration", classes="pane_title")
            
            with Vertical(id="tele_details_box"):
                yield Checkbox("Enable Telegram Bot", id="tele_active")
                yield Checkbox("Enable Telegram TTS (Voice Replies)", id="tele_tts_active")
                yield Checkbox("Allow File Reception (Telegram ➔ Workspace)", id="tele_allow_files")
                yield Checkbox("Allow Sending Files (Workspace ➔ Telegram)", id="tele_allow_send_files")
                yield Label("Bot Token (from @BotFather):")
                yield Input(id="tele_bot_token", password=True)
                yield Label("Allowed Users (comma separated usernames/IDs, optional):")
                yield Input(id="tele_allowed_users", placeholder="Leave blank for public access")
            
            with Horizontal(classes="tele_row"):
                yield Button("Save & Connect", id="tele_save_btn", variant="success")
                yield Button("Cancel", id="tele_cancel_btn", variant="error")

    def on_mount(self):
        config = load_telegram_config()
        self.query_one("#tele_active", Checkbox).value = config.get("is_active", False)
        self.query_one("#tele_tts_active", Checkbox).value = config.get("tts_enabled", False)
        self.query_one("#tele_allow_files", Checkbox).value = config.get("allow_file_reception", False)
        self.query_one("#tele_allow_send_files", Checkbox).value = config.get("allow_file_sending", False)
        self.query_one("#tele_bot_token", Input).value = config.get("bot_token", "")
        self.query_one("#tele_allowed_users", Input).value = config.get("allowed_users", "")

    @on(Button.Pressed, "#tele_save_btn")
    def save_btn(self):
        config = {
            "is_active": self.query_one("#tele_active", Checkbox).value,
            "tts_enabled": self.query_one("#tele_tts_active", Checkbox).value,
            "allow_file_reception": self.query_one("#tele_allow_files", Checkbox).value,
            "allow_file_sending": self.query_one("#tele_allow_send_files", Checkbox).value,
            "bot_token": self.query_one("#tele_bot_token", Input).value,
            "allowed_users": self.query_one("#tele_allowed_users", Input).value
        }
        save_telegram_config(config)
        self.dismiss("update")

    @on(Button.Pressed, "#tele_cancel_btn")
    def cancel_btn(self):
        self.dismiss("cancel")


# --- TELEGRAM MANAGER ---

class TelegramManager:
    def __init__(self, callback, log_callback=None):
        self.callback = callback
        self.log_callback = log_callback
        self.thread = None
        self.is_running = False
        self.bot_token = ""
        self.offset = 0
        self.allowed_users =[]
        self.tts_enabled = False
        
        # Dictionary to hold the stop signals for our background typing loops
        self.chat_actions = {} 
        self.kokoro_model = None
        self.reload_config()

    def reload_config(self):
        self.stop()
        config = load_telegram_config()
        self.bot_token = config.get("bot_token", "").strip()
        self.tts_enabled = config.get("tts_enabled", False)
        self.allow_file_reception = config.get("allow_file_reception", False)
        self.allow_file_sending = config.get("allow_file_sending", False)
        self.allowed_users = [
            u.strip().lower() for u in config.get("allowed_users", "").split(",") if u.strip()
        ]
        
        if config.get("is_active") and self.bot_token:
            # --- SECURITY FIX: Prevent public bot access ---
            if not self.allowed_users:
                if self.log_callback:
                    self.log_callback("[bold red]Telegram Disabled: 'Allowed Users' cannot be empty. Public access is strictly forbidden.[/bold red]")
                return
            
            # --- SINGLE USER ENFORCEMENT ---
            if len(self.allowed_users) > 1:
                if self.log_callback:
                    self.log_callback("[bold red]Telegram Disabled: Multiple users detected. Only a single authorized operator is allowed to protect single-user session state.[/bold red]")
                return
                
            self.start()

    def start(self):
        if self.is_running or not self.bot_token:
            return
        self.is_running = True
        self.thread = threading.Thread(target=self._poll_worker, daemon=True)
        self.thread.start()
        if self.log_callback:
            self.log_callback("[bold cyan] Telegram Bot connection established and polling.[/bold cyan]")

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=1)
            self.thread = None
            
    def start_chat_action(self, chat_id, action="typing"):
        """
        Mimics your async logic: Runs a background loop pinging Telegram 
        every 4 seconds until the stop_chat_action flips the event flag.
        """
        self.stop_chat_action(chat_id)
            
        # This event acts exactly like your cancel['cancel'] flag
        cancel_event = threading.Event()
        self.chat_actions[chat_id] = cancel_event
        
        def _keep_acting():
            url = f"https://api.telegram.org/bot{self.bot_token}/sendChatAction"
            while not cancel_event.is_set():
                try:
                    requests.post(url, json={"chat_id": chat_id, "action": action}, timeout=2)
                except Exception:
                    pass
                # Wait 4 seconds (Telegram expires it at 5s)
                cancel_event.wait(4.0) 
                
        threading.Thread(target=_keep_acting, daemon=True).start()

    def stop_chat_action(self, chat_id):
        """Sets the flag to True, stopping the background typing loop."""
        if chat_id in self.chat_actions:
            self.chat_actions[chat_id].set()
            del self.chat_actions[chat_id]

    def send_message(self, chat_id, text, title="Agent_Response", voice=None):
        if not self.bot_token: return
            
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        for i in range(0, len(text), 4000):
            chunk = text[i:i+4000]
            try: requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=10)
            except Exception as e:
                if self.log_callback: self.log_callback(f"[bold red]Telegram Send Error:[/bold red] {e}")

        if self.tts_enabled:
            # --- NEW: Pass the voice along ---
            threading.Thread(target=self._send_audio_worker, args=(chat_id, text, title, voice), daemon=True).start()

    def _send_audio_worker(self, chat_id, text, title="Voice_Message", voice=None):
        """Generates TTS locally and sends it as a named audio file."""
        clean_text = clean_markdown_for_speech(text)
        if not clean_text: return
        
        try: from kokoro_onnx import Kokoro
        except ImportError: return

        if not self.kokoro_model:
            try: 
                model_path = os.path.join(REPO_DIR, "kokoro-v1.0.onnx")
                voices_path = os.path.join(REPO_DIR, "voices-v1.0.bin")
                self.kokoro_model = Kokoro(model_path, voices_path)
            except Exception: return

        audio_config = load_audio_config()
        # --- NEW: Use specific agent voice if provided ---
        voice_to_use = voice if voice else audio_config.get("tts_voice", "af_sarah")
        speed = audio_config.get("tts_speed", 1.1)
        
        sentences = re.split(r'(?<=[.!?])\s+', clean_text)
        all_samples = []
        sample_rate = 24000
        
        for s in sentences:
            if s.strip():
                try:
                    samples, sr = self.kokoro_model.create(s.strip(), voice=voice_to_use, speed=speed, lang="en-us")
                    all_samples.append(samples)
                    sample_rate = sr
                except: pass
                    
        if not all_samples: return
        final_audio = np.concatenate(all_samples)
        
        # Security: Clean the title for use as a filename
        safe_title = "".join([c if c.isalnum() else "_" for c in title])
        temp_wav = f"temp_tele_{chat_id}_{int(time.time())}.wav"
        
        try:
            audio_data = np.int16(final_audio * 32767)
            with wave.open(temp_wav, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(audio_data.tobytes())
                
            url_send = f"https://api.telegram.org/bot{self.bot_token}/sendAudio"
            with open(temp_wav, 'rb') as f:
                # Tricking Telegram: we pass a tuple (filename, file_handle, mimetype) 
                # to the 'audio' field to control the name shown on the user's phone.
                files = {"audio": (f"{safe_title}.wav", f, "audio/wav")}
                data = {
                    "chat_id": chat_id, 
                    "title": title, 
                    "performer": "AI Agent", 
                    "protect_content": True
                }
                requests.post(url_send, data=data, files=files, timeout=60)
                
            os.remove(temp_wav)
        except Exception:
            try: os.remove(temp_wav)
            except: pass

    def format_telegram_file_attachment(self, saved_name: str) -> str:
        """Formats a downloaded file directly into an internal attachment payload without using ampersand commands."""
        try:
            from toolbox import CURRENT_APP
            workspace_dir = str(CURRENT_APP.query_one("#dir_tree").path) if CURRENT_APP else os.getcwd()
        except Exception:
            workspace_dir = os.getcwd()

        abs_path = os.path.abspath(os.path.join(workspace_dir, saved_name))
        ext = os.path.splitext(saved_name)[1].lower()

        # Image and PDF formats -> Vision/Image payload
        if ext in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf'}:
            return f"\n[Attached Image: {abs_path}]\n"
        
        # Code/Text formats -> Numbered text payload
        else:
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                try:
                    from toolbox import format_numbered_lines
                    formatted_content = format_numbered_lines(lines, start_line_num=1)
                except Exception:
                    formatted_content = "".join(f"{i+1}: {line}" for i, line in enumerate(lines))
                
                return f"\n--- Content of {saved_name} ---\n{formatted_content}\n--- End of {saved_name} ---\n"
            except Exception as e:
                return f"\n[Attached File: {saved_name} (Failed to read text: {e})]\n"

    def download_file_to_workspace(self, file_id: str, original_filename: str) -> str:
        """Downloads a file securely from Telegram into the active workspace."""
        if not self.bot_token:
            return None

        try:
            # 1. Get remote path from Telegram API
            get_file_url = f"https://api.telegram.org/bot{self.bot_token}/getFile"
            res = requests.get(get_file_url, params={"file_id": file_id}, timeout=15)
            if res.status_code != 200 or not res.json().get("ok"):
                return None

            file_path_remote = res.json()["result"]["file_path"]

            # 2. Get local workspace directory
            try:
                from toolbox import CURRENT_APP
                workspace_dir = str(CURRENT_APP.query_one("#dir_tree").path) if CURRENT_APP else os.getcwd()
            except Exception:
                workspace_dir = os.getcwd()

            # 3. Sanitize filename to prevent path traversal (../../)
            clean_name = os.path.basename(original_filename or file_path_remote)
            clean_name = re.sub(r'[^\w\.\-]', '_', clean_name)

            # Extension Whitelist Guardrail
            ALLOWED_EXTENSIONS = {
                '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp',
                '.py', '.txt', '.md', '.json', '.yaml', '.yml',
                '.csv', '.sh', '.c', '.cpp', '.h', '.rs', '.go', '.js', '.ts'
            }
            ext = os.path.splitext(clean_name)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                if self.log_callback:
                    self.log_callback(f"[bold yellow]Rejected Telegram file {clean_name}: Extension '{ext}' not allowed.[/bold yellow]")
                return None
            if not clean_name:
                clean_name = f"telegram_file_{int(time.time())}"

            local_abs_path = os.path.join(workspace_dir, clean_name)

            # 4. Download file bytes
            download_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path_remote}"
            file_resp = requests.get(download_url, timeout=60, stream=True)
            if file_resp.status_code == 200:
                with open(local_abs_path, "wb") as f:
                    for chunk in file_resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                return clean_name
            return None
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"[bold red]Telegram Download Error:[/bold red] {e}")
            return None

    def _poll_worker(self):
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        while self.is_running:
            try:
                resp = requests.get(url, params={"offset": self.offset, "timeout": 30}, timeout=35)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        for update in data.get("result", []):
                            self.offset = update["update_id"] + 1
                            msg = update.get("message")
                            if msg:
                                chat_id = msg["chat"]["id"]
                                username = msg.get("from", {}).get("username", "").lower()
                                
                                # Refuse group chats
                                if chat_id < 0:
                                    self.send_message(chat_id, "Group chats are disabled for security reasons. Please message me privately.")
                                    continue
                                    
                                # Unconditional auth check
                                if username not in self.allowed_users and str(chat_id) not in self.allowed_users:
                                    self.send_message(chat_id, "Sorry, you are not authorized.")
                                    continue

                                # Extract files (documents or photos)
                                file_id = None
                                file_name = None
                                if "document" in msg:
                                    doc = msg["document"]
                                    file_id = doc.get("file_id")
                                    file_name = doc.get("file_name") or f"file_{int(time.time())}"
                                elif "photo" in msg:
                                    photos = msg["photo"]
                                    if photos:
                                        file_id = photos[-1].get("file_id") # Highest resolution
                                        file_name = f"photo_{int(time.time())}.jpg"

                                caption = msg.get("caption", "").strip()
                                text = msg.get("text", "").strip()

                                if file_id:
                                    if not getattr(self, "allow_file_reception", False):
                                        self.send_message(chat_id, "ℹ️ File reception from Telegram is disabled in settings.")
                                        continue

                                    self.send_message(chat_id, f"📥 Receiving file `{file_name}`...")
                                    saved_name = self.download_file_to_workspace(file_id, file_name)
                                    if saved_name:
                                        # Internal Turn-0 Attachment Formatting (No ampersand parsing required)
                                        attachment_payload = self.format_telegram_file_attachment(saved_name)
                                        if caption:
                                            prompt = f"{attachment_payload}\nUser Instruction: {caption}"
                                        else:
                                            prompt = f"{attachment_payload}\nUser provided the attached file '{saved_name}'. Please inspect it. No need to call read_file it is already injected into your context."
                                        self.callback(chat_id, prompt)
                                    else:
                                        self.send_message(chat_id, f"❌ Failed to download file `{file_name}`.")
                                elif text:
                                    self.callback(chat_id, text)
                else:
                    time.sleep(2)
            except requests.exceptions.Timeout:
                continue 
            except Exception:
                time.sleep(5)
                
    def send_document(self, chat_id, filepath, caption=""):
        """Sends a local file (PDF, MD, etc.) to the Telegram chat."""
        if not self.bot_token or not os.path.exists(filepath):
            return
        
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        try:
            with open(filepath, 'rb') as f:
                requests.post(url, data={"chat_id": chat_id, "caption": caption, "protect_content": False}, files={"document": f}, timeout=60)
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"[bold red]Telegram Document Error:[/bold red] {e}")
                
    def send_voiceover(self, chat_id, full_text, notification_text="", title="Voice_Report"):
        """Sends a short text notification and generates audio from the full text."""
        if not self.bot_token: return
            
        if notification_text:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": notification_text}, timeout=10)

        if self.tts_enabled and full_text.strip():
            threading.Thread(target=self._send_audio_worker, args=(chat_id, full_text, title), daemon=True).start()