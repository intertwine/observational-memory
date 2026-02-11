"""QMD search backend (requires bun + qmd installed externally)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import Document, DocumentSource, SearchResult


class QMDBackend:
    """Search backend using the QMD CLI.

    Args:
        memory_dir: Path to the observational memory data directory.
        mode: QMD search command to use. "search" for BM25 keyword matching,
              "query" for hybrid search (BM25 + vector embeddings + LLM reranking).
    """

    COLLECTION_NAME = "observational-memory"

    def __init__(self, memory_dir: Path, mode: str = "search") -> None:
        self._memory_dir = memory_dir
        self._docs_dir = memory_dir / ".qmd-docs"
        self._mode = mode

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
                "qmd", self._mode, query,
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
            # file is a qmd:// URL like "qmd://observational-memory/ref-active-projects.md"
            file_url = hit.get("file", "")
            # Extract filename from URL: last segment after /
            filename = file_url.rsplit("/", 1)[-1] if "/" in file_url else file_url
            stem = filename.removesuffix(".md") if filename else ""

            # Try reading the actual file from .qmd-docs/
            local_path = self._docs_dir / filename if filename else None
            if local_path and local_path.exists():
                content = local_path.read_text()
            else:
                content = hit.get("snippet", hit.get("content", ""))

            doc_id = stem.replace("_", ":", 1) if stem else ""
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
                        heading=hit.get("title", ""),
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
