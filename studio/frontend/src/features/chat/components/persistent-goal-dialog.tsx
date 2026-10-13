// SPDX-License-Identifier: AGPL-3.0-only
import { useState } from "react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { toast } from "@/lib/toast";
import { createChatProject } from "../hooks/use-chat-projects";
import { useChatRuntimeStore } from "../stores/chat-runtime-store";
import { ProjectToolsPanel } from "./project-tools-panel";

export function PersistentGoalDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const activeProjectId = useChatRuntimeStore(s => s.activeProjectId);
  const activeThreadId = useChatRuntimeStore(s => s.activeThreadId);
  const [createdProjectId, setCreatedProjectId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const projectId = activeProjectId || createdProjectId;
  async function create() {
    setBusy(true);
    try { setCreatedProjectId((await createChatProject("Persistent goals")).id); }
    catch (error) { toast.error(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }
  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
      <DialogHeader><DialogTitle>Persistent goal</DialogTitle><DialogDescription>
        Keep working toward an objective, with pause, stop and execution limits. Saved goals resume when Unsloth starts.
      </DialogDescription></DialogHeader>
      {projectId ? <ProjectToolsPanel key={`${projectId}:${activeThreadId ?? "new"}`} projectId={projectId}
        initialTab="goals" goalThreadId={activeProjectId ? activeThreadId ?? undefined : undefined} />
        : <div className="space-y-3"><p className="text-sm text-muted-foreground">Save this goal in a project so its workspace and progress survive restart.</p>
          <Button disabled={busy} onClick={() => void create()}>{busy ? "Creating…" : "Create persistent goals project"}</Button></div>}
    </DialogContent>
  </Dialog>;
}
