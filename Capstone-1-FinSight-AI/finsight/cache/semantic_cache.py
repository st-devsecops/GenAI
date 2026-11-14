
"""Semantic cache (Module 6, Session 9).

If a new question is semantically near-identical (cosine >= threshold) to one
answered recently, return the stored answer and skip retrieval + generation.
SQLite + NumPy: free, file-based, no extra server.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np


class SemanticCache:
    def __init__(self, db_path: Path, threshold: float = 0.95, ttl_s: int = 86400):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.threshold = threshold
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cache (id INTEGER PRIMARY KEY, scope TEXT, question TEXT, "
            "embedding BLOB, payload TEXT, created REAL)"
        )
        self._db.commit()

    @staticmethod
    def _norm(vec: list[float]) -> np.ndarray:
        v = np.asarray(vec, dtype=np.float32)
        n = np.linalg.norm(v)
        return v / n if n else v

    def lookup(self, embedding: list[float], scope: str = "") -> tuple[dict, float] | None:
        """Find the best matching unexpired embedding within the requested scope."""
        query_vector = self._norm(embedding)

        if not np.any(query_vector):
            return None

        cutoff = time.time() - self.ttl_s
        best_payload = None
        best_similarity = -1.0

        with self._lock:
            rows = self._db.execute(
                """
                SELECT embedding, payload
                FROM cache
                WHERE scope = ? AND created >= ?
                """,
                (scope, cutoff),
            ).fetchall()

        for stored_embedding, payload in rows:
            stored_vector = np.frombuffer(
                stored_embedding,
                dtype=np.float32,
            )

            # Skip incompatible embedding dimensions.
            if stored_vector.shape != query_vector.shape:
                continue

            similarity = float(np.dot(query_vector, stored_vector))

            if similarity > best_similarity:
                best_similarity = similarity
                best_payload = payload

        if best_payload is not None and best_similarity >= self.threshold:
            return json.loads(best_payload), best_similarity

        return None

    def store(
        self,
        question: str,
        embedding: list[float],
        payload: dict,
        scope: str = "",
    ) -> None:
        """Store a question, normalized embedding and JSON payload."""
        normalized = self._norm(embedding)

        with self._lock:
            self._db.execute(
                """
                INSERT INTO cache (scope, question, embedding, payload, created)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    question,
                    normalized.tobytes(),
                    json.dumps(payload),
                    time.time(),
                ),
            )
            self._db.commit()

    def clear(self) -> int:
        with self._lock:
            n = self._db.execute("DELETE FROM cache").rowcount
            self._db.commit()
        return n

    def size(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
