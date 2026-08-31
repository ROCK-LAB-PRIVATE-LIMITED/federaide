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

    def get_embeddings(self, text: str) -> List[Dict[str, Any]]:
        """Calls the Go binary to get sentences and vectors."""
        try:
            # We use absolute path for the binary if it's in the current dir
            cmd = [self.binary_path, text]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
            return json.loads(result.stdout)
        except Exception as e:
            print(f"Error calling embed binary: {e}")
            return []

    def index_message(self, agent_name: str, session_id: str, message_idx: int, text: str):
        """Embeds and stores a message in the database."""
        results = self.get_embeddings(text)
        if not results:
            return

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            for s_idx, res in enumerate(results):
                # Store vector as binary blob (float64)
                vector_np = np.array(res["vector"], dtype=np.float64)
                cursor.execute("""
                    INSERT INTO embeddings (agent_name, session_id, message_idx, sentence_idx, text, vector)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (agent_name, session_id, message_idx, s_idx, res["text"], vector_np.tobytes()))
            conn.commit()
            conn.close()

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
