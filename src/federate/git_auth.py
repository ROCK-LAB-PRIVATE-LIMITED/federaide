"""
    FEDERaiDE is a multi-agent multi-modal automation and orchestration harness.
    Copyright (C) 2026  ROCK LAB PRIVATE LIMITED

    Single-Use Credential Broker for Git Authentication with Ghost Honeypot Tripwire.
    Fully compatible with Windows, Termux (Android), macOS, and Linux.
"""

import contextlib
import hmac
import os
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading

try:
    from textual.screen import ModalScreen
    from textual.app import ComposeResult
    from textual.containers import Vertical, Horizontal
    from textual.widgets import Label, Button
    from textual import on
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


if HAS_TEXTUAL:
    class PatCompromisedModal(ModalScreen[None]):
        DEFAULT_CSS = """
        PatCompromisedModal { 
            align: center middle; 
            background: $background 80%; 
        }
        #pat_alert_dialog { 
            width: 76; 
            height: auto; 
            border: thick $error; 
            background: $surface; 
            padding: 1 2; 
        }
        .alert_title { 
            background: $error; 
            color: $text; 
            text-style: bold; 
            text-align: center; 
            width: 100%; 
            height: 1;
            margin-bottom: 1;
        }
        .alert_msg { 
            margin: 1 0; 
            text-wrap: wrap; 
            height: auto; 
            width: 100%;
            color: $text; 
        }
        .alert_critical {
            color: $error;
            text-style: bold;
            margin-top: 1;
            margin-bottom: 1;
            text-align: center;
            text-wrap: wrap;
            height: auto;
            width: 100%;
        }
        .buttons { 
            height: auto; 
            align: right middle; 
            margin-top: 1; 
        }
        .buttons Button { 
            margin-left: 1; 
        }
        """

        def compose(self) -> ComposeResult:
            with Vertical(id="pat_alert_dialog"):
                yield Label(" SECURITY ALERT: GIT PAT INTERCEPTED", classes="alert_title")
                yield Label(
                    "An unauthorized connection or timing attack attempted to harvest your "
                    "ephemeral Git credentials from the local host.\n\n"
                    "• The credential broker immediately burned and erased the token from memory.\n"
                    "• Further credential delivery was disabled to protect your account.\n"
                    "• Because an unauthorized process probed the authentication endpoint, "
                    "your Personal Access Token (PAT) may have been compromised.",
                    classes="alert_msg"
                )
                yield Label(
                    "ACTION REQUIRED: ROTATE / REVOKE YOUR GIT PAT IMMEDIATELY ON GITHUB/GITLAB!",
                    classes="alert_critical"
                )
                with Horizontal(classes="buttons"):
                    yield Button("I Understand / Dismiss", id="btn_ack_pat_alert", variant="error")

        @on(Button.Pressed, "#btn_ack_pat_alert")
        def on_ack(self):
            self.dismiss()


def trigger_pat_compromised_alert():
    """Prints a console alarm and pops an immediate modal dialog in the Textual TUI."""
    msg = (
        "\n\033[1;31m[🚨 SECURITY ALERT] Credential interception or unauthorized probe detected during Git operation!\033[0m\n"
        "The single-use token broker was probed by an unauthorized connection.\n"
        "The token was immediately burned and further credential delivery disabled.\n"
        "\033[1;31mACTION REQUIRED: YOUR GIT PERSONAL ACCESS TOKEN (PAT) MAY BE COMPROMISED.\n"
        "PLEASE ROTATE / REVOKE IT IMMEDIATELY ON GITHUB/GITLAB!\033[0m\n\n"
    )
    sys.stderr.write(msg)
    sys.stderr.flush()

    try:
        import toolbox
        if getattr(toolbox, "CURRENT_APP", None):
            app = toolbox.CURRENT_APP
            def _show_ui_alert():
                try:
                    app.notify(
                        "CRITICAL SECURITY ALERT: Git PAT intercepted or probed! Rotate token now!",
                        title="PAT Security Warning",
                        severity="error",
                        timeout=15.0
                    )
                except Exception:
                    pass
                try:
                    if hasattr(toolbox, "log_tool"):
                        toolbox.log_tool("[bold red]🚨 CRITICAL SECURITY ALERT: Git PAT was probed or intercepted! Rotate your token immediately![/bold red]")
                except Exception:
                    pass
                if HAS_TEXTUAL:
                    try:
                        app.push_screen(PatCompromisedModal())
                    except Exception:
                        pass

            app.call_from_thread(_show_ui_alert)
    except Exception:
        pass


def _recv_exact(sock, size):
    chunks = []
    remaining = size

    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Credential broker connection closed.")
        chunks.append(chunk)
        remaining -= len(chunk)

    return b"".join(chunks)


