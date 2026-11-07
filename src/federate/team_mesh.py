"""
Team-to-Team Asymmetric Mesh Protocol for FEDERaiDE via Dedicated Git Relay.
Closed-Channel Pre-Shared Key (PSK) Relay Protocol with ChaCha20-Poly1305 AEAD and Ed25519 Signatures, and 3-tick ACK auto-purge.
"""
import os
import json
import time
import zipfile
import io
import subprocess
import tempfile
import stat
import contextlib
from pathlib import Path
from typing import List, Tuple, Dict

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
from Crypto.Cipher import ChaCha20_Poly1305
from Crypto.Protocol.KDF import HKDF
from Crypto.Hash import SHA256

import keyring
import toolbox
import threading
import re

KEYRING_SERVICE = "Federate"
SEED_KEY = "federation_user_seed"
RELAY_USER_KEY = "mesh_relay_user"
RELAY_PAT_KEY = "mesh_relay_pat"
_RELAY_LOCK = threading.RLock()

def _get_team_tag(team_dir: str = None) -> str:
    t_dir = team_dir or toolbox.get_team_dir()
    return re.sub(r'[^a-zA-Z0-9_]', '_', os.path.basename(t_dir)).lower().lstrip("_") or "default"

def get_relay_local_dir(team_dir: str = None) -> str:
    tag = _get_team_tag(team_dir)
    return os.path.join(toolbox.FEDERAIDE_SYS_DIR, "mesh_relays", tag)

# --- CREDENTIAL HELPERS (Scoped per team/channel) ---

def get_or_create_user_seed(team_dir: str = None) -> str:
    tag = _get_team_tag(team_dir)
    key = f"mesh_seed_{tag}"
    seed = keyring.get_password(KEYRING_SERVICE, key)
    if not seed:
        seed = keyring.get_password(KEYRING_SERVICE, SEED_KEY)
    if not seed:
        import secrets
        seed = secrets.token_hex(32)
        keyring.set_password(KEYRING_SERVICE, key, seed)
    return seed

def set_user_seed(seed_phrase: str, team_dir: str = None):
    tag = _get_team_tag(team_dir)
    key = f"mesh_seed_{tag}"
    keyring.set_password(KEYRING_SERVICE, key, seed_phrase.strip())

def get_relay_credentials(team_dir: str = None) -> Tuple[str, str, str]:
    t_dir = team_dir or toolbox.get_team_dir()
    tag = _get_team_tag(t_dir)
    team_settings = toolbox.load_team_settings() if t_dir == toolbox.get_team_dir() else {}
    if not team_settings and os.path.exists(os.path.join(t_dir, "team_settings.json")):
        try:
            with open(os.path.join(t_dir, "team_settings.json"), "r", encoding="utf-8") as f:
                team_settings = json.load(f)
        except Exception:
            team_settings = {}
    url = team_settings.get("mesh_relay_url", "").strip()
    if not url:
        g_settings = toolbox.load_global_settings()
        url = g_settings.get("mesh_relay_url", "").strip()
    user = keyring.get_password(KEYRING_SERVICE, f"mesh_relay_user_{tag}") or keyring.get_password(KEYRING_SERVICE, RELAY_USER_KEY) or ""
    pat = keyring.get_password(KEYRING_SERVICE, f"mesh_relay_pat_{tag}") or keyring.get_password(KEYRING_SERVICE, RELAY_PAT_KEY) or ""
    return url, user, pat

def save_relay_credentials(url: str, user: str, pat: str, team_dir: str = None):
    t_dir = team_dir or toolbox.get_team_dir()
    tag = _get_team_tag(t_dir)
    team_settings = toolbox.load_team_settings() if t_dir == toolbox.get_team_dir() else {}
    if not team_settings and os.path.exists(os.path.join(t_dir, "team_settings.json")):
        try:
            with open(os.path.join(t_dir, "team_settings.json"), "r", encoding="utf-8") as f:
                team_settings = json.load(f)
        except Exception:
            team_settings = {}
    team_settings["mesh_relay_url"] = url.strip()
    if t_dir == toolbox.get_team_dir():
        toolbox.save_team_settings(team_settings)
    else:
        try:
            with open(os.path.join(t_dir, "team_settings.json"), "w", encoding="utf-8") as f:
                json.dump(team_settings, f, indent=4)
        except Exception:
            pass

    if user.strip():
        keyring.set_password(KEYRING_SERVICE, f"mesh_relay_user_{tag}", user.strip())
    if pat.strip():
        keyring.set_password(KEYRING_SERVICE, f"mesh_relay_pat_{tag}", pat.strip())

