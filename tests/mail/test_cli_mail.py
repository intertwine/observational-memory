"""End-to-end `om mail` CLI tests over the localdir provider.

Two isolated "machines" (separate XDG data homes) share only a localdir
mailbox root — the same shape as two hosts sharing an AgentMail account.
The flow under test is the full concept prove-out: A mails B a memory note,
B reviews and accepts it into observations, A then asks B's memory a
question over email and receives B's recall answer, and A ships B an
encrypted, scope-filtered context pack.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from observational_memory.cli import cli
from observational_memory.mail.account import new_shared_key_b64

SECRET = "SECRET-LOCAL-ONLY-FACT"


def _agent_env(tmp_path, name: str, maildir) -> dict[str, str]:
    base = tmp_path / name
    for sub in ("home", "config", "data"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(base / "home"),
        "XDG_CONFIG_HOME": str(base / "config"),
        "XDG_DATA_HOME": str(base / "data"),
        "OM_MAIL_PROVIDER": "localdir",
        "OM_MAIL_LOCALDIR": str(maildir),
        "OM_SEARCH_BACKEND": "bm25",
        "OM_CLUSTER_ENABLED": "0",
        "OM_USAGE_TRACKING": "0",
    }


def _memory_dir(tmp_path, name: str):
    return tmp_path / name / "data" / "observational-memory"


def _invoke(runner, env, args, expect_exit=0):
    result = runner.invoke(cli, args, env=env)
    assert result.exit_code == expect_exit, f"om {' '.join(args)} failed:\n{result.output}"
    return result


def _status(runner, env) -> dict:
    return json.loads(_invoke(runner, env, ["mail", "status", "--json"]).output)


def _pin(runner, env, address, key, *, shared_key=None, allow_recall=False, auto_accept=False):
    args = ["mail", "peers", "add", address, "--key", key]
    if shared_key:
        args += ["--shared-key", shared_key]
    if allow_recall:
        args.append("--allow-recall")
    if auto_accept:
        args.append("--auto-accept")
    _invoke(runner, env, args)


def test_two_agents_exchange_memory_over_localdir_mailboxes(tmp_path):
    runner = CliRunner()
    maildir = tmp_path / "shared-mail"
    env_a = _agent_env(tmp_path, "agent-a", maildir)
    env_b = _agent_env(tmp_path, "agent-b", maildir)

    _invoke(runner, env_a, ["mail", "init", "--username", "agent-a"])
    _invoke(runner, env_b, ["mail", "init", "--username", "agent-b"])
    status_a = _status(runner, env_a)
    status_b = _status(runner, env_b)
    shared_key = new_shared_key_b64()

    # Mutual pinning: B will answer A's recall requests; B does NOT auto-accept
    # notes, so ingestion stays an explicit human/agent decision.
    _pin(runner, env_a, status_b["address"], status_b["signing_public_key_b64"], shared_key=shared_key)
    _pin(
        runner,
        env_b,
        status_a["address"],
        status_a["signing_public_key_b64"],
        shared_key=shared_key,
        allow_recall=True,
    )

    # 1. A mails B a memory note; B holds it, reviews, and accepts it.
    _invoke(
        runner,
        env_a,
        [
            "mail",
            "send-note",
            status_b["address"],
            "--subject",
            "deploy window",
            "--text",
            "- 🟡 Project Aurora deploys on Fridays only.",
        ],
    )
    sync = json.loads(_invoke(runner, env_b, ["mail", "sync", "--json"]).output)
    assert sync["held"] == 1 and sync["ingested"] == 0
    held = json.loads(_invoke(runner, env_b, ["mail", "inbox", "--json"]).output)
    assert len(held) == 1 and "accept" in held[0]["reason"]
    _invoke(runner, env_b, ["mail", "accept", held[0]["message_id"]])
    observations = (_memory_dir(tmp_path, "agent-b") / "observations.md").read_text()
    assert "Project Aurora deploys on Fridays" in observations
    assert f"source=mail:{status_a['address']}" in observations

    # 2. A asks B's memory a question over email; B answers from recall.
    asked = json.loads(
        _invoke(
            runner,
            env_a,
            ["mail", "ask", status_b["address"], "--query", "When does Aurora deploy?", "--json"],
        ).output
    )
    assert asked["status"] == "sent"
    respond = json.loads(_invoke(runner, env_b, ["mail", "sync", "--respond", "--json"]).output)
    assert respond["responded"] == 1
    pickup = json.loads(_invoke(runner, env_a, ["mail", "sync", "--json"]).output)
    assert pickup["responses"] == 1
    response_path = _memory_dir(tmp_path, "agent-a") / "mail" / "responses" / f"{asked['request_id']}.json"
    response = json.loads(response_path.read_text())
    assert response["recall_status"] == "ok"
    assert any("aurora" in str(item.get("content", "")).lower() for item in response["results"])

    # 3. A ships B an encrypted context pack; scope=local content never leaves.
    memory_a = _memory_dir(tmp_path, "agent-a")
    memory_a.mkdir(parents=True, exist_ok=True)
    (memory_a / "reflections.md").write_text(
        "# Reflections\n\n## Working Mode\n\n"
        "- Ships small reversible steps. <!--om: id=wm1 kind=preference-->\n"
        f"- {SECRET} <!--om: scope=local-->\n"
    )
    (memory_a / "profile.md").write_text("# Profile\n\n- Engineer working on memory systems.\n")
    _invoke(runner, env_a, ["mail", "send-pack", status_b["address"]])
    packs = json.loads(_invoke(runner, env_b, ["mail", "sync", "--json"]).output)
    assert packs["packs"] == 1
    pack_dirs = list((_memory_dir(tmp_path, "agent-b") / "mail" / "packs").iterdir())
    assert len(pack_dirs) == 1
    packed_reflections = (pack_dirs[0] / "reflections.md").read_text()
    assert "small reversible steps" in packed_reflections
    assert SECRET not in packed_reflections
    raw_mailbox = b"".join(path.read_bytes() for path in maildir.rglob("*") if path.is_file())
    assert SECRET.encode() not in raw_mailbox  # never on the wire, even encrypted-at-source

    # `om mail search` covers the mail-side corpus on the receiving end.
    found = _invoke(runner, env_b, ["mail", "search", "--query", "reversible"]).output
    assert "reversible" in found


def test_unknown_sender_is_held_and_never_ingested(tmp_path):
    runner = CliRunner()
    maildir = tmp_path / "shared-mail"
    env_b = _agent_env(tmp_path, "agent-b", maildir)
    env_c = _agent_env(tmp_path, "agent-c", maildir)

    _invoke(runner, env_b, ["mail", "init", "--username", "agent-b"])
    _invoke(runner, env_c, ["mail", "init", "--username", "agent-c"])
    status_b = _status(runner, env_b)

    # C knows B's key, but B never pinned C — the note must quarantine.
    _pin(runner, env_c, status_b["address"], status_b["signing_public_key_b64"])
    _invoke(runner, env_c, ["mail", "send-note", status_b["address"], "--text", "- injected fact"])
    sync = json.loads(_invoke(runner, env_b, ["mail", "sync", "--json"]).output)
    assert sync["held"] == 1 and sync["ingested"] == 0
    held = json.loads(_invoke(runner, env_b, ["mail", "inbox", "--json"]).output)
    assert "unknown sender" in held[0]["reason"]
    observations_path = _memory_dir(tmp_path, "agent-b") / "observations.md"
    assert not observations_path.exists() or "injected fact" not in observations_path.read_text()

    # Accepting an unknown-sender note must fail closed, not ingest.
    result = runner.invoke(cli, ["mail", "accept", held[0]["message_id"]], env=env_b)
    assert result.exit_code != 0


def test_mail_init_is_guarded_and_status_requires_account(tmp_path):
    runner = CliRunner()
    maildir = tmp_path / "shared-mail"
    env_a = _agent_env(tmp_path, "agent-a", maildir)

    result = runner.invoke(cli, ["mail", "status"], env=env_a)
    assert result.exit_code != 0 and "om mail init" in result.output

    _invoke(runner, env_a, ["mail", "init", "--username", "agent-a"])
    result = runner.invoke(cli, ["mail", "init", "--username", "agent-a"], env=env_a)
    assert result.exit_code != 0 and "--force" in result.output
