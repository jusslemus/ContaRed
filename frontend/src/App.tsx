import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { createClient, type Session, type SupabaseClient } from "@supabase/supabase-js";
import logoUrl from "../../backend/img/logo.jpg";
import { authenticatedFetch, AuthenticatedSessionUnavailable, createScopedAuthStorage } from "./authenticatedApi";
import NavigationSidebar, { type Section } from "./components/NavigationSidebar";
import ContentHeader from "./components/ContentHeader";
import BankingModule from "./components/BankingModule";
import DirectReconciliationModule from "./components/DirectReconciliationModule";
import UserAdministrationModule from "./components/UserAdministrationModule";

type AuthorizedAccess = {
  empresa_id: string;
  empresa: string;
  pais_codigo: "SV" | "GT";
  pais: string;
  rol: "administrador" | "auxiliar" | "revisor" | "consulta";
};
type SessionInfo = { usuario: { id: string; nombre: string }; accesos: AuthorizedAccess[]; paises: { codigo: "SV" | "GT"; nombre: string }[]; cambio_clave_obligatorio: boolean };
type BackendState = { kind: "checking" } | { kind: "ready"; data: SessionInfo } | { kind: "blocked"; message: string; errorCode: string } | { kind: "error"; message: string };
const supabaseUrl = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const supabaseKey = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY as string | undefined;
const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "");
const SESSION_VALIDATION_TIMEOUT_MS = 15_000;
const AUTH_LOGOUT_TIMEOUT_MS = 5_000;
const supabaseStorageKey = supabaseUrl
  ? `sb-${new URL(supabaseUrl).hostname.split(".")[0]}-auth-token`
  : "";
const supabaseStorage = typeof window !== "undefined" && supabaseStorageKey
  ? createScopedAuthStorage(window.localStorage, supabaseStorageKey)
  : null;
const supabase: SupabaseClient | null = supabaseUrl && supabaseKey && supabaseStorage
  ? createClient(supabaseUrl, supabaseKey, {
    auth: { storageKey: supabaseStorageKey, storage: supabaseStorage },
    global: {
      fetch: async (input, init) => {
        const controller = new AbortController();
        const requestUrl = input instanceof Request
          ? input.url
          : input instanceof URL
            ? input.href
            : input;
        const timeoutMs = requestUrl.includes("/auth/v1/logout")
          ? AUTH_LOGOUT_TIMEOUT_MS
          : SESSION_VALIDATION_TIMEOUT_MS;
        const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
        const abortFromCaller = () => controller.abort();
        if (init?.signal?.aborted) controller.abort();
        else init?.signal?.addEventListener("abort", abortFromCaller, { once: true });
        try {
          return await fetch(input, { ...init, signal: controller.signal });
        } finally {
          window.clearTimeout(timeout);
          init?.signal?.removeEventListener("abort", abortFromCaller);
        }
      },
    },
  })
  : null;

