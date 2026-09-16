import assert from "node:assert/strict";
import test from "node:test";

import { resolveBaselinePath } from "./boundaries.mjs";

test("resolves a Windows governance path against the Linux project root", () => {
  assert.equal(
    resolveBaselinePath("D:\\Foresightnine\\nested\\plan.md", "/tmp/Foresightnine"),
    "/tmp/Foresightnine/nested/plan.md",
  );
});

test("resolves relative baseline paths against the project root", () => {
  assert.equal(resolveBaselinePath("plan.md", "/tmp/Foresightnine"), "/tmp/Foresightnine/plan.md");
});