# --- DETERMINISTIC ASYMMETRIC KEY DERIVATION ---

def derive_keys(seed: str):
    seed_bytes = seed.encode("utf-8")
    salt = b"FEDERaiDE-Team-Mesh-Salt-v1"
    key_material = HKDF(seed_bytes, 64, salt, SHA256, context=b"team-keys")
    sign_seed = io.BytesIO(key_material[:32])
    enc_seed = io.BytesIO(key_material[32:])

    sign_key = ECC.generate(curve="Ed25519", randfunc=sign_seed.read)
    enc_key = ECC.generate(curve="Ed25519", randfunc=enc_seed.read)
    return sign_key, enc_key

# --- RELAY GIT RUNNER ---

from git_auth import git_askpass_context

def sync_relay_repo(team_dir: str = None) -> bool:
    with _RELAY_LOCK:
        url, user, pat = get_relay_credentials(team_dir)
        if not url or not pat:
            return False

        relay_dir = get_relay_local_dir(team_dir)
        from urllib.parse import urlparse, urlunparse, quote
        parsed = urlparse(url)
        user_part = quote(user.strip(), safe='') if user else "git"
        host = parsed.netloc.split("@")[-1]
        auth_netloc = f"{user_part}@{host}"
        safe_url = urlunparse((parsed.scheme, auth_netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

        with git_askpass_context(pat) as env:
            try:
                git_dir = os.path.join(relay_dir, ".git")
                if not os.path.exists(git_dir):
                    if os.path.exists(relay_dir):
                        import shutil
                        shutil.rmtree(relay_dir, ignore_errors=True)
                    os.makedirs(relay_dir, exist_ok=True)
                    res = subprocess.run(["git", "-c", "credential.helper=", "clone", safe_url, relay_dir], capture_output=True, text=True, env=env, timeout=45)
                    if res.returncode != 0:
                        return False
                    subprocess.run(["git", "remote", "set-url", "origin", url], cwd=relay_dir, capture_output=True)
                    subprocess.run(["git", "config", "user.name", "FEDERaiDE-Mesh"], cwd=relay_dir, capture_output=True, timeout=10)
                    subprocess.run(["git", "config", "user.email", "mesh@local"], cwd=relay_dir, capture_output=True, timeout=10)
                else:
                    # Stage and commit any local processed/deleted message files first so git merge never aborts on a dirty worktree
                    subprocess.run(["git", "add", "-A"], cwd=relay_dir, capture_output=True, timeout=10)
                    st = subprocess.run(["git", "status", "--porcelain"], cwd=relay_dir, capture_output=True, text=True, timeout=10)
                    if st.stdout.strip():
                        subprocess.run(["git", "-c", "user.name=FEDERaiDE-Mesh", "-c", "user.email=mesh@local", "commit", "-m", "Local relay state commit"], cwd=relay_dir, capture_output=True, timeout=10)

                    branch_res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=relay_dir, capture_output=True, text=True)
                    branch = branch_res.stdout.strip() if branch_res.returncode == 0 and branch_res.stdout.strip() and branch_res.stdout.strip() != "HEAD" else "main"

                    subprocess.run(["git", "-c", "credential.helper=", "fetch", safe_url, branch], cwd=relay_dir, capture_output=True, env=env, timeout=30)
                    fetch_head = os.path.join(relay_dir, ".git", "FETCH_HEAD")
                    if os.path.exists(fetch_head) and os.path.getsize(fetch_head) > 0:
                        subprocess.run(["git", "-c", "user.name=FEDERaiDE-Mesh", "-c", "user.email=mesh@local", "-c", "credential.helper=", "merge", "--no-edit", "-X", "theirs", "FETCH_HEAD"], cwd=relay_dir, capture_output=True, env=env, timeout=15)

                # Auto-ignore .processed files so they never get staged to remote git
                gitignore_path = os.path.join(relay_dir, ".gitignore")
                if not os.path.exists(gitignore_path) or "*.processed" not in open(gitignore_path, "r", encoding="utf-8").read():
                    with open(gitignore_path, "a", encoding="utf-8") as gf:
                        gf.write("\n*.processed\n")
                    subprocess.run(["git", "add", ".gitignore"], cwd=relay_dir, capture_output=True, timeout=10)
                    subprocess.run(["git", "-c", "user.name=FEDERaiDE-Mesh", "-c", "user.email=mesh@local", "commit", "-m", "Ignore processed message files"], cwd=relay_dir, capture_output=True, timeout=10)
                return True
            except (subprocess.TimeoutExpired, Exception):
                return False

def push_relay_repo(team_dir: str = None) -> bool:
    with _RELAY_LOCK:
        url, user, pat = get_relay_credentials(team_dir)
        relay_dir = get_relay_local_dir(team_dir)
        if not url or not pat or not os.path.exists(relay_dir):
            return False
        from urllib.parse import urlparse, urlunparse, quote
        parsed = urlparse(url)
        user_part = quote(user.strip(), safe='') if user else "git"
        host = parsed.netloc.split("@")[-1]
        safe_url = urlunparse((parsed.scheme, f"{user_part}@{host}", parsed.path, parsed.params, parsed.query, parsed.fragment))

        with git_askpass_context(pat) as env:
            try:
                branch_res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=relay_dir, capture_output=True, text=True)
                branch = branch_res.stdout.strip() if branch_res.returncode == 0 and branch_res.stdout.strip() and branch_res.stdout.strip() != "HEAD" else "main"

                subprocess.run(["git", "-c", "credential.helper=", "fetch", safe_url, branch], cwd=relay_dir, capture_output=True, env=env, timeout=30)
                fetch_head = os.path.join(relay_dir, ".git", "FETCH_HEAD")
                if os.path.exists(fetch_head) and os.path.getsize(fetch_head) > 0:
                    subprocess.run(["git", "-c", "user.name=FEDERaiDE-Mesh", "-c", "user.email=mesh@local", "-c", "credential.helper=", "merge", "--no-edit", "-X", "theirs", "FETCH_HEAD"], cwd=relay_dir, capture_output=True, env=env, timeout=15)

                subprocess.run(["git", "add", "."], cwd=relay_dir, capture_output=True, timeout=15)
                status = subprocess.run(["git", "status", "--porcelain"], cwd=relay_dir, capture_output=True, text=True, timeout=10)
                if status.stdout.strip():
                    subprocess.run(["git", "-c", "user.name=FEDERaiDE-Mesh", "-c", "user.email=mesh@local", "commit", "-m", "Mesh State Update"], cwd=relay_dir, capture_output=True, timeout=15)

                branch_res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=relay_dir, capture_output=True, text=True)
                branch = branch_res.stdout.strip() if branch_res.returncode == 0 and branch_res.stdout.strip() and branch_res.stdout.strip() != "HEAD" else "main"

                res = subprocess.run(["git", "-c", "credential.helper=", "push", safe_url, f"HEAD:{branch}"], cwd=relay_dir, capture_output=True, text=True, env=env, timeout=30)
                return res.returncode == 0
            except (subprocess.TimeoutExpired, Exception):
                return False

