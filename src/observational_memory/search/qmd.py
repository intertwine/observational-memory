"""QMD search backend (requires qmd installed externally)."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import Document, DocumentSource, SearchResult

_QMD_INSPECT_TIMEOUT_SECONDS = 5.0


def _combine_output(result: subprocess.CompletedProcess) -> str:
    parts = []
    if result.stdout:
        parts.append(result.stdout.strip())
    if result.stderr:
        parts.append(result.stderr.strip())
    return "\n".join(part for part in parts if part).strip()


def _qmd_env(env_overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return env


def _run_qmd(
    args: list[str],
    *,
    env_overrides: dict[str, str] | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["qmd", *args],
        capture_output=True,
        text=True,
        check=check,
        env=_qmd_env(env_overrides),
        timeout=timeout,
    )


@dataclass
class QMDInstallInfo:
    available: bool
    binary_path: str | None = None
    supports_index: bool = False
    supports_no_rerank: bool = False
    supports_bench: bool = False
    help_output: str = ""
    error: str | None = None


@dataclass
class QMDIndexInfo:
    index_name: str
    collection_name: str
    collection_exists: bool = False
    index_path: str | None = None
    total_files: int | None = None
    vectors_embedded: int | None = None
    pending_vectors: int | None = None
    updated: str | None = None
    raw_output: str = ""
    error: str | None = None


def inspect_qmd_install(env_overrides: dict[str, str] | None = None) -> QMDInstallInfo:
    """Return QMD installation and capability details."""
    binary_path = shutil.which("qmd")
    if not binary_path:
        return QMDInstallInfo(available=False, error="qmd not found on PATH")

    try:
        result = _run_qmd(["--help"], env_overrides=env_overrides, timeout=_QMD_INSPECT_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return QMDInstallInfo(available=False, error="qmd not found on PATH")
    except subprocess.TimeoutExpired:
        return QMDInstallInfo(
            available=False,
            binary_path=binary_path,
            error=f"qmd help timed out after {_QMD_INSPECT_TIMEOUT_SECONDS:g}s",
        )

    help_output = _combine_output(result)
    return QMDInstallInfo(
        available=True,
        binary_path=binary_path,
        supports_index="--index" in help_output,
        supports_no_rerank="--no-rerank" in help_output,
        supports_bench="qmd bench" in help_output,
        help_output=help_output,
        error=None if help_output else "qmd help returned no output",
    )


def _collection_names(list_output: str) -> list[str]:
    names = []
    for line in list_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "\t" in line:
            names.append(line.split("\t", 1)[0].strip())
            continue
        if line[:1].isspace() or stripped.startswith("Collections ("):
            continue
        match = re.match(r"(.+?)\s+\(qmd://[^)]+\)$", stripped)
        if match:
            names.append(match.group(1).strip())
    return names


def _inspect_collection_listing(
    index_name: str,
    *,
    env_overrides: dict[str, str] | None = None,
    timeout: float | None = _QMD_INSPECT_TIMEOUT_SECONDS,
) -> tuple[list[str] | None, str, str | None]:
    try:
        list_result = _run_qmd(
            ["--index", index_name, "collection", "list"],
            env_overrides=env_overrides,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, "", "qmd not found on PATH"
    except subprocess.TimeoutExpired:
        return None, "", f"qmd collection list timed out after {timeout:g}s"

    list_output = _combine_output(list_result)
    if list_result.returncode != 0:
        return None, list_output, list_output or "qmd collection list failed"

    return _collection_names(list_output), list_output, None


def qmd_collection_exists(
    index_name: str,
    collection_name: str,
    *,
    env_overrides: dict[str, str] | None = None,
    timeout: float | None = _QMD_INSPECT_TIMEOUT_SECONDS,
) -> tuple[bool, str, str | None]:
    names, raw_output, error = _inspect_collection_listing(
        index_name,
        env_overrides=env_overrides,
        timeout=timeout,
    )
    if names is None:
        return False, raw_output, error
    return collection_name in names, raw_output, None


def inspect_qmd_index(
    index_name: str,
    collection_name: str,
    *,
    env_overrides: dict[str, str] | None = None,
) -> QMDIndexInfo:
    """Inspect whether an OM-managed QMD index and collection are ready."""
    info = QMDIndexInfo(index_name=index_name, collection_name=collection_name)

    collection_exists, list_output, error = qmd_collection_exists(
        index_name,
        collection_name,
        env_overrides=env_overrides,
    )
    if error:
        info.error = error
        info.raw_output = list_output
        return info

    info.collection_exists = collection_exists
    if not info.collection_exists:
        info.raw_output = list_output
        return info

    try:
        status_result = _run_qmd(
            ["--index", index_name, "status"],
            env_overrides=env_overrides,
            timeout=_QMD_INSPECT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        info.error = f"qmd status timed out after {_QMD_INSPECT_TIMEOUT_SECONDS:g}s"
        return info

    status_output = _combine_output(status_result)
    info.raw_output = status_output
    if status_result.returncode != 0:
        info.error = status_output or "qmd status failed"
        return info

    info.index_path = _extract_text(r"^Index:\s+(.+)$", status_output)
    info.updated = _extract_text(r"^\s*Updated:\s+(.+)$", status_output)
    info.total_files = _extract_int(r"^\s*Total:\s+(\d+)\s+files indexed$", status_output)
    info.vectors_embedded = _extract_int(r"^\s*Vectors:\s+(\d+)\s+embedded$", status_output)
    info.pending_vectors = _extract_int(r"^\s*Pending:\s+(\d+)\s+need embedding", status_output)
    return info


def _extract_int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, flags=re.MULTILINE)
    return int(match.group(1)) if match else None


def _extract_text(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


class QMDBackend:
    """Search backend using the QMD CLI.

    Args:
        memory_dir: Path to the observational memory data directory.
        mode: QMD search command to use. "search" for BM25 keyword matching,
              "query" for hybrid search (BM25 + vector embeddings + optional reranking).
    """

    COLLECTION_NAME = "observational-memory"

    def __init__(
        self,
        memory_dir: Path,
        mode: str = "search",
        *,
        index_name: str = "observational-memory",
        no_rerank: bool = False,
        model_env: dict[str, str] | None = None,
    ) -> None:
        self._memory_dir = memory_dir
        self._docs_dir = memory_dir / ".qmd-docs"
        self._manifest_path = self._docs_dir / "manifest.json"
        self._mode = mode
        self._index_name = index_name
        self._no_rerank = no_rerank
        self._model_env = model_env or {}
        self._collection_ready: bool | None = None
        self._supports_no_rerank: bool | None = None

    def index(self, documents: list[Document]) -> None:
        """Write documents as .md files and run qmd update."""
        self._docs_dir.mkdir(parents=True, exist_ok=True)

        for f in self._docs_dir.glob("*.md"):
            f.unlink()

        manifest: dict[str, dict[str, object]] = {}
        for doc in documents:
            filename = self._filename_for_doc_id(doc.doc_id)
            (self._docs_dir / filename).write_text(doc.content)
            manifest[filename] = {
                "doc_id": doc.doc_id,
                "source": doc.source.value,
                "heading": doc.heading,
                "date": doc.date,
                "metadata": doc.metadata,
            }

        self._manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

        self._ensure_collection()
        _run_qmd(self._with_index(["update"]), env_overrides=self._model_env, check=True)

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        command = self._search_command(query, limit=limit, json_output=True)

        try:
            result = _run_qmd(command, env_overrides=self._model_env)
        except FileNotFoundError:
            return []
        if result.returncode != 0:
            return []

        try:
            hits = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        manifest = self._load_manifest()
        results = []
        for rank, hit in enumerate(hits, start=1):
            file_url = hit.get("file", "")
            filename = file_url.rsplit("/", 1)[-1] if "/" in file_url else file_url
            local_path = self._docs_dir / filename if filename else None

            manifest_entry = manifest.get(filename, {})
            if local_path and local_path.exists():
                content = local_path.read_text()
            else:
                content = hit.get("snippet", hit.get("content", ""))

            doc_id = str(manifest_entry.get("doc_id") or self._fallback_doc_id(filename))
            source = self._source_from_manifest_or_docid(manifest_entry, doc_id)
            metadata = dict(manifest_entry.get("metadata") or {})
            if file_url:
                metadata["qmd_file"] = file_url
            if hit.get("docid"):
                metadata["qmd_docid"] = hit["docid"]
            if hit.get("line") is not None:
                qmd_line = int(hit["line"])
                metadata["line"] = qmd_line
                metadata["qmd_line"] = qmd_line
                source_start_line = metadata.get("source_start_line")
                if isinstance(source_start_line, int):
                    # QMD reports hit lines as 1-based within the stored section doc.
                    metadata["source_line"] = source_start_line + qmd_line - 1

            results.append(
                SearchResult(
                    document=Document(
                        doc_id=doc_id,
                        source=source,
                        heading=str(manifest_entry.get("heading") or hit.get("title", "")),
                        content=content,
                        date=manifest_entry.get("date"),
                        metadata=metadata,
                    ),
                    score=float(hit.get("score", 0.0)),
                    rank=rank,
                )
            )
        return results

    def raw_search_output(self, query: str, limit: int = 10) -> tuple[str, str, int]:
        """Return raw QMD CLI output for advanced passthrough use."""
        command = self._search_command(query, limit=limit, json_output=False)

        try:
            result = _run_qmd(command, env_overrides=self._model_env)
        except FileNotFoundError:
            return "", "qmd not found on PATH", 127

        return result.stdout, result.stderr, result.returncode

    def _search_command(self, query: str, *, limit: int, json_output: bool) -> list[str]:
        query_arg = query
        if self._mode == "query" and self._no_rerank and self._can_use_no_rerank():
            query_arg = self._structured_no_rerank_query(query)

        command = self._with_index(
            [
                self._mode,
                query_arg,
                "-c",
                self.COLLECTION_NAME,
                "-n",
                str(limit),
            ]
        )
        if json_output:
            command.append("--json")
        if self._mode == "query" and self._no_rerank and self._can_use_no_rerank():
            command.append("--no-rerank")
        return command

    def is_ready(self) -> bool:
        if self._collection_ready is not None:
            return self._collection_ready

        collection_exists, _, _ = qmd_collection_exists(
            self._index_name,
            self.COLLECTION_NAME,
            env_overrides=self._model_env,
            timeout=_QMD_INSPECT_TIMEOUT_SECONDS,
        )
        self._collection_ready = collection_exists
        return collection_exists

    def _ensure_collection(self) -> None:
        if self.is_ready():
            return
        _run_qmd(
            self._with_index(
                [
                    "collection",
                    "add",
                    str(self._docs_dir),
                    "--name",
                    self.COLLECTION_NAME,
                    "--mask",
                    "**/*.md",
                ]
            ),
            env_overrides=self._model_env,
            check=True,
        )
        self._collection_ready = True

    def _with_index(self, args: list[str]) -> list[str]:
        return ["--index", self._index_name, *args]

    def _can_use_no_rerank(self) -> bool:
        if self._supports_no_rerank is None:
            install = inspect_qmd_install()
            self._supports_no_rerank = install.supports_no_rerank
        return self._supports_no_rerank

    def _load_manifest(self) -> dict[str, dict[str, object]]:
        if not self._manifest_path.exists():
            return {}
        try:
            raw = json.loads(self._manifest_path.read_text())
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}

        manifest: dict[str, dict[str, object]] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            manifest[key] = value
            manifest.setdefault(key.lower(), value)
        return manifest

    def _fallback_doc_id(self, filename: str) -> str:
        stem = filename.removesuffix(".md") if filename else ""
        if not stem:
            return ""

        if len(stem) % 2 == 0 and re.fullmatch(r"[0-9a-f]+", stem):
            try:
                return bytes.fromhex(stem).decode()
            except ValueError:
                pass

        padding = "=" * (-len(stem) % 4)
        try:
            return base64.urlsafe_b64decode((stem + padding).encode()).decode()
        except (ValueError, UnicodeDecodeError, binascii.Error):
            # Best-effort compatibility for pre-round-trip-safe filenames.
            return stem.replace("_", ":", 1)

    def _filename_for_doc_id(self, doc_id: str) -> str:
        # Keep filenames lowercase-safe because qmd:// result URLs may normalize paths.
        return f"{doc_id.encode().hex()}.md"

    def _structured_no_rerank_query(self, query: str) -> str:
        # Keep typed query lines single-line so QMD stays on the lightweight lex+vec path.
        normalized_query = " ".join(query.split())
        return f"lex: {normalized_query}\nvec: {normalized_query}"

    def _source_from_manifest_or_docid(
        self,
        manifest_entry: dict[str, object],
        doc_id: str,
    ) -> DocumentSource:
        source_name = manifest_entry.get("source")
        if isinstance(source_name, str):
            try:
                return DocumentSource(source_name)
            except ValueError:
                pass

        if doc_id.startswith("obs:"):
            return DocumentSource.OBSERVATIONS
        if doc_id.startswith("amem:"):
            return DocumentSource.AUTO_MEMORY
        return DocumentSource.REFLECTIONS