function payloadMessage(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const record = payload as Record<string, unknown>;
  if (typeof record.detail === "string") return record.detail;
  if (typeof record.message === "string") return record.message;
  return null;
}
function payloadCode(payload: unknown): string | null {
  if (typeof payload !== "object" || payload === null) return null;
  const code = (payload as Record<string, unknown>).code;
  return typeof code === "string" ? code : null;
}
function isSessionInfo(payload: unknown): payload is SessionInfo {
  if (typeof payload !== "object" || payload === null) return false;
  const record = payload as Record<string, unknown>;
  return typeof record.usuario === "object" && record.usuario !== null && Array.isArray(record.accesos) && Array.isArray(record.paises) && typeof record.cambio_clave_obligatorio === "boolean";
}
function backendError(code: string | null, fallback: string) {
  switch (code) {
    case "profile_not_found": return "Tu usuario de Supabase no está vinculado a un perfil de ContaRed. Contacta al administrador.";
    case "profile_inactive": return "Tu perfil de ContaRed está inactivo. Contacta al administrador.";
    case "no_access": return "Tu perfil no tiene accesos activos a empresas de El Salvador o Guatemala.";
    case "database_unavailable": return "No se pudo consultar la base de ContaRed. Verifica la conexión del backend.";
    case "identity_provider_unavailable": return "No se pudo validar la sesión con Supabase. Intenta de nuevo más tarde.";
    case "authentication_failed": case "not_authenticated": return "Tu sesión ya no es válida. Cierra sesión e inicia nuevamente.";
    default: return fallback;
  }
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authRecoveryAttempt, setAuthRecoveryAttempt] = useState(0);
  const [authRecoveryFailed, setAuthRecoveryFailed] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const [backend, setBackend] = useState<BackendState>({ kind: "checking" });
  const [backendUserId, setBackendUserId] = useState("");
  const [sessionRefreshError, setSessionRefreshError] = useState("");
  const [sessionRefresh, setSessionRefresh] = useState(0);
  const [passwordSetupRequired, setPasswordSetupRequired] = useState(() => new URLSearchParams(window.location.search).get("establecer-clave") === "1");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [passwordSetupError, setPasswordSetupError] = useState("");
  const [passwordSetupSaving, setPasswordSetupSaving] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  const [signingOut, setSigningOut] = useState(false);
  const [selectedCountry, setSelectedCountry] = useState<"SV" | "GT" | "">("");
  const [selectedCompanyId, setSelectedCompanyId] = useState("");
  const [activeNav, setActiveNav] = useState<Section>("Nueva conciliación");
  const [visitedModules, setVisitedModules] = useState<Set<Section>>(() => new Set<Section>(["Cuentas"]));
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [historyAccountId, setHistoryAccountId] = useState("");
  const [historyBankId, setHistoryBankId] = useState("");
  const [newReconciliationAccountId, setNewReconciliationAccountId] = useState("");
  const sessionRef = useRef<Session | null>(null);
  const authorizedIdentity = useRef("");
  const authGeneration = useRef(0);
  const passwordSetupRequest = useRef<AbortController | null>(null);
  const lastSessionValidation = useRef(0);
  const workspaceScroll = useRef(0);

  function clearAuthenticatedState() {
    authGeneration.current += 1;
    passwordSetupRequest.current?.abort();
    passwordSetupRequest.current = null;
    sessionRef.current = null;
    authorizedIdentity.current = "";
    setSession(null);
    setBackend({ kind: "checking" });
    setBackendUserId("");
    setSessionRefreshError("");
    setSelectedCountry("");
    setSelectedCompanyId("");
    setActiveNav("Cuentas");
    setVisitedModules(new Set<Section>(["Cuentas"]));
    setMobileNavOpen(false);
    setHistoryAccountId("");
    setHistoryBankId("");
    setNewReconciliationAccountId("");
    setPasswordSetupRequired(false);
    setCurrentPassword("");
    setNewPassword("");
    setConfirmPassword("");
    setPasswordSetupError("");
    setPasswordSetupSaving(false);
    setEmail("");
    setPassword("");
    workspaceScroll.current = 0;
    window.history.replaceState(null, "", window.location.pathname);
    window.scrollTo(0, 0);
  }

  useEffect(() => {
    if (!supabase) { setAuthLoading(false); setBackend({ kind: "error", message: "Falta configurar Supabase en frontend/.env." }); return; }
    let active = true;
    let authEventReceived = false;
    let recoveryInProgress = true;
    setAuthLoading(true);
    setAuthRecoveryFailed(false);
    setFormError("");
    const recoveryTimeout = window.setTimeout(() => {
      if (!active) return;
      recoveryInProgress = false;
      if (!authEventReceived) {
        setAuthRecoveryFailed(true);
        setFormError("No se pudo recuperar la sesión de Supabase a tiempo. Reintenta o inicia sesión nuevamente.");
      }
      setAuthLoading(false);
    }, SESSION_VALIDATION_TIMEOUT_MS);
    function applySession(nextSession: Session | null) {
      const previousIdentity = sessionRef.current?.user.id ?? "";
      const nextIdentity = nextSession?.user.id ?? "";
      sessionRef.current = nextSession;
      if (previousIdentity !== nextIdentity) {
        authGeneration.current += 1;
        setLogoutError("");
        if (previousIdentity || !nextIdentity) {
          setPasswordSetupRequired(false);
          setCurrentPassword("");
          setNewPassword("");
          setConfirmPassword("");
          setPasswordSetupError("");
          setEmail("");
          setPassword("");
        }
        authorizedIdentity.current = "";
        setBackendUserId("");
        setBackend({ kind: "checking" });
        setSessionRefreshError("");
        setSelectedCountry("");
        setSelectedCompanyId("");
        setActiveNav("Cuentas");
        setVisitedModules(new Set<Section>(["Cuentas"]));
        setMobileNavOpen(false);
        setHistoryAccountId("");
        setHistoryBankId("");
        setNewReconciliationAccountId("");
        workspaceScroll.current = 0;
      }
      setSession(nextSession);
    }
    // Keep this callback synchronous: Supabase awaits auth subscribers while holding its auth lock.
    const subscription = supabase.auth.onAuthStateChange((_event, nextSession) => {
      if (!active) return;
      authEventReceived = true;
      window.clearTimeout(recoveryTimeout);
      setAuthRecoveryFailed(false);
      applySession(nextSession);
      setAuthLoading(false);
    });
    void supabase.auth.getSession().then(({ data, error }) => {
      if (!active) return;
      window.clearTimeout(recoveryTimeout);
      if (error) setFormError(`No se pudo recuperar la sesión: ${error.message}`);
      if (error) setAuthRecoveryFailed(true);
      if (!authEventReceived) applySession(data.session);
      recoveryInProgress = false;
      setAuthLoading(false);
    }).catch((error: unknown) => {
      window.clearTimeout(recoveryTimeout);
      recoveryInProgress = false;
      if (active) {
        setAuthRecoveryFailed(true);
        setFormError(`No se pudo recuperar la sesión: ${error instanceof Error ? error.message : "Error desconocido."}`);
        setAuthLoading(false);
      }
    });
    const handlePageShow = () => {
      if (!active || recoveryInProgress) return;
      setAuthLoading(true);
      void supabase.auth.getSession().then(({ data, error }) => {
        if (!active) return;
        if (error) {
          try {
            supabaseStorage?.clearSession();
            clearAuthenticatedState();
          } catch {
            setSession(null);
            sessionRef.current = null;
          }
          setFormError("No se pudo verificar la sesión al volver a esta página. Inicia sesión nuevamente.");
          setAuthRecoveryFailed(true);
          setAuthLoading(false);
          return;
        }
        applySession(data.session);
        setAuthLoading(false);
      }).catch(() => {
        if (active) {
          try {
            supabaseStorage?.clearSession();
            clearAuthenticatedState();
          } catch {
            setSession(null);
            sessionRef.current = null;
          }
          setFormError("No se pudo verificar la sesión al volver a esta página. Inicia sesión nuevamente.");
          setAuthRecoveryFailed(true);
          setAuthLoading(false);
        }
      });
    };
    window.addEventListener("pageshow", handlePageShow);
    return () => {
      active = false;
      window.clearTimeout(recoveryTimeout);
      window.removeEventListener("pageshow", handlePageShow);
      subscription.data.subscription.unsubscribe();
    };
  }, [authRecoveryAttempt]);

  useEffect(() => {
    if (!session) {
      authorizedIdentity.current = "";
      setBackendUserId("");
      setBackend({ kind: "checking" });
      setSessionRefreshError("");
      setSelectedCountry("");
      setSelectedCompanyId("");
      return;
    }
    if (!supabase) {
      setBackend({ kind: "error", message: "Falta configurar Supabase en frontend/.env." });
      return;
    }
    const identity = session.user.id;
    const token = session.access_token;
    const generation = authGeneration.current;
    const hasAuthorizedWorkspace = authorizedIdentity.current === identity;
    if (!apiBaseUrl) {
      const message = "Falta configurar VITE_API_BASE_URL en frontend/.env.";
      if (hasAuthorizedWorkspace) setSessionRefreshError(message);
      else setBackend({ kind: "error", message });
      return;
    }
    const controller = new AbortController();
    let timedOut = false;
    if (!hasAuthorizedWorkspace) setBackend({ kind: "checking" });
    lastSessionValidation.current = Date.now();
    setSessionRefreshError("");
    const requestTimeout = window.setTimeout(() => {
      if (controller.signal.aborted) return;
      timedOut = true;
      controller.abort();
      if (authGeneration.current !== generation
        || sessionRef.current?.user.id !== identity
        || sessionRef.current.access_token !== token) return;
      const message = "La validación de la sesión autorizada agotó el tiempo de espera. Comprueba el backend y reintenta.";
      if (hasAuthorizedWorkspace) setSessionRefreshError(message);
      else setBackend({ kind: "error", message });
    }, SESSION_VALIDATION_TIMEOUT_MS);
    const timer = window.setTimeout(() => { void authenticatedFetch(supabase.auth, `${apiBaseUrl}/api/v1/session/`, { signal: controller.signal }).then(async (response) => {
      const payload: unknown = await response.json().catch(() => null);
      if (controller.signal.aborted || authGeneration.current !== generation
        || sessionRef.current?.user.id !== identity
        || sessionRef.current.access_token !== token) return;
      if (response.ok && isSessionInfo(payload)) {
        authorizedIdentity.current = identity;
        setBackendUserId(identity);
        setPasswordSetupRequired((required) => required || payload.cambio_clave_obligatorio);
        setBackend({ kind: "ready", data: payload });
        return;
      }
      const code = payloadCode(payload);
      const accessDenied = response.status === 401 || response.status === 403
        || code === "profile_not_found" || code === "profile_inactive" || code === "no_access"
        || code === "authentication_failed" || code === "not_authenticated";
      const message = backendError(code, payloadMessage(payload) ?? `El backend respondió con HTTP ${response.status}.`);
      if (accessDenied) {
        authorizedIdentity.current = "";
        setBackendUserId("");
        setBackend({ kind: "blocked", errorCode: code ?? "access_denied", message });
      } else if (hasAuthorizedWorkspace) {
        setSessionRefreshError(`No se pudieron revalidar los permisos: ${message}`);
      } else {
        setBackend({ kind: "error", message });
      }
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === "AbortError") {
        if (timedOut && authGeneration.current === generation
          && sessionRef.current?.user.id === identity
          && sessionRef.current.access_token === token) {
          const message = "La validación de la sesión autorizada agotó el tiempo de espera. Comprueba el backend y reintenta.";
          if (hasAuthorizedWorkspace) setSessionRefreshError(message);
          else setBackend({ kind: "error", message });
        }
        return;
      }
      if (authGeneration.current !== generation
        || sessionRef.current?.user.id !== identity
        || sessionRef.current.access_token !== token) return;
      const message = `No fue posible consultar el backend: ${error instanceof Error ? error.message : "Error de conexión desconocido."}`;
      if (hasAuthorizedWorkspace) setSessionRefreshError(message);
      else setBackend({ kind: "error", message });
    }).finally(() => window.clearTimeout(requestTimeout));
    }, 0);
    return () => {
      window.clearTimeout(timer);
      window.clearTimeout(requestTimeout);
      controller.abort();
    };
  }, [session?.user.id, session?.access_token, sessionRefresh]);

  useEffect(() => {
    const revalidateWhenVisible = () => {
      if (document.visibilityState !== "visible" || !sessionRef.current) return;
      if (Date.now() - lastSessionValidation.current < 30_000) return;
      setSessionRefresh((value) => value + 1);
    };
    document.addEventListener("visibilitychange", revalidateWhenVisible);
    return () => document.removeEventListener("visibilitychange", revalidateWhenVisible);
  }, []);

  useEffect(() => {
    if (activeNav === "Cuentas" || activeNav === "Nueva conciliación" || activeNav === "Historial") {
      window.requestAnimationFrame(() => window.scrollTo(0, workspaceScroll.current));
    }
  }, [activeNav]);

  useEffect(() => {
    if (backend.kind !== "ready") return;
    const accessibleCountries = activeNav === "Administración de usuarios"
      ? [...new Set(backend.data.accesos.filter((item) => item.rol === "administrador").map((item) => item.pais_codigo))]
      : backend.data.paises.map((item) => item.codigo);
    const country: "SV" | "GT" | "" = accessibleCountries.find((code) => code === selectedCountry)
      ?? accessibleCountries[0]
      ?? "";
    setSelectedCountry(country);
    const companies = backend.data.accesos.filter((item) => item.pais_codigo === country
      && (activeNav !== "Administración de usuarios" || item.rol === "administrador"));
    setSelectedCompanyId(companies.some((item) => item.empresa_id === selectedCompanyId) ? selectedCompanyId : companies[0]?.empresa_id ?? "");
  }, [backend, activeNav]);

  async function handleSignIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!supabase || signingOut) return;
    setFormError(""); setSubmitting(true);
    try {
      const { error } = await supabase.auth.signInWithPassword({ email, password });
      if (error) setFormError("No se pudo iniciar sesión. Verifica el correo y la contraseña.");
    }
    catch { setFormError("No se pudo iniciar sesión. Intenta de nuevo."); }
    finally { setPassword(""); setSubmitting(false); }
  }
  async function handleSignOut() {
    if (!supabase || !supabaseStorage || signingOut) return;
    setSigningOut(true);
    setLogoutError("");
    setFormError("");
    setPasswordSetupError("");
    try {
      const { error } = await supabase.auth.signOut({ scope: "local" });
      if (error) {
        supabaseStorage.clearSession();
        clearAuthenticatedState();
        setFormError("La sesión se cerró en este navegador, pero Supabase no confirmó el cierre remoto. Puedes iniciar sesión de nuevo.");
      } else {
        clearAuthenticatedState();
      }
    } catch {
      try {
        supabaseStorage.clearSession();
        clearAuthenticatedState();
        setFormError("La sesión se cerró en este navegador; no se pudo confirmar con Supabase. Inicia sesión nuevamente.");
      } catch {
        setLogoutError("No se pudo cerrar la sesión de ContaRed en este navegador. Comprueba el almacenamiento del navegador e inténtalo de nuevo.");
      }
    } finally {
      setSigningOut(false);
    }
  }

  async function returnToSignIn(message: string) {
    await handleSignOut();
    if (sessionRef.current === null) setFormError(message);
    else setPasswordSetupError("La sesión venció. Reintenta cerrar sesión o inicia sesión de nuevo.");
  }

  async function handleSetPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!supabase) {
      setPasswordSetupError("No se pudo conectar con Supabase. Intenta de nuevo más tarde.");
      return;
    }
    if (newPassword !== confirmPassword || (
      backend.kind === "ready"
      && backend.data.cambio_clave_obligatorio
      && newPassword === currentPassword
    )) {
      setPasswordSetupError(newPassword !== confirmPassword
        ? "Las contraseñas nuevas no coinciden."
        : "La contraseña nueva debe ser distinta de la temporal.");
      return;
    }
    setPasswordSetupSaving(true);
    setPasswordSetupError("");
    const generation = authGeneration.current;
    const controller = new AbortController();
    passwordSetupRequest.current = controller;
    try {
      if (backend.kind === "ready" && backend.data.cambio_clave_obligatorio) {
        if (!apiBaseUrl) throw new Error("Falta configurar VITE_API_BASE_URL en frontend/.env.");
        const response = await authenticatedFetch(supabase.auth, `${apiBaseUrl}/api/v1/session/password/`, {
          method: "POST",
          signal: controller.signal,
          headers: {
            Authorization: `******`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            contrasena_actual: currentPassword,
            nueva_contrasena: newPassword,
          }),
        });
        if (generation !== authGeneration.current) return;
        const payload: unknown = await response.json().catch(() => null);
        if (response.status === 401) {
          await returnToSignIn("Tu sesión de Supabase venció. Inicia sesión nuevamente para completar el cambio de contraseña.");
          return;
        }
        if (!response.ok) {
          throw new Error(payloadMessage(payload) ?? `La solicitud falló (HTTP ${response.status}).`);
        }
        if (typeof payload !== "object" || payload === null
          || (payload as Record<string, unknown>).cambio_clave_obligatorio !== false) {
          throw new Error("El backend no confirmó que el cambio obligatorio se completó.");
        }
        setBackend((current) => current.kind === "ready"
          ? { ...current, data: { ...current.data, cambio_clave_obligatorio: false } }
          : current);
      } else {
        const { error } = await supabase.auth.updateUser({ password: newPassword });
        if (error) {
          setPasswordSetupError("No se pudo establecer la contraseña. Verifica el enlace de recuperación e intenta de nuevo.");
          return;
        }
      }
      setPasswordSetupRequired(false);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      const url = new URL(window.location.href);
      url.searchParams.delete("establecer-clave");
      window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    } catch (error: unknown) {
      if (generation !== authGeneration.current) return;
      if (error instanceof AuthenticatedSessionUnavailable) {
        await returnToSignIn(error.message);
      } else {
        setPasswordSetupError(`No se pudo establecer la contraseña: ${error instanceof Error ? error.message : "Error desconocido."}`);
      }
    } finally {
      if (passwordSetupRequest.current === controller) passwordSetupRequest.current = null;
      if (generation === authGeneration.current) {
        setCurrentPassword("");
        setNewPassword("");
        setConfirmPassword("");
        setPasswordSetupSaving(false);
      }
    }
  }

  function handleCountryChange(country: "SV" | "GT") {
    setSelectedCountry(country); setSelectedCompanyId("");
    if (backend.kind !== "ready") return;
    const first = backend.data.accesos.find((item) => item.pais_codigo === country
      && (activeNav !== "Administración de usuarios" || item.rol === "administrador"));
    setSelectedCompanyId(first?.empresa_id ?? "");
  }

  if (authLoading) return <main className="loading-screen"><span className="loader" /><p>Verificando sesión…</p></main>;

  return <main className="app-shell">
    {!session ? <>
      <header className="topbar login-topbar"><div className="brand"><img src={logoUrl} alt="ContaRed" /><span><strong>ContaRed</strong><small>Conciliación bancaria</small></span></div></header>
      <section className="login-layout">
        <div className="intro"><p className="eyebrow">CONTROL FINANCIERO</p><h1>Conciliación clara, cuenta por cuenta.</h1><p>Un espacio de trabajo para comparar libros y bancos de El Salvador y Guatemala, con permisos por empresa y país.</p></div>
        <form className="login-card" onSubmit={handleSignIn}>
          <h2>Iniciar sesión</h2>
          <p className="muted">Usa la cuenta autorizada por tu administrador.</p>
          <label htmlFor="email">Correo electrónico</label>
          <input id="email" autoComplete="username" type="email" required value={email} onChange={(event) => setEmail(event.target.value)} />
          <label htmlFor="password">Contraseña</label>
          <input id="password" autoComplete="current-password" type="password" required value={password} onChange={(event) => setPassword(event.target.value)} />
          {formError && <p className="notice error" role="alert">{formError}</p>}
          {logoutError && <div className="notice error" role="alert"><span>{logoutError}</span><button className="retry-button" type="button" onClick={handleSignOut} disabled={signingOut}>Reintentar cierre de sesión</button></div>}
          {signingOut && <p className="notice warning" role="status">Cerrando la sesión anterior…</p>}
          {authRecoveryFailed && <button className="retry-button" type="button" onClick={() => setAuthRecoveryAttempt((attempt) => attempt + 1)}>Reintentar recuperación de sesión</button>}
          <button className="primary-button" disabled={submitting || signingOut || !supabase} type="submit">{submitting ? "Ingresando…" : "Ingresar"}</button>
          <p className="no-signup">El acceso es administrado. No hay registro público.</p>
        </form>
      </section>
    </> : passwordSetupRequired && backend.kind === "ready" ? <section className="session-error-layout">
      <header className="topbar login-topbar"><div className="brand"><img src={logoUrl} alt="ContaRed" /><span><strong>ContaRed</strong></span></div></header>
      <form className="login-card password-setup-card" onSubmit={handleSetPassword}>
        <button className="quiet-button" type="button" onClick={handleSignOut} disabled={signingOut}>{signingOut ? "Cerrando sesión…" : "Volver a iniciar sesión"}</button>
        <h2>Establecer nueva contraseña</h2>
        <p className="muted">Crea una contraseña nueva para tu cuenta de ContaRed. No reutilices la contraseña temporal.</p>
        {backend.kind === "ready" && backend.data.cambio_clave_obligatorio && <>
          <label htmlFor="current-password">Contraseña temporal actual</label>
          <input id="current-password" autoComplete="current-password" type="password" required value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} />
        </>}
        <label htmlFor="new-password">Nueva contraseña</label>
        <input id="new-password" autoComplete="new-password" type="password" minLength={8} required value={newPassword} onChange={(event) => setNewPassword(event.target.value)} />
        <label htmlFor="confirm-password">Confirmar contraseña</label>
        <input id="confirm-password" autoComplete="new-password" type="password" minLength={8} required value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} />
        {passwordSetupError && <p className="notice error" role="alert">{passwordSetupError}</p>}
        <button className="primary-button" disabled={passwordSetupSaving} type="submit">{passwordSetupSaving ? "Guardando…" : "Establecer contraseña"}</button>
      </form>
    </section> : backend.kind !== "ready" || backendUserId !== session.user.id ? <section className="session-error-layout">
      <header className="topbar login-topbar"><div className="brand"><img src={logoUrl} alt="ContaRed" /><span><strong>ContaRed</strong></span></div><button className="quiet-button" onClick={handleSignOut}>Volver a iniciar sesión</button></header>
      <div className={`notice ${backend.kind === "checking" || backendUserId !== session.user.id ? "warning" : "error"}`} role="alert">
        {backend.kind === "checking" || backendUserId !== session.user.id ? "Validando la sesión autorizada…" : backend.kind === "ready" ? "" : backend.message}
        {backend.kind === "error" && <button className="retry-button" type="button" onClick={() => setSessionRefresh((value) => value + 1)}>Reintentar</button>}
      </div>
    </section> : (() => {
      const isUserAdministration = activeNav === "Administración de usuarios";
      const visibleAccesses = isUserAdministration
        ? backend.data.accesos.filter((item) => item.rol === "administrador")
        : backend.data.accesos;
      const companies = visibleAccesses.filter((item) => item.pais_codigo === selectedCountry);
      const selectedAccess = companies.find((item) => item.empresa_id === selectedCompanyId) ?? companies[0];
      if (!selectedAccess) return <section className="session-error-layout"><div className="notice error">No hay una empresa autorizada en el país seleccionado.</div></section>;
      const title = activeNav === "Cuentas" ? "Cuentas y bancos" : activeNav;
      return <div className="workspace-shell">
        <NavigationSidebar active={activeNav} userName={backend.data.usuario.nombre} role={selectedAccess.rol} isAdministrator={backend.data.accesos.some((item) => item.rol === "administrador")} mobileOpen={mobileNavOpen} onChange={(section) => { workspaceScroll.current = window.scrollY; if (section === "Nueva conciliación") setNewReconciliationAccountId(""); if (section === "Administración de usuarios") { const admin = backend.data.accesos.find((item) => item.rol === "administrador"); if (admin) { setSelectedCountry(admin.pais_codigo); setSelectedCompanyId(admin.empresa_id); } } setVisitedModules((current) => new Set(current).add(section)); setActiveNav(section); }} onCloseMobile={() => setMobileNavOpen(false)} />
        <div className="main-column"><ContentHeader title={title} countries={isUserAdministration ? backend.data.paises.filter((country) => visibleAccesses.some((item) => item.pais_codigo === country.codigo)) : backend.data.paises} companies={companies.map((item) => ({ empresa_id: item.empresa_id, empresa: item.empresa }))} countryCode={selectedCountry} companyId={selectedAccess.empresa_id} onCountryChange={handleCountryChange} onCompanyChange={(companyId) => setSelectedCompanyId(companyId)} onLogout={handleSignOut} logoutError={logoutError} signingOut={signingOut} onOpenMobile={() => setMobileNavOpen(true)} /><main className="content-area">
          {sessionRefreshError && <div className="notice warning" role="status"><span>{sessionRefreshError}</span><button type="button" className="retry-button" onClick={() => setSessionRefresh((value) => value + 1)}>Reintentar</button></div>}
          <div style={{ display: activeNav === "Cuentas" ? undefined : "none" }} aria-hidden={activeNav !== "Cuentas"}>
            {visitedModules.has("Cuentas") && <BankingModule key={`${session.user.id}-${selectedAccess.empresa_id}-${selectedAccess.pais_codigo}`} session={session} companyId={selectedAccess.empresa_id} countryCode={selectedAccess.pais_codigo} role={selectedAccess.rol} onNewReconciliation={(accountId) => { setNewReconciliationAccountId(accountId); workspaceScroll.current = 0; setVisitedModules((current) => new Set(current).add("Nueva conciliación")); setActiveNav("Nueva conciliación"); }} onViewAccount={(accountId) => { setHistoryAccountId(accountId); setHistoryBankId(""); workspaceScroll.current = window.scrollY; setVisitedModules((current) => new Set(current).add("Historial")); setActiveNav("Historial"); }} onViewBank={(bankId) => { setHistoryBankId(bankId); setHistoryAccountId(""); workspaceScroll.current = window.scrollY; setVisitedModules((current) => new Set(current).add("Historial")); setActiveNav("Historial"); }} />}
          </div>
          <div style={{ display: activeNav === "Nueva conciliación" ? undefined : "none" }} aria-hidden={activeNav !== "Nueva conciliación"}>
            {visitedModules.has("Nueva conciliación") && <DirectReconciliationModule key={`${session.user.id}-${selectedAccess.empresa_id}-${selectedAccess.pais_codigo}`} session={session} companyId={selectedAccess.empresa_id} companyName={selectedAccess.empresa} countryCode={selectedAccess.pais_codigo} countryName={selectedAccess.pais} role={selectedAccess.rol} mode="new" initialAccountId={newReconciliationAccountId || undefined} onSavedAccount={(accountId) => { setHistoryAccountId(accountId); setHistoryBankId(""); workspaceScroll.current = 0; setVisitedModules((current) => new Set(current).add("Historial")); setActiveNav("Historial"); }} />}
          </div>
          <div style={{ display: activeNav === "Historial" ? undefined : "none" }} aria-hidden={activeNav !== "Historial"}>
            {visitedModules.has("Historial") && <DirectReconciliationModule key={`history-${session.user.id}-${selectedAccess.empresa_id}-${selectedAccess.pais_codigo}-${historyAccountId}-${historyBankId}`} session={session} companyId={selectedAccess.empresa_id} companyName={selectedAccess.empresa} countryCode={selectedAccess.pais_codigo} countryName={selectedAccess.pais} role={selectedAccess.rol} mode="history" cuentaId={historyAccountId || undefined} bancoId={historyBankId || undefined} onBackToAccount={() => setActiveNav("Cuentas")} onBackToBank={() => setActiveNav("Cuentas")} />}
          </div>
          <div style={{ display: activeNav === "Administración de usuarios" ? undefined : "none" }} aria-hidden={activeNav !== "Administración de usuarios"}>
            {visitedModules.has("Administración de usuarios") && <UserAdministrationModule key={`users-${session.user.id}-${selectedAccess.empresa_id}-${selectedAccess.pais_codigo}`} session={session} adminScopes={backend.data.accesos.filter((item) => item.rol === "administrador")} initialCompanyId={selectedAccess.empresa_id} initialCountryCode={selectedAccess.pais_codigo} onScopeChange={(companyId, countryCode) => { setSelectedCompanyId(companyId); setSelectedCountry(countryCode); }} />}
          </div>
        </main></div>
      </div>;
    })()}
  </main>;
}
