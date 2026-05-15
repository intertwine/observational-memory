"""Low-dependency HTTP relay server for opaque OM Cluster artifacts."""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from observational_memory import __version__

_FORBIDDEN_ARTIFACT_PATTERNS = [
    re.compile(rb"signing_private_key_b64"),
    re.compile(rb"encryption_private_key_b64"),
    re.compile(rb"request_secret_b64"),
    re.compile(rb'"data_keys"\s*:'),
    re.compile(rb"ANTHROPIC_API_KEY"),
    re.compile(rb"OPENAI_API_KEY"),
    re.compile(rb"sk-[A-Za-z0-9_-]{12,}"),
]


def serve_relay(storage_dir: Path, *, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create a file-backed relay server.

    The relay stores opaque JSON/bytes artifacts under ``storage_dir``. It does
    not decrypt, validate membership, or become cluster trust.
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    handler = _handler_for(storage_dir)
    server = ThreadingHTTPServer((host, port), handler)
    server.storage_dir = storage_dir  # type: ignore[attr-defined]
    return server


def scan_relay_artifacts(storage_dir: Path) -> dict[str, Any]:
    """Scan relay artifacts for obvious plaintext secret material."""
    findings: list[dict[str, str]] = []
    file_count = 0
    byte_count = 0
    if storage_dir.exists():
        for path in sorted(p for p in storage_dir.rglob("*") if p.is_file()):
            file_count += 1
            data = path.read_bytes()
            byte_count += len(data)
            for pattern in _FORBIDDEN_ARTIFACT_PATTERNS:
                if pattern.search(data):
                    findings.append({"path": str(path.relative_to(storage_dir)), "pattern": pattern.pattern.decode()})
    return {
        "artifact_dir": str(storage_dir),
        "file_count": file_count,
        "byte_count": byte_count,
        "ok": not findings,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run an OM Cluster relay server.")
    parser.add_argument("--storage-dir", required=True, help="Directory for opaque relay artifacts.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", default=8765, type=int, help="Bind port.")
    args = parser.parse_args(argv)
    server = serve_relay(Path(args.storage_dir), host=args.host, port=args.port)
    host, port = server.server_address
    print(f"OM Cluster relay serving http://{host}:{port} storage={args.storage_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _handler_for(storage_dir: Path) -> type[BaseHTTPRequestHandler]:
    class RelayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = self._parts()
            if parts == ["healthz"]:
                return self._send_json(
                    {
                        "ok": True,
                        "version": __version__,
                        "storage": str(storage_dir),
                        "artifact_scan": scan_relay_artifacts(storage_dir),
                    }
                )
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "clusters" and parts[3] == "heads":
                return self._send_json(self._heads(parts[2]))
            if len(parts) == 5 and parts[3] == "heads":
                return self._send_stored(parts[2], "heads", parts[4])
            if len(parts) == 4 and parts[3] == "nodes":
                return self._send_json(self._ids(parts[2], "nodes"))
            if len(parts) == 5 and parts[3] == "nodes":
                return self._send_stored(parts[2], "nodes", parts[4])
            if len(parts) == 4 and parts[3] == "join-requests":
                return self._send_json(self._ids(parts[2], "join-requests"))
            if len(parts) == 5 and parts[3] == "join-requests":
                return self._send_stored(parts[2], "join-requests", parts[4])
            if len(parts) == 5 and parts[3] == "join-approvals":
                return self._send_stored(parts[2], "join-approvals", parts[4])
            if len(parts) == 5 and parts[3] == "records":
                return self._send_json(self._ids(parts[2], "records", parts[4]))
            if len(parts) == 6 and parts[3] == "records":
                return self._send_stored(parts[2], "records", parts[4], parts[5])
            self.send_error(404)

        def do_PUT(self) -> None:
            parts = self._parts()
            if len(parts) == 5 and parts[3] in {"heads", "nodes", "join-requests", "join-approvals"}:
                return self._store(parts[2], parts[3], parts[4])
            if len(parts) == 6 and parts[3] == "records":
                return self._store(parts[2], "records", parts[4], parts[5])
            self.send_error(404)

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _parts(self) -> list[str]:
            return [unquote(part) for part in self.path.split("?", 1)[0].split("/") if part]

        def _store(self, cluster_id: str, *parts: str) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            path = _artifact_path(storage_dir, cluster_id, *parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.rfile.read(length))
            self.send_response(204)
            self.end_headers()

        def _send_stored(self, cluster_id: str, *parts: str) -> None:
            path = _artifact_path(storage_dir, cluster_id, *parts)
            if not path.exists():
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(path.read_bytes())

        def _send_json(self, value: Any) -> None:
            data = json.dumps(value, sort_keys=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def _ids(self, cluster_id: str, *parts: str) -> list[str]:
            directory = _artifact_path(storage_dir, cluster_id, *parts)
            if not directory.exists():
                return []
            return sorted(path.name for path in directory.iterdir() if path.is_file())

        def _heads(self, cluster_id: str) -> dict[str, int]:
            heads: dict[str, int] = {}
            for node_id in self._ids(cluster_id, "heads"):
                try:
                    head_path = _artifact_path(storage_dir, cluster_id, "heads", node_id)
                    heads[node_id] = int(json.loads(head_path.read_text())["seq"])
                except Exception:
                    continue
            return heads

    return RelayHandler


def _artifact_path(storage_dir: Path, cluster_id: str, *parts: str) -> Path:
    safe = [cluster_id, *parts]
    for part in safe:
        if not part or part in {".", ".."} or "/" in part or "\\" in part:
            raise ValueError("Invalid relay artifact path")
    return storage_dir.joinpath(*safe)
