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
import re
import queue
import threading
import urllib.request
import urllib.error
import collections
import numpy as np
import time

# Base directory where this python code actually lives
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# Audio/AI Dependencies (Handled gracefully)
try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    from kokoro_onnx import Kokoro
except ImportError:
    Kokoro = None

try:
    import sherpa_onnx
except ImportError:
    sherpa_onnx = None

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual.widgets import Label, Input, Button, RichLog
from textual.screen import ModalScreen
from textual import on

FEDERATE_DIR = os.path.join(os.path.expanduser("~"), ".federate")
AUDIO_CONFIG_FILE = os.path.join(FEDERATE_DIR, "audio_config.json")
_DOWNLOAD_LOCK = threading.Lock()

def _download_file(url: str, dest_path: str, log_cb=None, max_retries=3):
    # Ensure absolute path and define temporary part file
    dest_path = os.path.abspath(dest_path)
    filename = os.path.basename(dest_path)
    part_path = dest_path + ".part"

    # Helper function for logging
    def log_msg(msg, style="dim yellow"):
        formatted = f"[{style}]{msg}[/{style}]"
        if log_cb:
            log_cb(formatted)
        else:
            try:
                from toolbox import log_tool
                log_tool(formatted)
            except ImportError:
                print(msg)

    # Helper function for Textual UI notifications
    def ui_notify(msg, title="Information", severity="information"):
        try:
            from toolbox import CURRENT_APP
            if CURRENT_APP:
                CURRENT_APP.call_from_thread(
                    CURRENT_APP.notify, 
                    msg, 
                    title=title, 
                    severity=severity,
                    timeout=3.0
                )
        except Exception:
            pass

    with _DOWNLOAD_LOCK:
        # 1. Fetch remote file size to verify integrity
        expected_size = 0
        try:
            req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                expected_size = int(resp.headers.get('Content-Length', 0))
        except Exception:
            pass # Server might block HEAD requests, proceed anyway

        # 2. Check if file already exists and is fully downloaded
        if os.path.exists(dest_path):
            if expected_size > 0:
                local_size = os.path.getsize(dest_path)
                if local_size == expected_size:
                    return # Fully downloaded, nothing to do
                else:
                    log_msg(f"Incomplete file detected: {filename} ({local_size}/{expected_size} bytes). Redownloading...", "dim yellow")
            else:
                return # Cannot verify size, assume existing file is fine

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        # 3. Download with Retries, Progress Tracking, and Chunking
        for attempt in range(1, max_retries + 1):
            try:
                log_msg(f" Downloading model: {filename} (Attempt {attempt}/{max_retries})...")
                if attempt == 1:
                    ui_notify(f"Starting download for {filename}...", title="Model Download")
                
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15) as response:
                    total_size = int(response.headers.get('Content-Length', 0))
                    block_size = 65536  # 64KB chunks for stability/speed
                    downloaded = 0
                    last_notified = -10 # Ensure 0% gets logged immediately
                    
                    with open(part_path, 'wb') as f:
                        while True:
                            buffer = response.read(block_size)
                            if not buffer:
                                break
                            f.write(buffer)
                            downloaded += len(buffer)
                            
                            # Calculate and report progress
                            if total_size > 0:
                                progress = int((downloaded / total_size) * 100)
                                if progress >= last_notified + 10:
                                    mb_dl = downloaded / (1024 * 1024)
                                    mb_tot = total_size / (1024 * 1024)
                                    p_msg = f"{filename}: {progress}% ({mb_dl:.1f}MB / {mb_tot:.1f}MB)"
                                    
                                    log_msg(f" {p_msg}")
                                    ui_notify(p_msg, title="Downloading...")
                                    last_notified = progress
                                    
                    # Verify download didn't drop before finishing
                    if total_size > 0 and downloaded < total_size:
                        raise urllib.error.URLError(f"Connection interrupted. Downloaded {downloaded} of {total_size} bytes.")
                
                # 4. Atomic rename - rename .part to final file only if fully successful
                os.replace(part_path, dest_path)
                
                log_msg(f" Finished downloading {filename}", "dim green")
                ui_notify(f"Successfully downloaded {filename}!", title="Download Complete", severity="success")
                return # Exit on success
                
            except Exception as e:
                log_msg(f" Error downloading {filename}: {e}", "bold red")
                if attempt == max_retries:
                    if os.path.exists(part_path):
                        try: os.remove(part_path)
                        except: pass
                    ui_notify(f"Failed to download {filename}: {e}", title="Download Failed", severity="error")
                    raise e
                
                time.sleep(2) # Wait 2 seconds before retrying

def load_audio_config():
    default_config = {
        "tts_voice": "af_sarah",
        "tts_speed": 1.1,
        "stt_start_words": "AGENT, ASSISTANT, COMPUTER",
        "stt_stop_words": "STOP LISTENING, END DICTATION",
        "stt_send_words": "EXECUTE, FINISH, SEND IT",
        "stt_delete_words": "DELETE LAST, REMOVE LAST, SCRAP THAT",
        "stt_agent_hotword": "ATTENTION", # <-- Default invocation keyword
        "stt_device": "",
        "stt_energy_threshold": 0.005,
        "stt_silence_timeout": 1.2,
        "stt_pre_roll": 0.5,
        "stt_min_speech_duration": 0.4,
        "stt_num_threads": 2,
        "stt_feature_dim": 80,
        "stt_decoding_method": "modified_beam_search",
        "stt_max_active_paths": 14
    }
    if os.path.exists(AUDIO_CONFIG_FILE):
        try:
            with open(AUDIO_CONFIG_FILE, "r") as f:
                data = json.load(f)
                default_config.update(data)
        except Exception:
            pass
    return default_config

def save_audio_config(config):
    try:
        with open(AUDIO_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)
    except Exception:
        pass

