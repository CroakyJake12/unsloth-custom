import assert from "node:assert/strict";
import test from "node:test";
import { filesFromFolderInput } from "../src/lib/dropped-folders.ts";

test("folder import keeps more than the old 2000-file cap and still excludes secrets", () => {
  const files = Array.from({ length: 2501 }, (_, i) => ({ name: `file${i}.ts`, size: 10, webkitRelativePath: `repo/file${i}.ts` })) as unknown as File[];
  files.push({ name: ".env", size: 10, webkitRelativePath: "repo/.env" } as File);
  const result = filesFromFolderInput(files);
  assert.equal(result.files.length, 2501);
  assert.equal(result.truncated, 0);
  assert.ok(result.files.every(file => file.name !== ".env"));
});
