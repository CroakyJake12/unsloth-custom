// SPDX-License-Identifier: AGPL-3.0-only
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/lib/toast";
import { projectToolsRequest } from "./project-tools-panel";

export function ResearchSteering({ runId }: { runId: string }) {
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  async function steer() {
    if (busy || !instruction.trim()) return;
    setBusy(true);
    try {
      await projectToolsRequest(`/research/${encodeURIComponent(runId)}/steer`, { instruction });
      setInstruction("");
      toast.success("Research instructions saved", { description: "The researcher applies them at the next step or synthesis pass." });
    } catch (err) { toast.error(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  }
  return <div className="mt-3 space-y-2 rounded-xl border border-border p-3">
    <label htmlFor={`research-steer-${runId}`} className="text-sm font-medium">Steer research</label>
    <Textarea id={`research-steer-${runId}`} rows={2} value={instruction} onChange={e => setInstruction(e.target.value)}
      disabled={busy} placeholder="Add a constraint, correct the direction, or request another source…"
      onKeyDown={e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void steer(); } }} />
    <Button size="sm" disabled={busy || !instruction.trim()} onClick={() => void steer()}>{busy ? "Saving…" : "Send steering instruction"}</Button>
  </div>;
}
