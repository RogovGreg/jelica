import assert from "node:assert/strict";
import test from "node:test";

import { resolveUploadSession } from "../../web/lib/uploads/session";

type Session = Readonly<{
  id: string;
  submission_status: "open" | "submitting" | "consumed";
}>;

test("upload session bootstrap restores an open stored session", async () => {
  let createCalls = 0;
  const restored = await resolveUploadSession<Session>({
    explicitSessionId: null,
    storedSessionId: "stored",
    getSession: async (id) => ({ id, submission_status: "open" }),
    createSession: async () => {
      createCalls += 1;
      return { id: "new", submission_status: "open" };
    },
    isStoredSessionUnavailable: () => false,
  });

  assert.equal(restored.id, "stored");
  assert.equal(createCalls, 0);
});

test("upload session bootstrap replaces non-open and unavailable stored sessions", async () => {
  for (const mode of ["submitting", "consumed", "unavailable"] as const) {
    const restored = await resolveUploadSession<Session>({
      explicitSessionId: null,
      storedSessionId: "stale",
      getSession: async () => {
        if (mode === "unavailable") throw new Error("missing");
        return { id: "stale", submission_status: mode };
      },
      createSession: async () => ({ id: "new", submission_status: "open" }),
      isStoredSessionUnavailable: () => mode === "unavailable",
    });

    assert.equal(restored.id, "new");
  }
});

test("upload session bootstrap does not hide service failures or replace explicit sessions", async () => {
  await assert.rejects(
    resolveUploadSession<Session>({
      explicitSessionId: null,
      storedSessionId: "stored",
      getSession: async () => {
        throw new Error("service unavailable");
      },
      createSession: async () => ({ id: "new", submission_status: "open" }),
      isStoredSessionUnavailable: () => false,
    }),
    /service unavailable/,
  );

  const explicit = await resolveUploadSession<Session>({
    explicitSessionId: "explicit",
    storedSessionId: "stored",
    getSession: async (id) => ({ id, submission_status: "consumed" }),
    createSession: async () => ({ id: "new", submission_status: "open" }),
    isStoredSessionUnavailable: () => false,
  });
  assert.equal(explicit.id, "explicit");
  assert.equal(explicit.submission_status, "consumed");
});