def clean_markdown_for_speech(text: str) -> str:
    """Strips markdown artifacts so TTS reads naturally, while PRESERVING linebreaks and punctuation."""
    if not text: return ""
    # 1. Remove large code blocks
    text = re.sub(r'```.*?```', '\n[Code block omitted]\n', text, flags=re.DOTALL)
    # 2. Extract text from inline code ticks (e.g. `variable` -> variable)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # 3. Strip URLs
    text = re.sub(r'http[s]?://\S+', 'link omitted', text)
    # 4. Extract text from markdown links
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    # 5. Strip formatting characters but KEEP newlines and punctuation
    text = re.sub(r'[*_#~>]+', '', text)
    
    # Step A: Swap periods surrounded by numbers with " point " (e.g., 3.14 -> 3 point 14)
    text = re.sub(r'(?<=\d)\.(?=\d)', ' point ', text)
    
    # Step B: Swap other internal periods with " dot " (e.g., y.com -> y dot com)
    text = re.sub(r'(?<=\w)\.(?=\w)', ' dot ', text)
    
    # 6. Clean up horizontal whitespace (spaces/tabs) without touching newlines
    text = re.sub(r'[ \t]+', ' ', text)
    # 7. Collapse excessive newlines into standard paragraphs
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# --- TEXTUAL UI MODAL ---