# --- FLEET DIRECTORY: PUBLISH & DISCOVER ---

def unpublish_team(team_name: str, team_dir: str = None) -> bool:
    """Removes a team's manifest and inbox from the channel relay."""
    with _RELAY_LOCK:
        if not sync_relay_repo(team_dir):
            return False
        relay_dir = get_relay_local_dir(team_dir)
        removed = False
        for sub in ("teams", "messages"):
            p = os.path.join(relay_dir, sub, team_name)
            if os.path.exists(p):
                import shutil
                shutil.rmtree(p, ignore_errors=True)
                subprocess.run(["git", "rm", "-rf", f"{sub}/{team_name}"], cwd=relay_dir, capture_output=True)
                removed = True
        if removed:
            return push_relay_repo(team_dir)
        return True

def publish_team_manifest(my_team: str, readme_content: str, team_dir: str = None, old_team_name: str = None):
    """Publishes team README.md and public key into mesh_relays/<channel>/teams/<team>/."""
    if not sync_relay_repo(team_dir):
        return False

    t_dir = team_dir or toolbox.get_team_dir()
    my_team = my_team or toolbox.get_team_name(t_dir)
    relay_dir = get_relay_local_dir(team_dir)

    # Collect all stale names to clean up from the relay (old name, legacy 'Default', legacy 'federate')
    stale_names = set()
    if old_team_name and old_team_name.lower() != my_team.lower():
        stale_names.add(old_team_name)

    default_workspace = os.path.abspath(os.path.expanduser("~/.federate"))
    if os.path.abspath(t_dir) == default_workspace and my_team.lower() != "default":
        stale_names.add("Default")
        stale_names.add("default")

    folder_base = os.path.basename(t_dir).lstrip(".")
    if folder_base and folder_base.lower() != my_team.lower():
        stale_names.add(folder_base)

    for stale in stale_names:
        for parent_sub in ("teams", "messages"):
            stale_path = os.path.join(relay_dir, parent_sub, stale)
            if os.path.exists(stale_path):
                import shutil
                shutil.rmtree(stale_path, ignore_errors=True)
                subprocess.run(["git", "rm", "-rf", f"{parent_sub}/{stale}"], cwd=relay_dir, capture_output=True)

    relay_team_dir = os.path.join(relay_dir, "teams", my_team)
    os.makedirs(relay_team_dir, exist_ok=True)

    with open(os.path.join(relay_team_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme_content)

    seed = get_or_create_user_seed(team_dir)
    sign_key, _ = derive_keys(seed)
    manifest = {
        "team_name": my_team,
        "pubkey": sign_key.public_key().export_key(format="PEM"),
        "updated_at": time.time()
    }
    with open(os.path.join(relay_team_dir, "identity.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=4)

    return push_relay_repo(team_dir)

def get_peer_team_pubkey(team_name: str, team_dir: str = None) -> str:
    """Retrieves the pre-registered Ed25519 public key for a team from the relay."""
    relay_dir = get_relay_local_dir(team_dir)
    identity_path = os.path.join(relay_dir, "teams", team_name, "identity.json")
    if not os.path.exists(identity_path):
        raise ValueError(f"Sender team '{team_name}' is not registered in the channel relay.")
    with open(identity_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    pubkey = data.get("pubkey")
    if not pubkey:
        raise ValueError(f"No public key registered for team '{team_name}'.")
    return pubkey

def discover_peer_teams(team_dir: str = None) -> Dict[str, str]:
    """Reads all peer team charters from the team's channel relay."""
    relay_dir = get_relay_local_dir(team_dir)
    teams_dir = os.path.join(relay_dir, "teams")
    if not os.path.exists(teams_dir):
        return {}

    t_dir = team_dir or toolbox.get_team_dir()
    my_team = toolbox.get_team_name(t_dir)
    peers = {}
    for t_name in os.listdir(teams_dir):
        if t_name.lower() == my_team.lower():
            continue
        # Filter out superseded default/folder aliases
        if t_name.lower() in ("default", "federate") and any(vt.lower() == "alpha" for vt in os.listdir(teams_dir)):
            continue
        t_path = os.path.join(teams_dir, t_name)
        if os.path.isdir(t_path):
            readme_path = os.path.join(t_path, "README.md")
            charter = ""
            if os.path.exists(readme_path):
                try:
                    with open(readme_path, "r", encoding="utf-8") as f:
                        charter = f.read().strip()
                except Exception:
                    pass
            peers[t_name] = charter or "No charter description provided."
    return peers

def auto_publish_manifest(team_dir: str = None) -> bool:
    """Silently publishes team identity and README to the relay if credentials exist."""
    t_dir = team_dir or toolbox.get_team_dir()
    url, _, pat = get_relay_credentials(t_dir)
    if not url or not pat:
        return False
    my_team = toolbox.get_team_name(t_dir)
    readme_path = os.path.join(t_dir, "README.md")
    readme_text = ""
    if os.path.exists(readme_path):
        try:
            with open(readme_path, "r", encoding="utf-8") as f:
                readme_text = f.read()
        except Exception: pass
    return publish_team_manifest(my_team, readme_text, team_dir=t_dir)

# --- PACKAGING & ENCRYPTION ---

def package_and_encrypt(instruction: str, attachments: List[str], target_team: str, from_team: str, team_dir: str = None) -> bytes:
    seed = get_or_create_user_seed(team_dir)
    sign_key, enc_key = derive_keys(seed)

    zip_buf = io.BytesIO()
    used_arcnames = set()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in (attachments or []):
            try:
                safe_p, disp_p = toolbox.get_safe_path(f)
            except Exception:
                safe_p = os.path.abspath(f)
                disp_p = os.path.basename(f)
            if os.path.exists(safe_p):
                arc = disp_p
                idx = 1
                while arc in used_arcnames:
                    base, ext = os.path.splitext(disp_p)
                    arc = f"{base}_{idx}{ext}"
                    idx += 1
                used_arcnames.add(arc)
                zf.write(safe_p, arcname=arc)
    raw_zip = zip_buf.getvalue()

    manifest = {
        "msg_id": f"msg_{int(time.time()*1000)}",
        "timestamp": time.time(),
        "from_team": from_team,
        "target_team": target_team,
        "instruction": instruction,
        "has_attachments": bool(raw_zip)
    }

    ephemeral_secret = os.urandom(32)
    session_key = HKDF(seed.encode("utf-8") + ephemeral_secret, 32, b"session-salt", SHA256)

    payload = json.dumps(manifest).encode("utf-8") + b"\n---ZIP---\n" + raw_zip
    cipher = ChaCha20_Poly1305.new(key=session_key)
    ciphertext, tag = cipher.encrypt_and_digest(payload)

    header = {
        "version": "1.0",
        "msg_id": manifest["msg_id"],
        "from_team": from_team,
        "target_team": target_team,
        "nonce": cipher.nonce.hex(),
        "tag": tag.hex(),
        "ephemeral": ephemeral_secret.hex(),
        "pubkey": sign_key.public_key().export_key(format="PEM")
    }
    header_bytes = json.dumps(header).encode("utf-8")
    signer = eddsa.new(sign_key, mode="rfc8032")
    sig = signer.sign(header_bytes + ciphertext)

    return header_bytes + b"\n---SIG---\n" + sig + b"\n---DATA---\n" + ciphertext

def unpack_and_decrypt(raw_bundle: bytes, stage_dir: str, team_dir: str = None) -> Tuple[dict, List[str]]:
    seed = get_or_create_user_seed(team_dir)
    parts = raw_bundle.split(b"\n---SIG---\n")
    header_bytes = parts[0]
    sig_and_data = parts[1].split(b"\n---DATA---\n")
    sig = sig_and_data[0]
    ciphertext = sig_and_data[1]

    header = json.loads(header_bytes.decode("utf-8"))
    sender_team = header.get("from_team", "")
    registered_pubkey = get_peer_team_pubkey(sender_team, team_dir=team_dir)
    sender_pub = ECC.import_key(registered_pubkey)

    # 1. Verify Ed25519 signature
    verifier = eddsa.new(sender_pub, mode="rfc8032")
    verifier.verify(header_bytes + ciphertext, sig)

    # 2. Decrypt with derived session key
    ephemeral_secret = bytes.fromhex(header["ephemeral"])
    session_key = HKDF(seed.encode("utf-8") + ephemeral_secret, 32, b"session-salt", SHA256)
    cipher = ChaCha20_Poly1305.new(key=session_key, nonce=bytes.fromhex(header["nonce"]))
    decrypted = cipher.decrypt_and_verify(ciphertext, bytes.fromhex(header["tag"]))

    dec_parts = decrypted.split(b"\n---ZIP---\n")
    manifest = json.loads(dec_parts[0].decode("utf-8"))
    raw_zip = dec_parts[1]

    extracted_files = []
    if raw_zip:
        out_path = os.path.abspath(os.path.join(stage_dir, manifest["msg_id"]))
        os.makedirs(out_path, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
            for member in zf.infolist():
                target = os.path.abspath(os.path.join(out_path, member.filename))
                if os.path.commonpath([out_path, target]) != out_path:
                    continue  # Skip path traversal attempts
                zf.extract(member, out_path)
                if not member.is_dir():
                    extracted_files.append(target)

    return manifest, extracted_files

# --- DISPATCH, ACK & AUTO-PURGE ---

def send_dispatch(target_team: str, instruction: str, file_attachments: List[str] = None, team_dir: str = None) -> str:
    with _RELAY_LOCK:
        if not sync_relay_repo(team_dir):
            return "Error: Could not sync with relay repo. Check Relay URL/PAT in Teams settings."

        t_dir = team_dir or toolbox.get_team_dir()
        from_team = toolbox.get_team_name(t_dir)
        relay_dir = get_relay_local_dir(t_dir)
        teams_dir = os.path.join(relay_dir, "teams")

        # Validate target_team against registered peers on the relay
        valid_teams = [d for d in os.listdir(teams_dir) if os.path.isdir(os.path.join(teams_dir, d))] if os.path.exists(teams_dir) else []
        matched_target = None
        for vt in valid_teams:
            if vt.lower() == target_team.strip().lower():
                matched_target = vt
                break

        if not matched_target:
            peers_list = [f"'{vt}'" for vt in valid_teams if vt.lower() != from_team.lower()]
            peers_str = ", ".join(peers_list) if peers_list else "None"
            return f"Error: No such team '{target_team}'. Registered peers on this channel: [{peers_str}]. Dispatch rejected."

        if matched_target.lower() == from_team.lower():
            return f"Error: Cannot dispatch message to your own team ('{from_team}')."

        target_team = matched_target
        bundle = package_and_encrypt(instruction, file_attachments or [], target_team, from_team, team_dir=t_dir)

        header = json.loads(bundle.split(b"\n---SIG---\n")[0].decode("utf-8"))
        msg_id = header["msg_id"]

        relay_dir = get_relay_local_dir(t_dir)
        team_inbox = os.path.join(relay_dir, "messages", target_team)
        os.makedirs(team_inbox, exist_ok=True)

        pkg_path = os.path.join(team_inbox, f"{msg_id}.fedmsg")
        with open(pkg_path, "wb") as f:
            f.write(bundle)

        meta_path = os.path.join(team_inbox, f"{msg_id}.meta.json")
        with open(meta_path, "w") as f:
            json.dump({"msg_id": msg_id, "from_team": from_team, "target_team": target_team, "ticks": 1, "created": time.time()}, f)

        push_relay_repo(t_dir)
        return f"[✓ Single Tick]: Dispatch package '{msg_id}' stored in relay inbox for team '{target_team}'."

def emit_ack(msg_id: str, for_team: str, status: str, team_dir: str = None):
    with _RELAY_LOCK:
        relay_dir = get_relay_local_dir(team_dir)
        acks_dir = os.path.join(relay_dir, "acks")
        os.makedirs(acks_dir, exist_ok=True)
        ack_file = os.path.join(acks_dir, f"ack_{msg_id}.json")
        with open(ack_file, "w") as f:
            json.dump({"msg_id": msg_id, "for_team": for_team, "status": status, "time": time.time()}, f)
        push_relay_repo(team_dir)

def process_acks_and_purge(team_dir: str = None):
    """Scans for triple_tick ACKs and automatically deletes heavy attachments from the relay repo."""
    with _RELAY_LOCK:
        relay_dir = get_relay_local_dir(team_dir)
        if not os.path.exists(relay_dir):
            return
        sync_relay_repo(team_dir)
        acks_dir = os.path.join(relay_dir, "acks")
        if not os.path.exists(acks_dir):
            return

        purged_any = False
        for fname in os.listdir(acks_dir):
            if fname.startswith("ack_") and fname.endswith(".json"):
                try:
                    ack_path = os.path.join(acks_dir, fname)
                    with open(ack_path, "r") as f:
                        ack = json.load(f)
                    if ack.get("status") == "triple_tick":
                        msg_id = ack.get("msg_id")
                        messages_root = os.path.join(relay_dir, "messages")
                        if os.path.exists(messages_root):
                            for team_sub in os.listdir(messages_root):
                                pkg_file = os.path.join(messages_root, team_sub, f"{msg_id}.fedmsg")
                                proc_file = pkg_file + ".processed"
                                meta_file = os.path.join(messages_root, team_sub, f"{msg_id}.meta.json")
                                
                                # 1. Always purge local file remnants first (even if meta was marked purged remotely)
                                for candidate in (pkg_file, proc_file):
                                    if os.path.exists(candidate):
                                        os.remove(candidate)
                                        subprocess.run(["git", "add", "-A"], cwd=relay_dir, capture_output=True, timeout=15)
                                        subprocess.run(["git", "commit", "-m", f"Purge acknowledged message {msg_id}"], cwd=relay_dir, capture_output=True, timeout=15)
                                        purged_any = True

                                # 2. Skip updating meta if already stamped
                                if os.path.exists(meta_file):
                                    try:
                                        with open(meta_file, "r") as mf:
                                            if json.load(mf).get("status") == "purged":
                                                continue
                                    except Exception: pass
                                    with open(meta_file, "w") as mf:
                                        json.dump({"msg_id": msg_id, "ticks": 3, "status": "purged"}, mf)
                except Exception:
                    pass

        if purged_any:
            push_relay_repo(team_dir)