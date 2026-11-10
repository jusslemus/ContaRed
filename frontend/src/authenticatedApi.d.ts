import type { SupabaseClient } from "@supabase/supabase-js";

export class AuthenticatedSessionUnavailable extends Error {}

export function createScopedAuthStorage(
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">,
  storageKey: string,
): {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
  removeItem: (key: string) => void;
  clearSession: () => void;
};

export function authenticatedFetch(
  auth: Pick<SupabaseClient["auth"], "getSession">,
  input: RequestInfo | URL,
  init?: RequestInit,
  fetchImplementation?: typeof fetch,
): Promise<Response>;