class AudioConfigModal(ModalScreen[str]):
    DEFAULT_CSS = """
    AudioConfigModal { align: center middle; background: $background 60%; }
    #audio_config_dialog { width: 75; height: 90%; border: round $primary; background: $surface; padding: 0; }
    #audio_scroll { padding: 1 2; }
    .section_label { background: $primary; color: $text; padding: 0 1; margin-top: 1; text-style: bold; width: 100%; }
    .field_container { margin-top: 1; height: auto; width: 100%; }
    .field_label { color: $text; text-style: bold; margin-bottom: 0; width: 100%; }
    .field_help { color: $text-muted; text-style: italic; margin-bottom: 0; text-wrap: wrap; height: auto; width: 100%; }
    Input { width: 100%; margin-top: 0; margin-bottom: 1; border: round $accent; }
    #audio_actions { margin-top: 1; align: right middle; height: auto; border-top: solid $primary; padding: 1 2; }
    #audio_actions Button { margin-left: 1; }
    """
    
    def compose(self) -> ComposeResult:
        with Vertical(id="audio_config_dialog"):
            yield Label(" Audio Configuration", classes="pane_title")
            
            with VerticalScroll(id="audio_scroll"):
                yield Label("Text-to-Speech (TTS) Settings", classes="section_label")
                
                with Vertical(classes="field_container"):
                    yield Label("Default TTS Voice", classes="field_label")
                    yield Label("Name of the voice file to use by default (e.g., af_sarah, af_bella, am_adam).", classes="field_help")
                    yield Input(id="tts_voice", placeholder="Voice (e.g., af_sarah)")
                    
                with Vertical(classes="field_container"):
                    yield Label("Voice Playback Speed", classes="field_label")
                    yield Label("Multiplier for playback speed (typically 1.0 to 1.5).", classes="field_help")
                    yield Input(id="tts_speed", placeholder="Speed (e.g., 1.1)")
                    
                yield Label("Speech-to-Text (STT) Settings", classes="section_label")
                
                with Vertical(classes="field_container"):
                    yield Label("Agent/Team/Room Invocation Hotword", classes="field_label")
                    yield Label("The keyword used to invoke specific agents, team, or room (default: ATTENTION).", classes="field_help")
                    yield Input(id="stt_agent_hotword", placeholder="Agent Hotword (e.g., ATTENTION)")

                with Vertical(classes="field_container"):
                    yield Label("Dictation Trigger Words", classes="field_label")
                    yield Label("Hotwords to start voice dictation (comma separated, e.g. AGENT, ASSISTANT, COMPUTER).", classes="field_help")
                    yield Input(id="stt_start", placeholder="Start Words")
                    
                with Vertical(classes="field_container"):
                    yield Label("Dictation Pause/Stop Words", classes="field_label")
                    yield Label("Hotwords to pause dictation and write to the text box (comma separated, e.g. STOP LISTENING).", classes="field_help")
                    yield Input(id="stt_stop", placeholder="Stop/Pause Words")
                    
                with Vertical(classes="field_container"):
                    yield Label("Dictation Send/Execute Words", classes="field_label")
                    yield Label("Hotwords to immediately send the transcribed message (comma separated, e.g. EXECUTE, SEND IT).", classes="field_help")
                    yield Input(id="stt_send", placeholder="Send/Execute Words")
                    
                with Vertical(classes="field_container"):
                    yield Label("Dictation Deletion Words", classes="field_label")
                    yield Label("Hotwords to clear the last appended statement (comma separated, e.g. DELETE LAST, SCRAP THAT).", classes="field_help")
                    yield Input(id="stt_delete", placeholder="Delete Words")
                    
                with Vertical(classes="field_container"):
                    yield Label("Microphone Device ID", classes="field_label")
                    yield Label("Optional integer index of the preferred sound card input (leave blank for OS default).", classes="field_help")
                    yield Input(id="stt_device", placeholder="Mic Device ID")

                with Vertical(classes="field_container"):
                    yield Label("Smart Microphone Energy Threshold", classes="field_label")
                    yield Label("Silence noise gate threshold (default: 0.005). Lower values are more sensitive; higher values filter background hum.", classes="field_help")
                    yield Input(id="stt_energy_threshold", placeholder="Threshold (e.g., 0.005)")

                with Vertical(classes="field_container"):
                    yield Label("Dictation Silence Timeout", classes="field_label")
                    yield Label("Silence duration (seconds) before speech is finalized (default: 1.2).", classes="field_help")
                    yield Input(id="stt_silence_timeout", placeholder="Timeout (e.g., 1.2)")

                with Vertical(classes="field_container"):
                    yield Label("Dictation Pre-Roll Buffer", classes="field_label")
                    yield Label("Duration (seconds) of background audio captured before voice start (default: 0.5).", classes="field_help")
                    yield Input(id="stt_pre_roll", placeholder="Pre-roll (e.g., 0.5)")

                with Vertical(classes="field_container"):
                    yield Label("Minimum Speech Duration", classes="field_label")
                    yield Label("Ignores voice segments shorter than this threshold (default: 0.4).", classes="field_help")
                    yield Input(id="stt_min_speech_duration", placeholder="Duration (e.g., 0.4)")

                with Vertical(classes="field_container"):
                    yield Label("Transducer Background Threads", classes="field_label")
                    yield Label("CPU threads allocated to the background hotword transducer model (default: 2).", classes="field_help")
                    yield Input(id="stt_num_threads", placeholder="Threads (e.g., 2)")

                with Vertical(classes="field_container"):
                    yield Label("Acoustic Feature Dimension", classes="field_label")
                    yield Label("Zipformer neural filterbanks dimension size (default: 80).", classes="field_help")
                    yield Input(id="stt_feature_dim", placeholder="Dimension (e.g., 80)")

                with Vertical(classes="field_container"):
                    yield Label("Decoding Method", classes="field_label")
                    yield Label("Transducer decoding strategy (greedy_search or modified_beam_search).", classes="field_help")
                    yield Input(id="stt_decoding_method", placeholder="Decoding Method (e.g., modified_beam_search)")

                with Vertical(classes="field_container"):
                    yield Label("Maximum Beam Search Paths", classes="field_label")
                    yield Label("Max active beam paths tracked during modified_beam_search (default: 14).", classes="field_help")
                    yield Input(id="stt_max_active_paths", placeholder="Max Paths (e.g., 14)")
                    
            with Horizontal(id="audio_actions"):
                yield Button("Save", id="save_audio_btn", variant="success")
                yield Button("Cancel", id="cancel_audio_btn", variant="error")

    def on_mount(self):
        config = load_audio_config()
        self.query_one("#tts_voice", Input).value = config.get("tts_voice", "")
        self.query_one("#tts_speed", Input).value = str(config.get("tts_speed", ""))
        self.query_one("#stt_agent_hotword", Input).value = config.get("stt_agent_hotword", "")
        self.query_one("#stt_start", Input).value = config.get("stt_start_words", "")
        self.query_one("#stt_stop", Input).value = config.get("stt_stop_words", "")
        self.query_one("#stt_send", Input).value = config.get("stt_send_words", "")
        self.query_one("#stt_delete", Input).value = config.get("stt_delete_words", "")
        self.query_one("#stt_device", Input).value = str(config.get("stt_device", ""))
        self.query_one("#stt_energy_threshold", Input).value = str(config.get("stt_energy_threshold", 0.005))
        self.query_one("#stt_silence_timeout", Input).value = str(config.get("stt_silence_timeout", 1.2))
        self.query_one("#stt_pre_roll", Input).value = str(config.get("stt_pre_roll", 0.5))
        self.query_one("#stt_min_speech_duration", Input).value = str(config.get("stt_min_speech_duration", 0.4))
        self.query_one("#stt_num_threads", Input).value = str(config.get("stt_num_threads", 2))
        self.query_one("#stt_feature_dim", Input).value = str(config.get("stt_feature_dim", 80))
        self.query_one("#stt_decoding_method", Input).value = str(config.get("stt_decoding_method", "modified_beam_search"))
        self.query_one("#stt_max_active_paths", Input).value = str(config.get("stt_max_active_paths", 14))

    @on(Button.Pressed, "#save_audio_btn")
    def save_btn(self):
        try: speed = float(self.query_one("#tts_speed", Input).value)
        except: speed = 1.1
        
        device_val = self.query_one("#stt_device", Input).value
        try: device = int(device_val) if device_val.strip() else ""
        except: device = ""

        try: threshold = float(self.query_one("#stt_energy_threshold", Input).value)
        except: threshold = 0.005

        try: silence_timeout = float(self.query_one("#stt_silence_timeout", Input).value)
        except: silence_timeout = 1.2

        try: pre_roll = float(self.query_one("#stt_pre_roll", Input).value)
        except: pre_roll = 0.5

        try: min_speech = float(self.query_one("#stt_min_speech_duration", Input).value)
        except: min_speech = 0.4

        try: num_threads = int(self.query_one("#stt_num_threads", Input).value)
        except: num_threads = 2

        try: feature_dim = int(self.query_one("#stt_feature_dim", Input).value)
        except: feature_dim = 80

        decoding_method = self.query_one("#stt_decoding_method", Input).value.strip() or "modified_beam_search"

        try: max_paths = int(self.query_one("#stt_max_active_paths", Input).value)
        except: max_paths = 14

        # --- PARAMETER VALIDATION GUARDRAILS ---
        if feature_dim != 80:
            self.notify("Error: Feature Dimension must be exactly 80 for the packaged Zipformer model.", severity="error")
            return

        if decoding_method not in ["greedy_search", "modified_beam_search"]:
            self.notify("Error: Decoding Method must be 'greedy_search' or 'modified_beam_search'.", severity="error")
            return

        if num_threads < 1 or num_threads > 16:
            self.notify("Error: Transducer Threads must be between 1 and 16.", severity="error")
            return

        if max_paths < 1 or max_paths > 100:
            self.notify("Error: Maximum Beam Search Paths must be between 1 and 100.", severity="error")
            return

        if silence_timeout < 0.1 or silence_timeout > 10.0:
            self.notify("Error: Silence Timeout must be between 0.1 and 10.0 seconds.", severity="error")
            return

        if pre_roll < 0.0 or pre_roll > 5.0:
            self.notify("Error: Pre-Roll Buffer must be between 0.0 and 5.0 seconds.", severity="error")
            return

        if min_speech < 0.0 or min_speech > 5.0:
            self.notify("Error: Minimum Speech Duration must be between 0.0 and 5.0 seconds.", severity="error")
            return
        
        config = load_audio_config()
        config.update({
            "tts_voice": self.query_one("#tts_voice", Input).value,
            "tts_speed": speed,
            "stt_start_words": self.query_one("#stt_start", Input).value,
            "stt_stop_words": self.query_one("#stt_stop", Input).value,
            "stt_send_words": self.query_one("#stt_send", Input).value,
            "stt_delete_words": self.query_one("#stt_delete", Input).value,
            "stt_agent_hotword": self.query_one("#stt_agent_hotword", Input).value,
            "stt_device": device,
            "stt_energy_threshold": threshold,
            "stt_silence_timeout": silence_timeout,
            "stt_pre_roll": pre_roll,
            "stt_min_speech_duration": min_speech,
            "stt_num_threads": num_threads,
            "stt_feature_dim": feature_dim,
            "stt_decoding_method": decoding_method,
            "stt_max_active_paths": max_paths
        })
        save_audio_config(config)
        self.dismiss("update")

    @on(Button.Pressed, "#cancel_audio_btn")
    def cancel_btn(self):
        self.dismiss("cancel")


