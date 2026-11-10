import assert from "node:assert/strict";
import test from "node:test";

import {
  authenticatedFetch,
  AuthenticatedSessionUnavailable,
  createScopedAuthStorage,
} from "../src/authenticatedApi.js";

const accessToken = "synthetic-access-token";

function authWithSession(session) {
  return {
    getSession: async () => ({ data: { session }, error: null }),
  };
}

test("sends the current Supabase bearer token on the session request", async () => {
  let request;
  await authenticatedFetch(
    authWithSession({ access_token: accessToken }),
    "/api/v1/session/",
    {},
    async (url, options) => {
      request = { url, options };
      return new Response(null, { status: 200 });
    },
  );

  assert.equal(request.url, "/api/v1/session/");
  assert.equal(request.options.headers.get("Authorization"), `Bearer ${accessToken}`);
});

test("replaces a stale POST authorization header with the current Supabase token", async () => {
  let request;
  const body = "{}";
  await authenticatedFetch(
    authWithSession({ access_token: accessToken }),
    "/api/v1/session/password/",
    {
      method: "POST",
      headers: {
        Authorization: "Bearer stale-token",
        "Content-Type": "application/json",
      },
      body,
    },
    async (url, options) => {
      request = { url, options };
      return new Response(null, { status: 200 });
    },
  );

  assert.equal(request.url, "/api/v1/session/password/");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.headers.get("Authorization"), `Bearer ${accessToken}`);
  assert.equal(request.options.body, body);
});

test("does not send API requests without a Supabase session", async () => {
  let requestSent = false;

  await assert.rejects(
    authenticatedFetch(
      authWithSession(null),
      "/api/v1/session/password/",
      { method: "POST" },
      async () => {
        requestSent = true;
        return new Response(null, { status: 200 });
      },
    ),
    AuthenticatedSessionUnavailable,
  );

  assert.equal(requestSent, false);
});

test("clears only ContaRed's Supabase session keys", () => {
  const entries = new Map([
    ["sb-contared-auth-token", "session"],
    ["sb-contared-auth-token-user", "user"],
    ["sb-contared-auth-token-code-verifier", "verifier"],
    ["contared-direct-user-company-SV", "draft"],
    ["unrelated-app-data", "preserved"],
  ]);
  const storage = {
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, value),
    removeItem: (key) => entries.delete(key),
  };
  const scopedStorage = createScopedAuthStorage(storage, "sb-contared-auth-token");

  scopedStorage.clearSession();

  assert.deepEqual([...entries.keys()], [
    "contared-direct-user-company-SV",
    "unrelated-app-data",
  ]);
});
