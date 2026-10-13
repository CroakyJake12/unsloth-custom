// SPDX-License-Identifier: AGPL-3.0-only
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { authFetch } from "@/features/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/lib/toast";
import { useChatRuntimeStore } from "../stores/chat-runtime-store";

export async function projectToolsRequest<T>(path: string, body?: unknown): Promise<T> {
  const response = await authFetch(`/api/project-tools${path}`, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Project operation failed");
  return data as T;
}

type GitState = { root: string; branch: string; files: { status: string; path: string }[]; diff: string; stagedDiff: string; remotes: string };
type Goal = { id: string; objective: string; status: string; turns: number; max_turns: number; elapsed: number;
  max_seconds: number; thread_id: string; error: string | null };

export function ProjectToolsPanel({ projectId }: { projectId: string }) {
  const navigate = useNavigate();
  const [tab, setTab] = useState<"git" | "goals">("git");
  const [git, setGit] = useState<GitState | null>(null);
  const [gitError, setGitError] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [url, setUrl] = useState("");
  const [message, setMessage] = useState("");
  const [branch, setBranch] = useState("");
  const [objective, setObjective] = useState("");
  const [model, setModel] = useState(() => useChatRuntimeStore.getState().params.checkpoint || "default");
  const [turns, setTurns] = useState(50);
  const [minutes, setMinutes] = useState(60);
  const [tokens, setTokens] = useState(8192);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const prefix = `/projects/${encodeURIComponent(projectId)}`;

  const refresh = useCallback(async () => {
    const [g, p] = await Promise.allSettled([
      projectToolsRequest<GitState>(`${prefix}/git`),
      projectToolsRequest<{ goals: Goal[] }>(`${prefix}/goals`),
    ]);
    if (g.status === "fulfilled") { setGit(g.value); setGitError(""); }
    else { setGit(null); setGitError(String(g.reason.message ?? g.reason)); }
    if (p.status === "fulfilled") { setGoals(p.value.goals); setError(""); }
    else setError(String(p.reason.message ?? p.reason));
  }, [prefix]);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 4000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function execute(path: string, body: unknown) {
    if (busy) return;
    setBusy(true);
    try {
      const result = await projectToolsRequest<{ warning?: string; output?: string }>(path, body);
      if (result.warning) toast.warning(result.warning);
      else toast.success(result.output?.trim() || "Saved");
      setSelected([]);
      await refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally { setBusy(false); }
  }
  function action(operation: string, extra: Record<string, unknown> = {}) {
    void execute(`${prefix}/git`, { operation, ...extra });
  }

  return <section className="space-y-4 rounded-[22px] border border-border bg-muted/20 p-4" aria-label="Project Git and goals">
    <div className="flex flex-wrap items-center gap-2">
      <Button variant={tab === "git" ? "default" : "ghost"} size="sm" onClick={() => setTab("git")}>Git &amp; GitHub</Button>
      <Button variant={tab === "goals" ? "default" : "ghost"} size="sm" onClick={() => setTab("goals")}>Persistent goals</Button>
      <Button variant="ghost" size="sm" disabled={busy} onClick={() => void refresh()}>Refresh</Button>
    </div>
    {tab === "git" ? <div className="space-y-3">
      <p className="text-sm text-muted-foreground">Clone a GitHub repository into this project. Its files become the working folder and are indexed as sources.</p>
      <div className="flex gap-2"><Input aria-label="GitHub repository URL" placeholder="https://github.com/owner/repository" value={url} onChange={e => setUrl(e.target.value)} />
        <Button disabled={busy || !url.trim()} onClick={() => void execute(`${prefix}/github`, { url, indexSources: true })}>{busy ? "Working…" : "Add repository"}</Button></div>
      {git ? <>
        <p className="break-all text-xs text-muted-foreground">{git.root} · {git.branch}</p>
        <div className="max-h-52 space-y-1 overflow-auto" aria-label="Changed files">{git.files.length === 0 ? <p className="text-sm">Working tree clean.</p> : git.files.map(file =>
          <label key={file.path} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={selected.includes(file.path)}
            onChange={e => setSelected(prev => e.target.checked ? [...prev, file.path] : prev.filter(p => p !== file.path))} />
            <code>{file.status}</code><span className="break-all">{file.path}</span></label>)}</div>
        <div className="flex flex-wrap gap-2"><Button size="sm" disabled={busy || !selected.length} onClick={() => action("stage", { paths: selected })}>Stage selected</Button>
          <Button size="sm" variant="outline" disabled={busy || !selected.length} onClick={() => action("unstage", { paths: selected })}>Unstage selected</Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => action("fetch")}>Fetch</Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => action("pull")}>Pull (fast-forward)</Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => { if (window.confirm("Push this project's current branch to origin?")) action("push"); }}>Push…</Button></div>
        <div className="flex gap-2"><Input aria-label="Commit message" placeholder="Commit message" value={message} onChange={e => setMessage(e.target.value)} />
          <Button disabled={busy || !message.trim()} onClick={() => { if (window.confirm("Commit the staged changes with this message?")) action("commit", { message }); }}>Commit staged…</Button></div>
        <div className="flex gap-2"><Input aria-label="Branch name" placeholder="Branch name" value={branch} onChange={e => setBranch(e.target.value)} />
          <Button variant="outline" disabled={busy || !branch} onClick={() => action("branch", { branch })}>Create branch</Button>
          <Button variant="outline" disabled={busy || !branch} onClick={() => action("checkout", { branch })}>Switch</Button></div>
        <details><summary className="cursor-pointer text-sm">Review changes</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs">{git.diff || "No unstaged diff."}{"\n\nStaged:\n"}{git.stagedDiff || "No staged diff."}</pre></details>
      </> : <div className="space-y-2"><p className="text-xs text-muted-foreground">{gitError}</p><Button variant="outline" disabled={busy} onClick={() => action("init")}>Initialize Git in working folder</Button></div>}
    </div> : <div className="space-y-3">
      <p className="text-sm text-muted-foreground">Goals keep working with the selected model and resume when Studio restarts. Saved providers retain their configured connection. Tool approvals and sandbox permissions stay active. Completion is reported by the model in the saved chat.</p>
      <Textarea aria-label="Goal objective" placeholder="What should this project accomplish? Include how to verify completion." value={objective} onChange={e => setObjective(e.target.value)} />
      <div className="grid grid-cols-2 gap-2">
        <label className="text-xs">Model (current selection by default)<Input aria-label="Goal model" value={model} onChange={e => setModel(e.target.value)} /></label>
        <label className="text-xs">Maximum turns<Input aria-label="Goal turn limit" type="number" min={1} max={10000} value={turns} onChange={e => setTurns(Number(e.target.value))} /></label>
        <label className="text-xs">Active time (minutes)<Input aria-label="Goal time limit" type="number" min={1} max={10080} value={minutes} onChange={e => setMinutes(Number(e.target.value))} /></label>
        <label className="text-xs">Output tokens per turn<Input aria-label="Goal token limit" type="number" min={64} max={131072} value={tokens} onChange={e => setTokens(Number(e.target.value))} /></label>
      </div>
      <Button disabled={busy || !objective.trim() || turns < 1 || minutes < 1 || tokens < 64} onClick={() => void execute(`${prefix}/goals`, { objective, model, maxTurns: turns, maxSeconds: minutes*60, tokensPerTurn: tokens })}>Start persistent goal</Button>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {goals.map(goal => <article key={goal.id} className="space-y-2 rounded-xl border border-border p-3">
        <p className="whitespace-pre-wrap text-sm">{goal.objective}</p>
        <p className="text-xs text-muted-foreground">{goal.status} · {goal.turns}/{goal.max_turns} turns · {Math.round(goal.elapsed)}/{goal.max_seconds}s active</p>
        {goal.error && <p role="alert" className="text-xs text-destructive">{goal.error}</p>}
        <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" onClick={() => navigate({ to: "/chat", search: { thread: goal.thread_id, project: projectId } })}>Open goal chat</Button>
          <Button size="sm" variant="outline" disabled={busy || !["running", "paused", "error"].includes(goal.status)} onClick={() => void execute(`${prefix}/goals/${goal.id}`, { action: goal.status === "running" ? "pause" : "resume" })}>{goal.status === "running" ? "Pause" : "Resume"}</Button>
          <Button size="sm" variant="outline" disabled={busy || ["completed", "stopped"].includes(goal.status)} onClick={() => void execute(`${prefix}/goals/${goal.id}`, { action: "stop" })}>Stop</Button></div>
      </article>)}
    </div>}
  </section>;
}