class MicTestModal(ModalScreen[None]):
    """An isolated testing sandbox to dial in STT and voice triggers in real-time."""
    
    DEFAULT_CSS = """
    MicTestModal { align: center middle; background: $background 60%; }
    #mictest_dialog { width: 75; height: 90%; border: round $primary; background: $surface; padding: 0; }
    #mictest_header_box { padding: 1 2; margin-bottom: 0; height: auto; background: $surface; border-bottom: solid $primary 30%; overflow-x: scroll; overflow-y: hidden; }
    #mictest_header_box Label { text-wrap: nowrap; width: auto; }
    #mictest_log { height: 1fr; border: none; background: $boost; padding: 0 1; }
    .mictest_row { layout: horizontal; height: auto; margin-top: 1; align: right middle; padding: 1 2; border-top: solid $primary 30%; }
    .mictest_row Button { margin-left: 1; }
    """
    
    def __init__(self, stt_manager, **kwargs):
        super().__init__(**kwargs)
        self.stt_manager = stt_manager
        self.orig_log_cb = stt_manager.log_callback
        self.orig_cb = stt_manager.callback
        
    def compose(self) -> ComposeResult:
        with Vertical(id="mictest_dialog"):
            yield Label(" Microphone & Hotword Tester", classes="pane_title")
            
            config = load_audio_config()
            start_words = config.get("stt_start_words", "AGENT, ASSISTANT, COMPUTER")
            stop_words = config.get("stt_stop_words", "STOP LISTENING, END DICTATION")
            send_words = config.get("stt_send_words", "EXECUTE, FINISH, SEND IT")
            delete_words = config.get("stt_delete_words", "DELETE LAST, REMOVE LAST, SCRAP THAT")
            agent_hotword = config.get("stt_agent_hotword", "ATTENTION")
            energy_threshold = config.get("stt_energy_threshold", 0.005)
            
            with Vertical(id="mictest_header_box"):
                yield Label(f"[bold green]Dictation Triggers:[/] {start_words}")
                yield Label(f"[bold yellow]Pause/Stop Hotwords:[/] {stop_words}")
                yield Label(f"[bold red]Deletion Hotwords:[/] {delete_words}")
                yield Label(f"[bold cyan]Send/Execute Hotwords:[/] {send_words}")
                yield Label(f"[bold magenta]Agent Invocation Trigger:[/] {agent_hotword} <agent_name>")
                yield Label(f"[bold]Settings:[/] Device ID: {config.get('stt_device') or 'Default'}, Energy Gate: {energy_threshold}, Silence Timeout: {config.get('stt_silence_timeout', 1.2)}s, Pre-Roll: {config.get('stt_pre_roll', 0.5)}s, Min Speech: {config.get('stt_min_speech_duration', 0.4)}s, Agent Hotword: {config.get('stt_agent_hotword', 'ATTENTION')}, Threads: {config.get('stt_num_threads', 2)}, Feature Dim: {config.get('stt_feature_dim', 80)}, Decoding: {config.get('stt_decoding_method', 'modified_beam_search')}, Max Paths: {config.get('stt_max_active_paths', 14)}")
                
            yield RichLog(id="mictest_log", markup=True, wrap=True, auto_scroll=True)
            
            with Horizontal(classes="mictest_row"):
                yield Button("Close Test", id="mictest_close_btn", variant="error")

    def on_mount(self):
        # Hijack callbacks and activate sandbox test-mode
        self.stt_manager.log_callback = self.write_log
        self.stt_manager.callback = self.test_callback
        self.stt_manager.test_mode = True
        
        self.query_one("#mictest_close_btn").focus()
        
        self.write_log("[bold green]Starting Hotword STT engine in sandboxed test mode...[/]")
        started = self.stt_manager.start_hotword()
        if not started:
            self.write_log("[bold red]Failed to initialize STT hardware. Check your Mic Device ID.[/]")

    def write_log(self, msg: str):
        try:
            log_widget = self.query_one("#mictest_log", RichLog)
            self.app.call_from_thread(log_widget.write, msg)
        except Exception:
            pass

    def test_callback(self, text: str, action: str = "append"):
        if action == "append":
            self.write_log(f"[bold green] [STT APPEND TO UI]:[/] {text}")
        elif action == "delete":
            self.write_log("[bold red] [STT DELETE SENTENCE]: (Last statement popped from UI)[/]")
        elif action == "submit":
            self.write_log(f"[bold cyan] [STT SUBMIT & EXECUTE PROMPT]:[/] {text}")

    @on(Button.Pressed, "#mictest_close_btn")
    def close_btn(self):
        self.dismiss()

    def on_unmount(self):
        # Shut down engine and cleanly restore standard operational callbacks
        self.stt_manager.stop()
        self.stt_manager.log_callback = self.orig_log_cb
        self.stt_manager.callback = self.orig_cb
        self.stt_manager.test_mode = False


# --- TTS MANAGER ---

AUDIO_LOCK = threading.Lock()

