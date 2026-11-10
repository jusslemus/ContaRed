export class AuthenticatedSessionUnavailable extends Error {
  constructor() {
    super("La sesión de Supabase venció o no está disponible. Inicia sesión de nuevo para continuar.");
    this.name = "AuthenticatedSessionUnavailable";
  }
}

export function createScopedAuthStorage(storage, storageKey) {
  return {
    getItem: (key) => storage.getItem(key),
    setItem: (key, value) => storage.setItem(key, value),
    removeItem: (key) => storage.removeItem(key),
    clearSession() {
      const keys = [
        storageKey,
        `${storageKey}-user`,
        `${storageKey}-code-verifier`,
      ];
      let firstError;
      for (const key of keys) {
        try {
          storage.removeItem(key);
        } catch (error) {
          firstError ??= error;
        }
      }
      if (firstError) throw firstError;
    },
  };
}

export async function authenticatedFetch(auth, input, init = {}, fetchImplementation = fetch) {
  const { data, error } = await auth.getSession();
  const accessToken = data.session?.access_token;
  if (error || !accessToken) throw new AuthenticatedSessionUnavailable();

  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${accessToken}`);
  return fetchImplementation(input, { ...init, headers });
}
