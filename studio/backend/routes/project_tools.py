# SPDX-License-Identifier: AGPL-3.0-only
"""Authenticated, project-scoped Git operations and durable local-model goals."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from urllib.parse import unquote
from contextlib import closing
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from auth.authentication import get_current_subject
from storage import studio_db as history
from storage import chat_generation_runs_db as runs
from utils.account_context import current_account, arun_as, run_as

router = APIRouter(dependencies=[Depends(get_current_subject)])
logger = logging.getLogger(__name__)


@router.get("/build")
def custom_build():
    return {"version": "0.1.999-custom.1", "projectFileLimit": None, "toolCallLimit": None,
            "git": True, "githubImport": True, "persistentGoals": True, "researchSteering": True}


def project_folder(project_id: str) -> Path:
    project = history.ensure_chat_project_workspace(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    path = history.available_project_working_folder(project) or project["sandboxPath"]
    from core.training.account_jobs import account_path
    return Path(account_path(path)).resolve(strict=True)


def git(root: Path, args: list[str], *, timeout: int = 60) -> str:
    executable = shutil.which("git")
    if not executable:
        raise HTTPException(503, "Install Git for Windows to use project repositories")
    # No shell or client-provided options. Repository hooks are not run by this UI.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        result = subprocess.run(
            [executable, "-c", "http.sslBackend=openssl", "-c", "core.hooksPath=/dev/null",
             "-c", "protocol.file.allow=never", *args],
            cwd=root, env=env, capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(408, "Git operation timed out") from exc
    if result.returncode:
        # Credentials are never accepted in repository URLs by these endpoints.
        detail = re.sub(r"https://[^/@\s]+@", "https://[redacted]@", result.stderr)
        raise HTTPException(409, detail.strip()[-4000:] or "Git operation failed")
    return result.stdout


def repository(project_id: str) -> Path:
    root = project_folder(project_id)
    top = Path(git(root, ["rev-parse", "--show-toplevel"]).strip()).resolve()
    if top != root:
        raise HTTPException(409, "The project's working folder must be the repository root")
    return root


class GitAction(BaseModel):
    operation: Literal["init", "stage", "unstage", "commit", "fetch", "pull", "push", "branch", "checkout"]
    paths: list[str] = Field(default_factory=list)
    message: str = Field(default="", max_length=2000)
    branch: str = Field(default="", max_length=200)


def checked_paths(root: Path, paths: list[str]) -> list[str]:
    if not paths:
        raise HTTPException(400, "Select at least one file")
    for value in paths:
        p = Path(value)
        if p.is_absolute() or ".." in p.parts or ".git" in p.parts or value.startswith("-"):
            raise HTTPException(400, "Invalid repository-relative path")
        if not (root / p).resolve().is_relative_to(root):
            raise HTTPException(400, "Path leaves the project repository")
    return paths


@router.get("/projects/{project_id}/git")
def git_status(project_id: str):
    root = repository(project_id)
    raw = git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    parts = iter(raw.split("\0"))
    files = []
    for part in parts:
        if not part:
            continue
        code, name = part[:2], part[3:]
        if "R" in code or "C" in code:
            next(parts, None)
        files.append({"status": code, "path": name})
    branch = git(root, ["branch", "--show-current"]).strip()
    return {"root": str(root), "branch": branch, "files": files,
            "diff": git(root, ["diff", "--no-ext-diff", "--no-textconv"]),
            "stagedDiff": git(root, ["diff", "--cached", "--no-ext-diff", "--no-textconv"]),
            "remotes": re.sub(r"https://[^/@\s]+@", "https://[redacted]@", git(root, ["remote", "-v"]))}


@router.post("/projects/{project_id}/git")
def git_action(project_id: str, payload: GitAction):
    root = project_folder(project_id) if payload.operation == "init" else repository(project_id)
    if payload.operation == "init":
        args = ["init"]
    elif payload.operation == "stage":
        paths = checked_paths(root, payload.paths)
        # Windows has a command-line byte limit; stage all selected files in bounded batches.
        for index in range(0, len(paths), 20):
            git(root, ["add", "--", *paths[index:index+20]])
        return {"output": f"Staged {len(paths)} files"}
    elif payload.operation == "unstage":
        paths = checked_paths(root, payload.paths)
        for index in range(0, len(paths), 20):
            git(root, ["restore", "--staged", "--", *paths[index:index+20]])
        return {"output": f"Unstaged {len(paths)} files"}
    elif payload.operation == "commit":
        if not payload.message.strip():
            raise HTTPException(400, "Enter a commit message")
        args = ["commit", "-m", payload.message]
    elif payload.operation in {"branch", "checkout"}:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", payload.branch) or ".." in payload.branch:
            raise HTTPException(400, "Invalid branch name")
        git(root, ["check-ref-format", "--branch", payload.branch])
        args = ["switch", "-c", payload.branch] if payload.operation == "branch" else ["switch", payload.branch]
    else:
        args = {"fetch": ["fetch", "origin"], "pull": ["pull", "--ff-only"], "push": ["push", "origin", "HEAD"]}[payload.operation]
    return {"output": git(root, args, timeout=120)}


class CloneRepository(BaseModel):
    url: str = Field(min_length=1, max_length=300)
    indexSources: bool = True


@router.post("/projects/{project_id}/github")
def clone_repository(project_id: str, payload: CloneRepository):
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", payload.url.strip())
    if not match or any(part in {".", ".."} for part in match.groups()):
        raise HTTPException(400, "Use https://github.com/owner/repository without credentials")
    project = history.ensure_chat_project_workspace(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    # Always a new child of the managed project folder. Existing files are never overwritten.
    base = Path(project["sandboxPath"]).resolve(strict=True)
    target = base / (match.group(2) + "-" + uuid.uuid4().hex[:8])
    git(base, ["clone", "--", payload.url.strip(), str(target)], timeout=300)
    identity = target.stat()
    history.set_chat_project_working_folder(project_id, str(target), (identity.st_dev, identity.st_ino))
    indexed = False
    warning = None
    if payload.indexSources:
        try:
            from core.rag import folder_sync
            folder = folder_sync.create_folder(scope_type="project", scope_id=project_id, path=str(target), auto_sync=True)
            folder_sync.request_sync(folder["id"])
            indexed = True
        except (ValueError, RuntimeError, OSError) as exc:
            warning = f"Repository cloned; source indexing could not start: {exc}"
    return {"root": str(target), "indexed": indexed, "warning": warning}


def goal_connection():
    conn = history.get_connection()
    conn.execute("""CREATE TABLE IF NOT EXISTS custom_goals (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, thread_id TEXT NOT NULL,
        subject TEXT NOT NULL, objective TEXT NOT NULL, model TEXT NOT NULL,
        status TEXT NOT NULL, max_turns INTEGER NOT NULL, turns INTEGER NOT NULL DEFAULT 0,
        max_seconds INTEGER NOT NULL, elapsed REAL NOT NULL DEFAULT 0,
        tokens_per_turn INTEGER NOT NULL, run_id TEXT, error TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    conn.commit()
    return conn


def goals(project_id: str | None = None) -> list[dict]:
    with closing(goal_connection()) as conn:
        if project_id is None:
            rows = conn.execute("SELECT * FROM custom_goals WHERE status='running' ORDER BY created_at").fetchall()
        else:
            rows = conn.execute("SELECT * FROM custom_goals WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
        return [dict(row) for row in rows]


def update_goal(goal_id: str, **values):
    allowed = {"status", "turns", "elapsed", "run_id", "error"}
    if not values or not set(values) <= allowed:
        raise ValueError("Invalid goal update")
    with closing(goal_connection()) as conn:
        conn.execute("UPDATE custom_goals SET " + ", ".join(key + "=?" for key in values) + ", updated_at=? WHERE id=?",
                     (*values.values(), time.time(), goal_id))
        conn.commit()


class CreateGoal(BaseModel):
    objective: str = Field(min_length=1, max_length=20000)
    model: str = Field(default="default", max_length=300)
    maxTurns: int = Field(default=50, ge=1, le=10000)
    maxSeconds: int = Field(default=3600, ge=10, le=604800)
    tokensPerTurn: int = Field(default=8192, ge=64, le=131072)


def saved_provider_routing(model: str) -> dict:
    if not model.startswith("external::"):
        return {}
    parts = model.split("::", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        raise HTTPException(400, "Invalid saved provider model")
    from storage import providers_db
    provider = providers_db.get_provider(parts[1])
    if not provider or not provider["is_enabled"]:
        raise HTTPException(400, "The goal's configured provider is unavailable")
    # Store references only. The inference proxy resolves current routing and credentials from
    # the account's existing provider record at execution, including after app restart.
    return {"provider_id": parts[1], "external_model": unquote(parts[2])}


@router.get("/projects/{project_id}/goals")
def list_goals(project_id: str):
    project_folder(project_id)
    return {"goals": goals(project_id)}


@router.post("/projects/{project_id}/goals")
def create_goal(project_id: str, payload: CreateGoal, subject: str = Depends(get_current_subject)):
    project_folder(project_id)
    saved_provider_routing(payload.model)
    goal_id, thread_id = "goal-" + uuid.uuid4().hex, uuid.uuid4().hex
    now = time.time()
    history.upsert_chat_thread({"id": thread_id, "title": "Goal: " + payload.objective[:100], "modelType": "model1",
                               "modelId": payload.model, "projectId": project_id, "createdAt": int(now*1000), "updatedAt": int(now*1000)})
    with closing(goal_connection()) as conn:
        conn.execute("""INSERT INTO custom_goals
          (id,project_id,thread_id,subject,objective,model,status,max_turns,max_seconds,tokens_per_turn,created_at,updated_at)
          VALUES (?,?,?,?,?,?,'running',?,?,?,?,?)""", (goal_id, project_id, thread_id, subject, payload.objective,
            payload.model, payload.maxTurns, payload.maxSeconds, payload.tokensPerTurn, now, now))
        conn.commit()
    return next(g for g in goals(project_id) if g["id"] == goal_id)


class GoalControl(BaseModel):
    action: Literal["pause", "resume", "stop"]


@router.post("/projects/{project_id}/goals/{goal_id}")
def control_goal(project_id: str, goal_id: str, payload: GoalControl, request: Request):
    found = next((g for g in goals(project_id) if g["id"] == goal_id), None)
    if not found:
        raise HTTPException(404, "Goal not found")
    status = {"pause": "paused", "resume": "running", "stop": "stopped"}[payload.action]
    if status == "running" and (found["turns"] >= found["max_turns"] or found["elapsed"] >= found["max_seconds"]):
        raise HTTPException(409, "Execution limit reached; create a new goal to continue")
    update_goal(goal_id, status=status, error=None)
    if status != "running" and found["run_id"]:
        cancel_goal_run(found, request.app)
    return {"status": status}


def cancel_goal_run(goal: dict, app):
    if goal["run_id"]:
        runs.request_cancel(goal["run_id"])
        supervisor = getattr(app.state, "chat_generation_supervisor", None)
        if supervisor:
            supervisor.cancel(goal["run_id"])


def message_text(message: dict) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")


def save_goal_transcript(goal: dict, run: dict) -> dict | None:
    """A goal has no browser follower to mirror SSE chunks into history; do that at settlement."""
    message = history.get_chat_message(goal["thread_id"], run["assistantMessageId"])
    if message is None:
        return None
    pieces = []
    after = 0
    while True:
        events = runs.list_events(run["id"], after=after, limit=1000)
        if not events:
            break
        for event in events:
            after = max(after, int(event["seq"]))
            if event["type"] != "chunk":
                continue
            payload = event["payload"]
            for choice in payload.get("choices", []):
                delta = choice.get("delta") or {}
                if isinstance(delta.get("content"), str):
                    pieces.append(delta["content"])
        if len(events) < 1000:
            break
    if pieces:
        content = message.get("content") or []
        message["content"] = [part for part in content if part.get("type") != "text"] + [{"type": "text", "text": "".join(pieces)}]
        metadata = dict(message.get("metadata") or {})
        metadata["generationSeq"] = run["lastEventSeq"]
        message["metadata"] = metadata
        history.upsert_chat_message(message, allow_generation_edit=True)
    return message


async def tick_goal(goal: dict, app, seconds: float):
    # Charge elapsed time while running; downtime/paused time does not use the budget.
    elapsed = goal["elapsed"] + seconds
    update_goal(goal["id"], elapsed=elapsed)
    if elapsed >= goal["max_seconds"]:
        update_goal(goal["id"], status="limit", error="Time limit reached")
        cancel_goal_run(goal, app)
        return
    if not history.get_chat_project(goal["project_id"]) or not history.get_chat_thread(goal["thread_id"]):
        update_goal(goal["id"], status="stopped", error="Project or goal chat was deleted")
        cancel_goal_run(goal, app)
        return
    if goal["run_id"]:
        run = runs.get_run(goal["run_id"])
        if run and run["status"] in runs.ACTIVE_STATUSES:
            return
        if run and run["status"] == "completed":
            message = save_goal_transcript(goal, run)
            if message and message_text(message).strip().endswith("[GOAL_COMPLETE]"):
                update_goal(goal["id"], status="completed", run_id=None)
                return
        elif run and run["status"] == "failed" and run.get("finishReason") != "interrupted":
            update_goal(goal["id"], status="error", error=run.get("error") or "Generation failed")
            return
        update_goal(goal["id"], run_id=None)
    if goal["turns"] >= goal["max_turns"]:
        update_goal(goal["id"], status="limit", error="Turn limit reached")
        return
    from routes.chat_generation_runs import CreateChatGenerationRun, _sanitize_request
    messages = history.list_chat_messages(goal["thread_id"])
    project = history.get_chat_project(goal["project_id"])
    system = (project.get("instructions") or "") + "\nWork persistently toward this goal: " + goal["objective"] + (
        "\nUse available tools, check results, and continue the next useful step. Stop for required user approval. "
        "Only after verifying completion, end your answer with [GOAL_COMPLETE]. Otherwise report progress and the next step."
    )
    replay = [{"role": "system", "content": system}]
    replay.extend({"role": m["role"], "content": message_text(m)} for m in messages if m["role"] in {"user", "assistant"})
    prompt = goal["objective"] if not messages else "Continue toward the saved goal from the recorded progress."
    replay.append({"role": "user", "content": prompt})
    run_id, user_id, assistant_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    payload = CreateChatGenerationRun(runId=run_id, threadId=goal["thread_id"], userMessageId=user_id,
        assistantMessageId=assistant_id, requestPayload={"model": goal["model"], "messages": replay,
        "stream": True, "max_tokens": goal["tokens_per_turn"], "enable_tools": True,
        "enabled_tools": ["python", "terminal", "edit_file", "view_image", "web_search", "search_knowledge_base"],
        "permission_mode": "auto", "max_tool_calls_per_message": 9999, "context_overflow": "truncate_oldest",
        "thread_id": goal["thread_id"], "session_id": "project-" + goal["project_id"],
        "rag_scope": {"project_id": goal["project_id"]}})
    sanitized = _sanitize_request(payload)
    sanitized.update(saved_provider_routing(goal["model"]))
    now = int(time.time()*1000)
    history.upsert_chat_message({"id": user_id, "threadId": goal["thread_id"], "role": "user",
                                "content": [{"type": "text", "text": prompt}], "createdAt": now})
    # Reserve BEFORE creating the run. A crash can consume a turn, but never exceed its limit.
    update_goal(goal["id"], turns=goal["turns"]+1, run_id=run_id)
    runs.create_run(run_id=run_id, owner_subject=goal["subject"], thread_id=goal["thread_id"],
                    user_message_id=user_id, assistant_message_id=assistant_id, request_payload=sanitized)
    # A pause request can race this synchronous worker. Re-check after publishing the run.
    latest = next((g for g in goals(goal["project_id"]) if g["id"] == goal["id"]), None)
    if not latest or latest["status"] != "running":
        runs.request_cancel(run_id)
        return
    app.state.chat_generation_supervisor.start(run_id, thread_id=goal["thread_id"], model=goal["model"])


async def goal_loop(app):
    from core.training.account_jobs import startup_reconciliation_accounts
    previous = time.monotonic()
    while True:
        await asyncio.sleep(2)
        now = time.monotonic()
        seconds, previous = now-previous, now
        for account in startup_reconciliation_accounts():
            goal = None
            try:
                pending = await asyncio.to_thread(run_as, account, goals)
                # One persistent goal per account at a time, preventing competing model runs.
                if pending:
                    goal = pending[0]
                    await arun_as(account, tick_goal(goal, app, seconds))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Persistent goal worker failed")
                if goal is not None:
                    await asyncio.to_thread(run_as, account, update_goal, goal["id"], status="error", error=str(exc)[:1000])


def start_goals(app):
    app.state.custom_goal_task = asyncio.create_task(goal_loop(app), name="persistent-project-goals")


async def stop_goals(app):
    task = getattr(app.state, "custom_goal_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class ResearchSteer(BaseModel):
    instruction: str = Field(min_length=1, max_length=20000)


@router.post("/research/{run_id}/steer")
def steer_research(run_id: str, payload: ResearchSteer):
    from storage.research_runs_db import add_steering, ResearchConflictError
    try:
        return add_steering(run_id, payload.instruction)
    except KeyError as exc:
        raise HTTPException(404, "Research run not found") from exc
    except ResearchConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