class TTSManager:
    def __init__(self):
        self.model = None
        self.audio_queue = queue.Queue()
        self.generator_queue = queue.Queue() # Queues sentences dynamically alongside their assigned voice
        self.stop_event = threading.Event() 
        self.split_pattern = re.compile(r'([.!?]+(?:\s|\n)|(?:\n+))')
        
        self.worker_thread = threading.Thread(target=self._audio_worker, daemon=True)
        self.worker_thread.start()
        
        self.generator_thread = threading.Thread(target=self._generator_worker, daemon=True)
        self.generator_thread.start()
        
        self.text_stream_buffer = {}
        self.reload_config()

    def reload_config(self):
        config = load_audio_config()
        self.voice = config.get("tts_voice", "af_sarah")
        self.speed = config.get("tts_speed", 1.1)

    def load_model(self):
        if not Kokoro or not sd: return
        if not self.model:
            model_path = os.path.join(FEDERATE_DIR, "kokoro-v1.0.onnx")
            voices_path = os.path.join(FEDERATE_DIR, "voices-v1.0.bin")
            try:
                _download_file("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx", model_path)
                _download_file("https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin", voices_path)
            except Exception:
                return
            self.model = Kokoro(model_path, voices_path)

    def stop_all_audio(self):
        """Thread-safe request to stop audio."""
        self.stop_event.set()
        
        # Drain the generator queue
        with self.generator_queue.mutex:
            self.generator_queue.queue.clear()
            
        # Drain the audio queue
        while not self.audio_queue.empty():
            try: self.audio_queue.get_nowait()
            except: break
            
        self.text_stream_buffer = ""

    def _audio_worker(self):
        while True:
            item = self.audio_queue.get()
            if item is None: break
            
            if self.stop_event.is_set():
                self.audio_queue.task_done()
                continue
                
            samples, sample_rate = item
            
            try:
                with AUDIO_LOCK:
                    if sd and not self.stop_event.is_set():
                        sd.play(samples, sample_rate)
                        while not self.stop_event.is_set():
                            try:
                                if not sd.get_stream().active: break
                            except: break
                            sd.sleep(50)
                        
                        if self.stop_event.is_set():
                            sd.stop()
            except Exception:
                pass
            finally:
                self.audio_queue.task_done()

    def _generator_worker(self):
        """Dedicated thread to generate audio sequentially using the voice assigned to each chunk."""
        while True:
            item = self.generator_queue.get()
            if item is None: break
            
            if self.stop_event.is_set():
                self.generator_queue.task_done()
                continue
                
            text, voice_to_use = item
            try:
                self.load_model()
                if self.model and not self.stop_event.is_set():
                    # Sentence splitting right before synthesis to avoid phoneme limit crashes on large blocks
                    for s in re.split(r'(?<=[.!?])\s+', text):
                        if s.strip() and not self.stop_event.is_set():
                            samples, sample_rate = self.model.create(s.strip(), voice=voice_to_use, speed=self.speed, lang="en-us")
                            if not self.stop_event.is_set():
                                self.audio_queue.put((samples, sample_rate))
            except Exception:
                pass
            finally:
                self.generator_queue.task_done()

    def start_stream(self, voice=None, agent_name: str = "default"):
        """Prepares the TTS to receive a chunked token stream."""
        self.stop_event.clear()
        if not isinstance(self.text_stream_buffer, dict):
            self.text_stream_buffer = {}
        self.text_stream_buffer[agent_name] = ""
        if voice:
            self.voice = voice
        else:
            self.reload_config()

    def stream_text(self, chunk: str, agent_name: str = "default", voice: str = None):
        """Appends chunks to the buffer to wait until the entire message arrives."""
        if not chunk or self.stop_event.is_set(): return
        if not isinstance(self.text_stream_buffer, dict):
            self.text_stream_buffer = {}
        if agent_name not in self.text_stream_buffer:
            self.text_stream_buffer[agent_name] = ""
        self.text_stream_buffer[agent_name] += chunk
        if voice:
            self.voice = voice

    def flush_stream(self, agent_name: str = "default", voice: str = None):
        """Pushes any remaining text in the buffer to the generator."""
        if not isinstance(self.text_stream_buffer, dict):
            self.text_stream_buffer = {}
        buffer = self.text_stream_buffer.get(agent_name, "")
        voice_to_use = voice or self.voice
        
        if buffer.strip() and not self.stop_event.is_set():
            to_speak = clean_markdown_for_speech(buffer.strip())
            if to_speak:
                self.generator_queue.put((to_speak, voice_to_use))
        self.text_stream_buffer[agent_name] = ""

    def speak(self, text: str, voice=None):
        """Instant speaking of whole blocks."""
        self.start_stream(voice)
        self.stream_text(text)
        self.flush_stream()

# --- STT MANAGER ---

