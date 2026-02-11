"""QMD search backend (requires bun + qmd installed externally)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import Document, DocumentSource, SearchResult


class QMDBackend:
    """Search backend using the QMD CLI."""

    COLLECTION_NAME = "observational-memory"

    def __init__(self, memory_dir: Path) -> None:
        self._memory_dir = memory_dir
        self._docs_dir = memory_dir / ".qmd-docs"

    def index(self, documents: list[Document]) -> None:
        """Write documents as .md files and run qmd update."""
        self._docs_dir.mkdir(parents=True, exist_ok=True)

        # Clear old files
        for f in self._docs_dir.glob("*.md"):
            f.unlink()

        for doc in documents:
            safe_name = doc.doc_id.replace(":", "_").replace("/", "_")
            (self._docs_dir / f"{safe_name}.md").write_text(doc.content)

        self._ensure_collection()
        subprocess.run(["qmd", "update"], capture_output=True, check=True)

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if not self.is_ready():
            return []

        result = subprocess.run(
            [
                "qmd", "search", query,
                "-c", self.COLLECTION_NAME,
                "-n", str(limit),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []

        try:
            hits = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        results = []
        for rank, hit in enumerate(hits, start=1):
            file_path = Path(hit.get("file", ""))
            content = file_path.read_text() if file_path.exists() else hit.get("content", "")
            doc_id = file_path.stem.replace("_", ":", 1) if file_path.stem else ""
            source = (
                DocumentSource.OBSERVATIONS
                if doc_id.startswith("obs:")
                else DocumentSource.REFLECTIONS
            )
            results.append(
                SearchResult(
                    document=Document(
                        doc_id=doc_id,
                        source=source,
                        heading="",
                        content=content,
                    ),
                    score=float(hit.get("score", 0.0)),
                    rank=rank,
                )
            )
        return results

    def is_ready(self) -> bool:
        try:
            result = subprocess.run(
                ["qmd", "collection", "list"],
                capture_output=True,
                text=True,
            )
            return self.COLLECTION_NAME in result.stdout
        except FileNotFoundError:
            return False

    def _ensure_collection(self) -> None:
        if self.is_ready():
            return
        subprocess.run(
            [
                "qmd", "collection", "add", str(self._docs_dir),
                "--name", self.COLLECTION_NAME,
                "--mask", "**/*.md",
            ],
            capture_output=True,
            check=True,
        )
