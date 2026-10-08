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
import sqlite3
import subprocess
import numpy as np
from typing import List, Dict, Any, Optional
import threading
import platform

class SemanticSearchEngine:
    def __init__(self, db_path: str = "episodic_memory.db", binary_path: str = None):
        self.db_path = db_path
        if binary_path is None:
            # Look in the packaged bin/ folder relative to this file
            rel_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "federate_embed" + (".exe" if platform.system() == "Windows" else ""))
        else:
            rel_path = binary_path
            
        # Convert to absolute path so Windows CreateProcess can resolve the binary correctly
        self.binary_path = os.path.abspath(rel_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name TEXT,
                    session_id TEXT,
                    message_idx INTEGER,
                    sentence_idx INTEGER,
                    text TEXT,
                    vector BLOB,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Index for fast filtering by agent
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent ON embeddings(agent_name)")
            conn.commit()
            conn.close()

    def get_embeddings(self, text: str, model_name: str = None, log_cb=None) -> List[Dict[str, Any]]:
        """Calls the Go binary to get sentences and vectors with dedicated log routing."""
        try:
            import toolbox
            if not model_name:
                settings = toolbox.load_global_settings()
                model_name = settings.get("embedding_model", "sentence-transformers/all-MiniLM-L6-v2")
            
            cmd = [self.binary_path, model_name, text]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            
            # Pipe stderr / Cybertron download logs to the caller's log_cb if provided
            if result.stderr and result.stderr.strip() and log_cb:
                for line in result.stderr.strip().splitlines():
                    if line.strip():
                        try:
                            log_data = json.loads(line)
                            msg = log_data.get("message") or line
                            log_cb(f"[dim]{msg}[/dim]")
                        except Exception:
                            log_cb(f"[dim]{line}[/dim]")

            if result.returncode != 0:
                err_msg = f"Embed binary failed (code {result.returncode}). Stderr: {result.stderr.strip()}"
                if log_cb:
                    log_cb(f"[red]{err_msg}[/red]")
                return []
                
            out_str = result.stdout.strip()
            if not out_str or out_str == "null" or out_str == "[]":
                return []

            if out_str.startswith("["):
                return json.loads(out_str)
                
            import re
            match = re.search(r'\[\s*\{.*\}\s*\]', out_str, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            
            err_msg = f"No JSON array found in output: {out_str[:200]}"
            if log_cb:
                log_cb(f"[red]{err_msg}[/red]")
            return []
        except Exception as e:
            if log_cb:
                log_cb(f"[red]Error calling embed binary: {e}[/red]")
            return []

    def index_message(self, agent_name: str, session_id: str, message_idx: int, text: str, model_name: str = None, log_cb=None):
        """Embeds and stores a message in the database."""
        results = self.get_embeddings(text, model_name=model_name, log_cb=log_cb)
        if not results:
            return

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            for s_idx, res in enumerate(results):
                vector_np = np.array(res["vector"], dtype=np.float64)
                cursor.execute("""
                    INSERT INTO embeddings (agent_name, session_id, message_idx, sentence_idx, text, vector)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (agent_name, session_id, message_idx, s_idx, res["text"], vector_np.tobytes()))
            conn.commit()
            conn.close()

    def reindex_all_sessions(self, sessions_dir: str, model_name: str = None, progress_cb=None, log_cb=None) -> int:
        """
        Clears and re-indexes all historical session JSON messages into the vector database
        using the selected embedding model.
        """
        if not os.path.exists(sessions_dir):
            return 0
            
        import glob
        session_files = glob.glob(os.path.join(sessions_dir, "*.json"))
        
        # 1. Collect all non-empty messages
        tasks = []
        for filepath in sorted(session_files):
            filename = os.path.basename(filepath)
            parts = filename.replace(".json", "").split("_")
            if len(parts) < 3:
                continue
            session_id = f"{parts[0]}_{parts[1]}"
            agent_name = "_".join(parts[2:])
            
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    history = json.load(f)
                if not isinstance(history, list):
                    continue
                for idx, msg in enumerate(history):
                    if not isinstance(msg, dict) or msg.get("role") == "system":
                        continue
                    content = msg.get("content", "")
                    if content and content.strip():
                        tasks.append((agent_name, session_id, idx, content.strip()))
            except Exception:
                continue
                
        total = len(tasks)
        if total == 0:
            if log_cb:
                log_cb("[yellow]No session messages found to re-index.[/yellow]")
            return 0

        # 2. Wipe the existing embeddings table to reset dimensions cleanly
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM embeddings;")
            conn.commit()
            cursor.execute("VACUUM;")
            conn.close()

        # 3. Re-index messages with live progress tracking
        indexed_count = 0
        for i, (agent_name, session_id, idx, content) in enumerate(tasks, start=1):
            pct = (i / total) * 100.0
            if progress_cb:
                snippet = content[:45].replace("\n", " ") + ("..." if len(content) > 45 else "")
                progress_cb(i, total, pct, f"{agent_name} ({session_id}) - \"{snippet}\"")
            
            self.index_message(agent_name, session_id, idx, content, model_name=model_name, log_cb=log_cb)
            indexed_count += 1

        return indexed_count

    def search(self, agent_name: str, query: str, limit: int = 5, exclude_session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Performs semantic search restricted to a specific agent, optionally excluding a session."""
        query_embeddings = self.get_embeddings(query)
        if not query_embeddings:
            return []
        
        # We use the first sentence of the query if multiple were generated
        query_vec = np.array(query_embeddings[0]["vector"], dtype=np.float64)
        
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            if exclude_session_id:
                # Retrieve all vectors for this agent EXCEPT the excluded session
                cursor.execute("""
                    SELECT id, text, vector, session_id, message_idx 
                    FROM embeddings 
                    WHERE agent_name = ? AND session_id != ?
                """, (agent_name, exclude_session_id))
            else:
                # Retrieve all vectors for this agent
                cursor.execute("SELECT id, text, vector, session_id, message_idx FROM embeddings WHERE agent_name = ?", (agent_name,))
                
            rows = cursor.fetchall()
            conn.close()

        if not rows:
            return []

        # Calculate similarities using NumPy
        # Note: For thousands of rows, this is still very fast on mobile
        matches = []
        for row_id, text, vec_blob, sess_id, msg_idx in rows:
            vec = np.frombuffer(vec_blob, dtype=np.float64)
            if vec.shape != query_vec.shape:
                continue
            # Cosine similarity: (A dot B) / (||A|| * ||B||)
            # Since vectors from Cybertron are usually normalized, we could just do dot
            # but let's be robust.
            similarity = np.dot(query_vec, vec) / (np.linalg.norm(query_vec) * np.linalg.norm(vec))
            matches.append({
                "id": row_id,
                "text": text,
                "score": float(similarity),
                "session_id": sess_id,
                "message_idx": msg_idx
            })

        # Sort by score descending
        matches.sort(key=lambda x: x["score"], reverse=True)
        return matches[:limit]

    def is_indexed(self, agent_name: str, session_id: str, message_idx: int) -> bool:
        """Checks if a specific message is already in the database."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM embeddings 
                WHERE agent_name = ? AND session_id = ? AND message_idx = ? 
                LIMIT 1
            """, (agent_name, session_id, message_idx))
            exists = cursor.fetchone() is not None
            conn.close()
            return exists