class STTManager:
    def __init__(self, callback, log_callback=None, tts_manager=None):
        self.callback = callback
        self.log_callback = log_callback
        self.tts_manager = tts_manager
        self.is_running = False
        self.mode = None
        self.thread = None
        self.audio_queue = queue.Queue()
        self.trigger_recognizer = None
        self.whisper_recognizer = None
        self.SAMPLE_RATE = 16000
        self.CHUNK_DURATION = 0.1
        self.pending_prefix = ""
        self.active_agent_map = {} # <-- Pre-compiled thread-safe cache
        self.test_mode = False     # <-- Sandboxed mic test-mode flag
        self.reload_config()

    def reload_config(self):
        config = load_audio_config()
        self.start_words = [w.strip().upper() for w in config.get("stt_start_words", "").split(",") if w.strip()]
        self.stop_words = [w.strip().upper() for w in config.get("stt_stop_words", "").split(",") if w.strip()]
        self.send_words = [w.strip().upper() for w in config.get("stt_send_words", "").split(",") if w.strip()]
        self.delete_words = [w.strip().upper() for w in config.get("stt_delete_words", "").split(",") if w.strip()]
        self.agent_hotword = config.get("stt_agent_hotword", "ATTENTION").strip().upper()
        
        try:
            self.energy_threshold = float(config.get("stt_energy_threshold", 0.005))
        except (ValueError, TypeError):
            self.energy_threshold = 0.005
            
        try:
            self.silence_timeout = float(config.get("stt_silence_timeout", 1.2))
        except (ValueError, TypeError):
            self.silence_timeout = 1.2
            
        try:
            self.pre_roll = float(config.get("stt_pre_roll", 0.5))
        except (ValueError, TypeError):
            self.pre_roll = 0.5
            
        try:
            self.min_speech_duration = float(config.get("stt_min_speech_duration", 0.4))
        except (ValueError, TypeError):
            self.min_speech_duration = 0.4

        try:
            self.num_threads = int(config.get("stt_num_threads", 2))
        except (ValueError, TypeError):
            self.num_threads = 2

        try:
            self.feature_dim = int(config.get("stt_feature_dim", 80))
        except (ValueError, TypeError):
            self.feature_dim = 80

        self.decoding_method = str(config.get("stt_decoding_method", "modified_beam_search")).strip()

        try:
            self.max_active_paths = int(config.get("stt_max_active_paths", 14))
        except (ValueError, TypeError):
            self.max_active_paths = 14
            
        self.device = config.get("stt_device", "")
        if self.device == "": self.device = None

    def load_models(self):
        if not sherpa_onnx or not sd:
            raise ImportError("sherpa-onnx or sounddevice is not installed.")
            
        if not self.whisper_recognizer:
            whisper_dir = os.path.join(FEDERATE_DIR, "sherpa-onnx-whisper-tiny.en")
            try:
                _download_file("https://huggingface.co/csukuangfj/sherpa-onnx-whisper-tiny.en/resolve/main/tiny.en-encoder.onnx", os.path.join(whisper_dir, "tiny.en-encoder.onnx"), self.log_callback)
                _download_file("https://huggingface.co/csukuangfj/sherpa-onnx-whisper-tiny.en/resolve/main/tiny.en-decoder.onnx", os.path.join(whisper_dir, "tiny.en-decoder.onnx"), self.log_callback)
                _download_file("https://huggingface.co/csukuangfj/sherpa-onnx-whisper-tiny.en/resolve/main/tiny.en-tokens.txt", os.path.join(whisper_dir, "tiny.en-tokens.txt"), self.log_callback)
            except Exception as e:
                raise RuntimeError(f"Failed to load Whisper STT model files: {e}")
                
            self.whisper_recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=f"{whisper_dir}/tiny.en-encoder.onnx",
                decoder=f"{whisper_dir}/tiny.en-decoder.onnx",
                tokens=f"{whisper_dir}/tiny.en-tokens.txt",
                num_threads=4
            )
            
        if not self.trigger_recognizer and self.mode == "hotword":
            model_dir = os.path.join(FEDERATE_DIR, "sherpa-onnx-streaming-zipformer-en-2023-02-21")
            try:
                _download_file("https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-02-21/resolve/main/tokens.txt", os.path.join(model_dir, "tokens.txt"), self.log_callback)
                _download_file("https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-02-21/resolve/main/encoder-epoch-99-avg-1.int8.onnx", os.path.join(model_dir, "encoder-epoch-99-avg-1.int8.onnx"), self.log_callback)
                _download_file("https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-02-21/resolve/main/decoder-epoch-99-avg-1.int8.onnx", os.path.join(model_dir, "decoder-epoch-99-avg-1.int8.onnx"), self.log_callback)
                _download_file("https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-02-21/resolve/main/joiner-epoch-99-avg-1.int8.onnx", os.path.join(model_dir, "joiner-epoch-99-avg-1.int8.onnx"), self.log_callback)
            except Exception as e:
                raise RuntimeError(f"Failed to load streaming hotword transducer model files: {e}")
                
            self.trigger_recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=f"{model_dir}/tokens.txt",
                encoder=f"{model_dir}/encoder-epoch-99-avg-1.int8.onnx",
                decoder=f"{model_dir}/decoder-epoch-99-avg-1.int8.onnx",
                joiner=f"{model_dir}/joiner-epoch-99-avg-1.int8.onnx",
                num_threads=getattr(self, "num_threads", 2),
                sample_rate=self.SAMPLE_RATE,
                feature_dim=getattr(self, "feature_dim", 80),
                decoding_method=getattr(self, "decoding_method", "modified_beam_search"),
                max_active_paths=getattr(self, "max_active_paths", 14)
            )

    def start_hotword(self):
        if self.mode == "hotword": return True
        self.stop()
        
        self.reload_config()
        agent_hotword = getattr(self, "agent_hotword", "ATTENTION") or "ATTENTION"
        
        # Compile uppercase triggers mapped to case-preserved prefixes dynamically using your hotword
        agent_map = {f"{agent_hotword} TEAM": "@team ", f"{agent_hotword} ROOM": "@room "}
        try:
            from toolbox import CURRENT_APP
            if CURRENT_APP:
                agent_view = CURRENT_APP.query_one("#ai_agent_view")
                for original_name in agent_view.agent_manager.agents.keys():
                    agent_map[f"{agent_hotword} {original_name.upper()}"] = f"@{original_name} "
        except Exception:
            pass

        try:
            self.mode = "hotword"
            self.is_running = True
            self.audio_queue = queue.Queue()
            self.active_agent_map = agent_map # Store on instance
            self.thread = threading.Thread(target=self._hotword_loop, daemon=True)
            self.thread.start()
            return True
        except Exception as e:
            self.mode = None
            if self.log_callback: self.log_callback(f"[bold red]STT Error:[/bold red] {e}")
            return False

    def start_smart_mic(self):
        if self.mode == "smart": return True
        self.stop()
        try:
            self.mode = "smart"
            self.is_running = True
            self.audio_queue = queue.Queue()
            self.thread = threading.Thread(target=self._smart_mic_loop, daemon=True)
            self.thread.start()
            return True
        except Exception as e:
            self.mode = None
            if self.log_callback: self.log_callback(f"[bold red]STT Error:[/bold red] {e}")
            return False

    def stop(self):
        self.is_running = False
        self.mode = None
        self.pending_prefix = ""
        self.active_agent_map = {}

    def _flush_whisper(self, audio_buffer, words_to_strip):
        """Helper to transcribe the buffer and strip trigger words."""
        if not audio_buffer: return ""
        audio_data = np.concatenate(audio_buffer).flatten()
        w_stream = self.whisper_recognizer.create_stream()
        w_stream.accept_waveform(self.SAMPLE_RATE, audio_data)
        self.whisper_recognizer.decode_stream(w_stream)
        
        text = w_stream.result.text.strip()
        for word in words_to_strip:
            text = re.sub(rf'\b{word}\b', '', text, flags=re.IGNORECASE)
        return text.strip(" .,")

    def _hotword_loop(self):
        def dbg(msg):
            if getattr(self, "test_mode", False) and self.log_callback:
                self.log_callback(f"[bold magenta][DBG-LOOP][/bold magenta] {msg}")

        dbg("THREAD STARTED: entering _hotword_loop.")
        try:
            dbg("Entering load_models() block...")
            self.load_models()
            dbg("load_models() block exited cleanly.")
            if self.log_callback:
                self.log_callback("[bold green] STT Engine Loaded & Active. Say your trigger word or 'Attention <agent>' to begin...[/bold green]")
        except Exception as e:
            import traceback
            dbg(f"FATAL EXCEPTION in load_models(): {e}")
            dbg(traceback.format_exc())
            if self.log_callback:
                self.log_callback(f"[bold red]STT Initialization failed:[/bold red] {e}")
            self.is_running = False
            self.mode = None
            return

        is_recording = False
        audio_buffer = []
        
        dbg("Initializing streaming transducer state...")
        try:
            trigger_stream = self.trigger_recognizer.create_stream()
            dbg("Streaming transducer stream successfully created.")
        except Exception as se:
            dbg(f"FATAL EXCEPTION in trigger_recognizer.create_stream(): {se}")
            self.is_running = False
            self.mode = None
            return

        chunk_count = 0
        def audio_callback(indata, frames, time, status):
            nonlocal chunk_count
            if self.is_running: 
                self.audio_queue.put(indata.copy())
                if getattr(self, "test_mode", False):
                    chunk_count += 1
                    if chunk_count % 500 == 0:
                        dbg(f"Callback status check: buffered {chunk_count} raw chunks so far.")

        dbg("Initializing sounddevice.InputStream configuration...")
        with AUDIO_LOCK:
            try:
                # Set blocksize explicitly to 1600 (100ms) to prevent audio callback flooding and high CPU load
                input_stream = sd.InputStream(
                    samplerate=self.SAMPLE_RATE, 
                    channels=1, 
                    dtype="float32", 
                    blocksize=int(self.SAMPLE_RATE * self.CHUNK_DURATION),
                    device=self.device, 
                    callback=audio_callback
                )
                dbg("InputStream successfully configured.")
            except Exception as ie:
                dbg(f"FATAL EXCEPTION in sd.InputStream configuration: {ie}")
                self.is_running = False
                self.mode = None
                return

        # Track decay timestamps
        last_speech_time = time.time()
        last_partial_text = ""

        try:
            dbg("Starting sounddevice.InputStream context manager...")
            with input_stream:
                dbg("InputStream context entered. Recording is now active on hardware level.")
                while self.is_running:
                    try: 
                        chunk = self.audio_queue.get(timeout=0.5)
                    except queue.Empty: 
                        continue
                        
                    # Waveform processing
                    try:
                        trigger_stream.accept_waveform(self.SAMPLE_RATE, chunk.flatten())
                        while self.trigger_recognizer.is_ready(trigger_stream):
                            self.trigger_recognizer.decode_stream(trigger_stream)
                        partial_text = self.trigger_recognizer.get_result(trigger_stream).upper()
                    except Exception as pe:
                        dbg(f"Exception during transducer decoding: {pe}")
                        partial_text = ""

                    # --- TIME-BASED DECAY / RESET GUARDRAILS ---
                    current_time = time.time()
                    if partial_text != last_partial_text:
                        last_speech_time = current_time
                        last_partial_text = partial_text
                    elif partial_text:
                        # Text is non-empty, but has remained unchanged. Check stale duration
                        silence_duration = current_time - last_speech_time
                        if silence_duration > 2.0:
                            dbg(f"Stale text '{partial_text}' detected for {silence_duration:.1f}s. Recreating stream.")
                            self.trigger_recognizer.reset(trigger_stream)
                            trigger_stream = self.trigger_recognizer.create_stream()
                            partial_text = ""
                            last_partial_text = ""
                            last_speech_time = current_time

                    if partial_text.strip():
                        dbg(f"Live Transcribed Text: '{partial_text}'")

                    # =========================================================================
                    #  CONTROL WORD EVALUATION (Runs in BOTH recording and non-recording states)
                    # =========================================================================
                    
                    # 1. Delete Keyword (Removes last appended statement from UI, stops recording)
                    if any(word in partial_text for word in self.delete_words):
                        dbg("Control Match: Delete keyword. Clearing buffer.")
                        audio_buffer = []
                        is_recording = False
                        self.callback("", action="delete")
                        if self.log_callback: self.log_callback("[dim red] Last sentence deleted.[/dim red]")
                        self.trigger_recognizer.reset(trigger_stream)
                        trigger_stream = self.trigger_recognizer.create_stream()
                        partial_text = ""
                        last_partial_text = ""
                        last_speech_time = current_time
                        continue

                    # 2. Stop/Pause Keyword (Ends active dictation session, flushes to box)
                    elif any(word in partial_text for word in self.stop_words):
                        dbg("Control Match: Stop keyword. Pausing dictation.")
                        is_recording = False
                        text = self._flush_whisper(audio_buffer, self.stop_words)
                        if text: 
                            final_text = self.pending_prefix + text
                            self.callback(final_text, action="append")
                        self.pending_prefix = "" # Clear prefix
                        audio_buffer = []
                        if self.log_callback: self.log_callback("[dim yellow] Dictation paused. Review your input.[/dim yellow]")
                        self.trigger_recognizer.reset(trigger_stream)
                        trigger_stream = self.trigger_recognizer.create_stream()
                        partial_text = ""
                        last_partial_text = ""
                        last_speech_time = current_time
                        continue

                    # 3. Send/Execute Keyword (Instantly fires the final prompt from the text box)
                    elif any(word in partial_text for word in self.send_words):
                        dbg("Control Match: Send keyword. Submitting prompt.")
                        is_recording = False
                        text = self._flush_whisper(audio_buffer, self.send_words)
                        final_text = self.pending_prefix + text
                        self.callback(final_text, action="submit")
                        self.pending_prefix = "" # Clear prefix
                        audio_buffer = []
                        self.trigger_recognizer.reset(trigger_stream)
                        trigger_stream = self.trigger_recognizer.create_stream()
                        partial_text = ""
                        last_partial_text = ""
                        last_speech_time = current_time
                        continue

                    # =========================================================================
                    #  STANDARD DICTATION & TRIGGER EVALUATIONS
                    # =========================================================================
                    if not is_recording:
                        # Thread-safe contiguous-only lookup from our pre-compiled active_agent_map
                        target_prefix = ""
                        for trigger_phrase, prefix in getattr(self, "active_agent_map", {}).items():
                            # Strictly contiguous check (e.g. "ATTENTION RITA" must be contiguous)
                            if trigger_phrase in partial_text:
                                target_prefix = prefix
                                dbg(f"CONTIGUOUS ATTENTION TRIGGER MATCHED: {target_prefix}")
                                break

                        # Mutual Exclusion: Standard trigger check is strictly bypassed if an Agent target has matched
                        trigger_matched = False
                        if not target_prefix:
                            trigger_matched = any(word in partial_text for word in self.start_words)
                            if trigger_matched:
                                dbg(f"STANDARD TRIGGER MATCHED: start_words={self.start_words}")

                        # Clean, unblocked evaluation
                        if target_prefix or trigger_matched:
                            dbg("TRIGGER CONDITIONS MET. Transitioning to recording state...")
                            is_recording = True
                            self.pending_prefix = target_prefix # Save the matched prefix
                            if self.tts_manager: 
                                dbg("Stopping any active TTS playback...")
                                self.tts_manager.stop_all_audio()
                            
                            if target_prefix:
                                log_msg = f"[dim green] {target_prefix.strip()} target detected. Dictation started...[/dim green]"
                            else:
                                log_msg = "[dim green] Trigger word detected. Dictation started...[/dim green]"
                            
                            if self.log_callback: self.log_callback(log_msg)
                            self.trigger_recognizer.reset(trigger_stream)
                            trigger_stream = self.trigger_recognizer.create_stream() # Wipes buffer clean
                    else:
                        audio_buffer.append(chunk)
                        if getattr(self, "test_mode", False) and len(audio_buffer) % 10 == 0:
                            dbg(f"Dictation Active: Buffered {len(audio_buffer)} chunks of active speech.")

                        # 4. Natural Pause (Appends to input box, keeps listening)
                        if self.trigger_recognizer.is_endpoint(trigger_stream):
                            dbg("Transducer endpoint (natural pause) detected.")
                            text = self._flush_whisper(audio_buffer, [])
                            dbg(f"Endpoint raw Whisper output: '{text}'")
                            if text: 
                                final_text = self.pending_prefix + text
                                dbg(f"Dispatching endpoint text: '{final_text}'")
                                self.callback(final_text, action="append")
                                self.pending_prefix = "" 
                            audio_buffer = []
                            self.trigger_recognizer.reset(trigger_stream)
                            trigger_stream = self.trigger_recognizer.create_stream()

        except Exception as e:
            import traceback
            dbg(f"FATAL EXCEPTION inside input_stream context: {e}")
            dbg(traceback.format_exc())
            self.is_running = False
            self.mode = None
            self.pending_prefix = ""

    def _smart_mic_loop(self):
        if self.log_callback:
            self.log_callback("[dim yellow] Initializing Smart Mic Models in background...[/dim yellow]")
        try:
            self.load_models()
            if self.log_callback:
                self.log_callback("[bold green] Smart Mic Loaded & Active. Speak naturally...[/bold green]")
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"[bold red]STT Initialization failed:[/bold red] {e}")
            self.is_running = False
            self.mode = None
            return

        chunk_samples = int(self.SAMPLE_RATE * self.CHUNK_DURATION)
        silence_chunks_limit = int(self.silence_timeout / self.CHUNK_DURATION)
        preroll_chunks_limit = int(self.pre_roll / self.CHUNK_DURATION)
        
        audio_buffer = []
        preroll_buffer = collections.deque(maxlen=preroll_chunks_limit)
        
        is_speaking = False
        silence_counter = 0

        def audio_callback(indata, frames, time, status):
            if self.is_running: self.audio_queue.put(indata.copy())

        try:
            with sd.InputStream(samplerate=self.SAMPLE_RATE, channels=1, dtype="float32", blocksize=chunk_samples, device=self.device, callback=audio_callback):
                while self.is_running:
                    try: chunk = self.audio_queue.get(timeout=0.5)
                    except queue.Empty: continue
                        
                    rms = np.sqrt(np.mean(np.square(chunk)))
                    
                    if rms > self.energy_threshold:
                        if not is_speaking:
                            is_speaking = True
                            if self.tts_manager: self.tts_manager.stop_all_audio()
                            if self.log_callback: self.log_callback("[dim green] (Voice Detected)[/dim green]")
                            audio_buffer = list(preroll_buffer)
                        audio_buffer.append(chunk)
                        silence_counter = 0
                    else:
                        if is_speaking:
                            audio_buffer.append(chunk)
                            silence_counter += 1
                            if silence_counter >= silence_chunks_limit:
                                is_speaking = False
                                
                                full_audio = np.concatenate(audio_buffer).flatten()
                                audio_buffer = []
                                silence_counter = 0
                                
                                if len(full_audio) > (self.SAMPLE_RATE * self.min_speech_duration):
                                    text = self._flush_whisper([full_audio], [])
                                    if text:
                                        self.callback(text, action="append") # Append to UI so you can edit
                        else:
                            preroll_buffer.append(chunk)
        except Exception as e:
            self.is_running = False
            self.mode = None
            self.pending_prefix = ""