@contextlib.contextmanager
def git_askpass_context(pat: str):
    """
    Supply a Git HTTPS password through a single-use credential broker
    equipped with an active post-claim Honeypot and Pre-emption Tripwire.
    """
    env = os.environ.copy()
    env.pop("FEDERAIDE_GIT_PASSWORD", None)
    env["GIT_TERMINAL_PROMPT"] = "0"

    if not pat:
        env.pop("GIT_ASKPASS", None)
        yield env
        return

    secret = bytearray(pat.encode("utf-8"))
    capability = secrets.token_bytes(32)

    temp_dir = tempfile.mkdtemp(prefix="federaide-git-auth-")
    if os.name != "nt":
        os.chmod(temp_dir, 0o700)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    listener.settimeout(0.25)

    port = listener.getsockname()[1]
    stopping = threading.Event()
    token_burned = threading.Event()
    honeypot_triggered = threading.Event()
    active_conn = [None]
    active_conn_lock = threading.Lock()

    def broker():
        nonlocal secret
        while not stopping.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            # Close the handoff window: if cleanup started during accept(), abort immediately
            if stopping.is_set():
                try:
                    conn.close()
                except OSError:
                    pass
                break

            with active_conn_lock:
                active_conn[0] = conn

            with conn:
                try:
                    conn.settimeout(3.0)
                    supplied = _recv_exact(conn, 32)
                    if not hmac.compare_digest(supplied, capability):
                        continue

                    # PHASE 1: Initial claim
                    if not token_burned.is_set():
                        token_burned.set()  # Mark as burned BEFORE network I/O
                        try:
                            conn.sendall(struct.pack("!I", len(secret)))
                            conn.sendall(secret)
                        finally:
                            # Single-use burn: erase secret buffer unconditionally
                            for i in range(len(secret)):
                                secret[i] = 0
                            secret.clear()
                        # Do not break here; transition into Phase 2 (Honeypot Trap)

                    # PHASE 2: Post-claim Honeypot Trap
                    # Legitimate askpass checks CLAIMED_FILE locally and NEVER connects twice.
                    # Any secondary connection querying this socket is an outside attacker!
                    else:
                        honeypot_triggered.set()
                        stopping.set()
                        break

                except (OSError, ConnectionError):
                    continue
                finally:
                    with active_conn_lock:
                        active_conn[0] = None

    broker_thread = threading.Thread(
        target=broker,
        name="federaide-git-credential-broker",
        daemon=True,
    )
    broker_thread.start()

    claimed_file = os.path.join(temp_dir, "claimed.ok").replace("\\", "/")
    alarm_file = os.path.join(temp_dir, "intercepted.alarm").replace("\\", "/")

    helper_py = os.path.join(temp_dir, "askpass.py")
    helper_command = os.path.join(
        temp_dir,
        "askpass.cmd" if os.name == "nt" else "askpass.sh",
    )

    helper_source = f'''import socket
import struct
import sys
import os

HOST = "127.0.0.1"
PORT = {port}
CAPABILITY = bytes.fromhex("{capability.hex()}")
CLAIMED_FILE = "{claimed_file}"
ALARM_FILE = "{alarm_file}"

prompt = " ".join(sys.argv[1:]).lower()
if "password" not in prompt:
    sys.exit(1)

# DEFENSE: If Git re-prompts for credentials on the same run, exit immediately
# without opening a socket so we never trip the post-claim honeypot.
if os.path.exists(CLAIMED_FILE):
    sys.exit(1)

def recv_exact(sock, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Broker disconnected")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

try:
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        sock.sendall(CAPABILITY)
        size = struct.unpack("!I", recv_exact(sock, 4))[0]

        if size > 1024 * 1024:
            sys.exit(1)

        credential = recv_exact(sock, size)

    # Legitimate claim completed: mark file before returning password to Git
    try:
        with open(CLAIMED_FILE, "w") as f:
            f.write("OK")
    except Exception:
        pass

    sys.stdout.write(credential.decode("utf-8") + "\\n")

except (ConnectionRefusedError, ConnectionResetError, ConnectionError):
    # Pre-emption Tripwire: Broker was already dead or burned before Git could claim it
    try:
        with open(ALARM_FILE, "w") as f:
            f.write("INTERCEPTED")
    except Exception:
        pass
    sys.exit(1)
except Exception:
    sys.exit(1)
'''

    try:
        with open(helper_py, "w", encoding="utf-8") as f:
            f.write(helper_source)

        if os.name == "nt":
            python_command = subprocess.list2cmdline([sys.executable, helper_py])
            with open(helper_command, "w", encoding="utf-8") as f:
                f.write("@echo off\r\n")
                f.write(f"{python_command} %*\r\n")
        else:
            with open(helper_command, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\n")
                f.write(
                    f"exec {shlex.quote(sys.executable)} "
                    f"{shlex.quote(helper_py)} \"$@\"\n"
                )
            os.chmod(helper_py, 0o600)
            os.chmod(helper_command, 0o700)

        env["GIT_ASKPASS"] = helper_command

        yield env

    finally:
        stopping.set()
        
        # 1. Stop accepting new connections
        try:
            listener.close()
        except OSError:
            pass
            
        # 2. Forcibly sever any active connection to unblock the broker thread
        with active_conn_lock:
            conn_to_kill = active_conn[0]
            if conn_to_kill:
                try:
                    conn_to_kill.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    conn_to_kill.close()
                except OSError:
                    pass

        # 3. Wait for the broker thread to cleanly exit
        broker_thread.join(timeout=4.0)

        # 4. Safely zero the buffer only if the broker is confirmed dead
        if broker_thread.is_alive():
            # Do not concurrently mutate secret while broker may still use it!
            # Treat this as a broker shutdown failure and trip the alarm.
            trigger_pat_compromised_alert()
        else:
            for i in range(len(secret)):
                secret[i] = 0
            secret.clear()

        legit_claimed = os.path.exists(claimed_file)
        alarm_flagged = os.path.exists(alarm_file)
        was_burned = token_burned.is_set()
        honeypot_hit = honeypot_triggered.is_set()

        shutil.rmtree(temp_dir, ignore_errors=True)

        # TRIPWIRE LOGIC:
        # 1. Honeypot hit: Secondary connection arrived on the socket after first claim.
        # 2. Alarm flagged: askpass.py encountered a dead broker while trying to authenticate.
        # 3. Orphan claim: Token was burned on the broker, but legitimate helper never completed.
        if honeypot_hit or alarm_flagged or (was_burned and not legit_claimed):
            trigger_pat_compromised_alert()