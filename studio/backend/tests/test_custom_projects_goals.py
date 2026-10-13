"""Real repository/storage regressions for the custom build; inference is deterministic."""
import asyncio
import json
import threading
from types import SimpleNamespace
from pathlib import Path
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routes import project_tools as tools
from storage import studio_db as history
from storage import chat_generation_runs_db as runs
from storage import research_runs_db as research


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("UNSLOTH_STUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("UNSLOTH_STUDIO_PROJECTS_HOME", str(tmp_path / "projects"))
    history._schema_ready.clear()
    p = history.upsert_chat_project({"id": "p", "name": "Custom test", "createdAt": 1, "updatedAt": 1})
    root = tools.project_folder("p")
    app = SimpleNamespace(state=SimpleNamespace(chat_generation_supervisor=SimpleNamespace(start=lambda *a, **k: None, cancel=lambda *a: None)))
    return SimpleNamespace(root=root, app=app, tmp=tmp_path, monkeypatch=monkeypatch)


def test_authentication_is_required(env):
    app = FastAPI()
    app.include_router(tools.router)
    with TestClient(app) as client:
        response = client.get("/projects/p/goals")
    assert response.status_code in {401, 403}


def test_git_stage_commit_diff_and_branch_on_real_repository(env):
    tools.git_action("p", tools.GitAction(operation="init"))
    tools.git(env.root, ["config", "user.name", "Custom test"])
    tools.git(env.root, ["config", "user.email", "test@example.invalid"])
    (env.root / "example.txt").write_text("first\n")
    status = tools.git_status("p")
    assert any(f["path"] == "example.txt" for f in status["files"])
    tools.git_action("p", tools.GitAction(operation="stage", paths=["example.txt"]))
    assert "+first" in tools.git_status("p")["stagedDiff"]
    tools.git_action("p", tools.GitAction(operation="commit", message="First"))
    tools.git_action("p", tools.GitAction(operation="branch", branch="feature/custom"))
    assert tools.git_status("p")["branch"] == "feature/custom"
    (env.root / "example.txt").write_text("second\n")
    assert "+second" in tools.git_status("p")["diff"]


@pytest.mark.parametrize("path", ["../outside", ".git/config", "-bad", "C:/Windows/test"])
def test_git_paths_cannot_escape_repository(env, path):
    with pytest.raises(HTTPException):
        tools.checked_paths(env.root, [path])


@pytest.mark.parametrize("url", ["https://example.com/repo", "https://user:secret@github.com/owner/repo", "file:///repo", "--upload-pack=evil"])
def test_clone_rejects_non_github_and_credentials(env, url):
    with pytest.raises(HTTPException) as exc:
        tools.clone_repository("p", tools.CloneRepository(url=url))
    assert exc.value.status_code == 400


def test_goal_storage_survives_new_connection_and_preserves_existing_settings(env):
    with history.get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings(key,value_json,updated_at) VALUES ('custom-test', '\"keep\"', 1)")
    goal = tools.create_goal("p", tools.CreateGoal(objective="Write and verify a file", maxTurns=3), "unsloth")
    assert tools.goals("p")[0]["id"] == goal["id"]
    with history.get_connection() as conn:
        assert conn.execute("SELECT value_json FROM app_settings WHERE key='custom-test'").fetchone()[0] == '"keep"'
    tools.update_goal(goal["id"], status="paused")
    assert not tools.goals()
    tools.update_goal(goal["id"], status="running")
    assert tools.goals()[0]["id"] == goal["id"]


def test_goal_time_and_turn_limits_do_not_start_extra_runs(env):
    goal = tools.create_goal("p", tools.CreateGoal(objective="Work", maxTurns=1, maxSeconds=10), "unsloth")
    asyncio.run(tools.tick_goal(goal, env.app, 11))
    assert tools.goals("p")[0]["status"] == "limit"
    tools.update_goal(goal["id"], status="running", elapsed=0, turns=1)
    asyncio.run(tools.tick_goal(tools.goals("p")[0], env.app, 0))
    assert tools.goals("p")[0]["status"] == "limit"
    assert tools.goals("p")[0]["run_id"] is None


def test_goal_dispatch_uses_durable_saved_chat_and_bounded_permission_mode(env):
    goal = tools.create_goal("p", tools.CreateGoal(objective="Work", maxTurns=2), "unsloth")
    asyncio.run(tools.tick_goal(goal, env.app, 0))
    saved = tools.goals("p")[0]
    run = runs.get_run(saved["run_id"])
    assert run["status"] == "queued"
    assert run["requestPayload"]["permission_mode"] == "auto"
    assert run["requestPayload"]["max_tool_calls_per_message"] == 9999
    assert saved["turns"] == 1
    assert run["requestPayload"]["session_id"] == "project-p"
    assert history.get_chat_message(goal["thread_id"], run["userMessageId"])
    assert history.get_chat_message(goal["thread_id"], run["assistantMessageId"])
    # Simulate app restart: re-read the saved state, then tick without dispatching another run.
    asyncio.run(tools.tick_goal(tools.goals("p")[0], env.app, 0))
    assert tools.goals("p")[0]["turns"] == 1


def test_goals_preserve_saved_provider_reference_without_credentials(env):
    from storage import providers_db
    providers_db.create_provider("fixture", "custom", "Fixture", "http://127.0.0.1:8900/v1", models=["fixture"])
    goal = tools.create_goal("p", tools.CreateGoal(objective="Work", model="external::fixture::fixture"), "unsloth")
    asyncio.run(tools.tick_goal(goal, env.app, 0))
    run = runs.get_run(tools.goals("p")[0]["run_id"])
    assert run["requestPayload"]["provider_id"] == "fixture"
    assert run["requestPayload"]["external_model"] == "fixture"
    assert "encrypted_api_key" not in run["requestPayload"]
    assert "provider_base_url" not in run["requestPayload"]


def test_goal_can_continue_current_project_chat_without_replacing_history(env):
    history.upsert_chat_thread({"id": "current", "title": "Keep title", "modelType": "model1", "modelId": "default", "projectId": "p", "createdAt": 1})
    history.upsert_chat_message({"id": "prior", "threadId": "current", "role": "user", "createdAt": 1, "content": [{"type": "text", "text": "Existing work"}]})
    goal = tools.create_goal("p", tools.CreateGoal(objective="Continue this work", threadId="current"), "unsloth")
    asyncio.run(tools.tick_goal(goal, env.app, 0))
    assert history.get_chat_thread("current")["title"] == "Keep title"
    assert history.get_chat_message("current", "prior")
    run = runs.get_run(tools.goals("p")[0]["run_id"])
    assert run["threadId"] == "current"
    assert any(m["content"] == "Existing work" for m in run["requestPayload"]["messages"])


def test_goal_cannot_reuse_chat_in_another_project(env):
    history.upsert_chat_thread({"id": "other", "title": "Other", "modelType": "model1", "modelId": "default", "createdAt": 1})
    with pytest.raises(HTTPException):
        tools.create_goal("p", tools.CreateGoal(objective="Work", threadId="other"), "unsloth")


def test_research_steering_is_durable_and_retains_evidence(env):
    history.upsert_chat_thread({"id": "research-thread", "title": "Research", "modelType": "model1", "modelId": "default", "createdAt": 1})
    history.upsert_chat_message({"id": "question", "threadId": "research-thread", "role": "user", "createdAt": 1, "content": [{"type": "text", "text": "Question"}]})
    research.create_run(run_id="research-test", owner_subject="unsloth", thread_id="research-thread", user_message_id="question", assistant_message_id=None, config={"instructions": "Original"})
    research.add_steering("research-test", "Use primary sources")
    research.add_steering("research-test", "Focus on Windows")
    saved = research.get_run("research-test")
    assert saved["config"]["instructions"] == "Original"
    assert [s["text"] for s in saved["config"]["steeringInstructions"]] == ["Use primary sources", "Focus on Windows"]
    assert saved["status"] == "planning"
    from core.research_runs import _refresh_user_steering
    asyncio.run(_refresh_user_steering(saved))
    assert "Use primary sources" in saved["config"]["instructions"]
    assert "Focus on Windows" in saved["config"]["instructions"]


def test_tool_loop_runs_past_old_iteration_and_parallel_limits_and_can_cancel(env):
    from core.inference.safetensors_agentic import run_safetensors_tool_loop
    calls = []
    round_number = [0]
    def turn(messages):
        round_number[0] += 1
        if round_number[0] <= 35:
            yield '<tool_call>' + json.dumps({"name": "web_search", "arguments": {"query": str(round_number[0])}}) + '</tool_call>'
        else:
            yield "Verified final answer."
    def execute(name, args, **kwargs):
        calls.append((name, args))
        return "Result " + args["query"]
    events = list(run_safetensors_tool_loop(single_turn=turn, messages=[{"role": "user", "content": "Research"}],
        tools=[{"type": "function", "function": {"name": "web_search"}}], execute_tool=execute,
        max_tool_iterations=1, permission_mode="off", deduplicate_tool_calls=False))
    assert len(calls) == 35
    assert any(e.get("text") == "Verified final answer." for e in events)
    cancel = threading.Event()
    cancel.set()
    calls.clear()
    list(run_safetensors_tool_loop(single_turn=turn, messages=[{"role": "user", "content": "Research"}],
        tools=[{"type": "function", "function": {"name": "web_search"}}], execute_tool=execute,
        max_tool_iterations=9999, cancel_event=cancel, permission_mode="off"))
    assert not calls


def test_parallel_tool_calls_are_not_cut_at_eight(env):
    from core.inference.safetensors_agentic import run_safetensors_tool_loop
    replies = iter(["".join('<tool_call>' + json.dumps({"name": "web_search", "arguments": {"query": str(i)}}) + '</tool_call>' for i in range(12)), "Done."])
    calls = []
    def turn(messages):
        yield next(replies)
    def execute(name, args, **kwargs):
        calls.append(args)
        return "OK"
    list(run_safetensors_tool_loop(single_turn=turn, messages=[{"role": "user", "content": "Search"}],
         tools=[{"type": "function", "function": {"name": "web_search"}}], execute_tool=execute,
         permission_mode="off", max_tool_iterations=1, deduplicate_tool_calls=False))
    assert len(calls) == 12


def test_goal_completion_is_read_from_durable_assistant_message(env):
    goal = tools.create_goal("p", tools.CreateGoal(objective="Work"), "unsloth")
    asyncio.run(tools.tick_goal(goal, env.app, 0))
    saved = tools.goals("p")[0]
    run = runs.get_run(saved["run_id"])
    with history.get_connection() as conn:
        conn.execute("UPDATE chat_messages SET content_json=? WHERE id=?", (json.dumps([{"type": "text", "text": "Verified. [GOAL_COMPLETE]"}]), run["assistantMessageId"]))
        conn.execute("UPDATE chat_generation_runs SET status='completed' WHERE id=?", (run["id"],))
    asyncio.run(tools.tick_goal(tools.goals("p")[0], env.app, 0))
    assert tools.goals("p")[0]["status"] == "completed"


def test_goal_replays_headless_chunks_into_saved_history(env):
    goal = tools.create_goal("p", tools.CreateGoal(objective="Work"), "unsloth")
    asyncio.run(tools.tick_goal(goal, env.app, 0))
    saved = tools.goals("p")[0]
    run = runs.get_run(saved["run_id"])
    with history.get_connection() as conn:
        conn.execute("INSERT INTO chat_generation_events(run_id,seq,event_type,payload_json,created_at) VALUES (?,?,?,?,?)",
                     (run["id"], 2, "chunk", json.dumps({"choices": [{"delta": {"content": "Verified. [GOAL_COMPLETE]"}}]}), 1))
        conn.execute("UPDATE chat_generation_runs SET status='completed',last_event_seq=2 WHERE id=?", (run["id"],))
        conn.execute("UPDATE chat_messages SET metadata_json=? WHERE id=?", (json.dumps({"generationRunId": run["id"], "generationSeq": 0, "generationStatus": "completed", "serverManaged": True}), run["assistantMessageId"]))
    asyncio.run(tools.tick_goal(tools.goals("p")[0], env.app, 0))
    assert tools.goals("p")[0]["status"] == "completed"
    message = history.get_chat_message(goal["thread_id"], run["assistantMessageId"])
    assert tools.message_text(message) == "Verified. [GOAL_COMPLETE]"


def test_linked_folder_can_scan_more_than_ten_thousand_files(env):
    from core.rag import config, folder_sync
    root = env.tmp / "large-source"
    root.mkdir()
    for i in range(10001):
        (root / f"source{i:05d}.txt").write_text("source\n")
    (root / ".env").write_text("secret=must-not-ingest\n")
    report = {}
    found, _ = folder_sync._scan(str(root), report=report)
    assert config.FOLDER_MAX_FILES == 0
    assert len(found) == 10001
    assert ".env" not in found
    assert report["over_limit"] == 0
