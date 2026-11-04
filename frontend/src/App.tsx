import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { createClient, type Session, type SupabaseClient } from "@supabase/supabase-js";
import logoUrl from "../../backend/img/logo.jpg";
import NavigationSidebar, { type Section } from "./components/NavigationSidebar";
import ContentHeader from "./components/ContentHeader";
import BankingModule from "./components/BankingModule";
import DirectReconciliationModule from "./components/DirectReconciliationModule";

type AuthorizedAccess = {
  empresa_id: string;
  empresa: string;
  pais_codigo: "SV" | "GT";
  pais: string;
  rol: "administrador" | "auxiliar" | "revisor" | "consulta";
};
type SessionInfo = { usuario: { id: string; nombre: string }; accesos: AuthorizedAccess[]; paises: { codigo: "SV" | "GT"; nombre: string }[] };
type BackendState = { kind: "checking" } | { kind: "ready"; data: SessionInfo } | { kind: "blocked"; message: string; errorCode: string } | { kind: "error"; message: string };
const supabaseUrl = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const supabaseKey = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY as string | undefined;
const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "");
const supabase: SupabaseClient | null = supabaseUrl && supabaseKey ? createClient(supabaseUrl, supabaseKey) : null;
const SESSION_VALIDATION_TIMEOUT_MS = 15_000;

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
  return typeof record.usuario === "object" && record.usuario !== null && Array.isArray(record.accesos) && Array.isArray(record.paises);
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
  const [selectedCountry, setSelectedCountry] = useState<"SV" | "GT" | "">("");
  const [selectedCompanyId, setSelectedCompanyId] = useState("");
  const [activeNav, setActiveNav] = useState<Section>("Nueva conciliación");
  const [visitedModules, setVisitedModules] = useState<Set<Section>>(() => new Set<Section>(["Cuentas"]));
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [historyAccountId, setHistoryAccountId] = useState("");
  const [historyBankId, setHistoryBankId] = useState("");
  const sessionRef = useRef<Session | null>(null);
  const authorizedIdentity = useRef("");
  const lastSessionValidation = useRef(0);
  const workspaceScroll = useRef(0);

  useEffect(() => {
    if (!supabase) { setAuthLoading(false); setBackend({ kind: "error", message: "Falta configurar Supabase en frontend/.env." }); return; }
    let active = true;
    let authEventReceived = false;
    setAuthLoading(true);
    setAuthRecoveryFailed(false);
    setFormError("");
    const recoveryTimeout = window.setTimeout(() => {
      if (!active) return;
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
        authorizedIdentity.current = "";
        setBackendUserId("");
        setBackend({ kind: "checking" });
        setSessionRefreshError("");
        setSelectedCountry("");
        setSelectedCompanyId("");
        setActiveNav("Cuentas");
        setVisitedModules(new Set<Section>(["Cuentas"]));
        setMobileNavOpen(false);
      }
      setSession(nextSession);
    }
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
      setAuthLoading(false);
    }).catch((error: unknown) => {
      window.clearTimeout(recoveryTimeout);
      if (active) {
        setAuthRecoveryFailed(true);
        setFormError(`No se pudo recuperar la sesión: ${error instanceof Error ? error.message : "Error desconocido."}`);
        setAuthLoading(false);
      }
    });
    return () => {
      active = false;
      window.clearTimeout(recoveryTimeout);
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
    const identity = session.user.id;
    const token = session.access_token;
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
      if (sessionRef.current?.user.id !== identity || sessionRef.current.access_token !== token) return;
      const message = "La validación de la sesión autorizada agotó el tiempo de espera. Comprueba el backend y reintenta.";
      if (hasAuthorizedWorkspace) setSessionRefreshError(message);
      else setBackend({ kind: "error", message });
    }, SESSION_VALIDATION_TIMEOUT_MS);
    const timer = window.setTimeout(() => { void fetch(`${apiBaseUrl}/api/v1/session/`, { headers: { Authorization: `Bearer ${session.access_token}` }, signal: controller.signal }).then(async (response) => {
      const payload: unknown = await response.json().catch(() => null);
      if (controller.signal.aborted || sessionRef.current?.user.id !== identity || sessionRef.current.access_token !== token) return;
      if (response.ok && isSessionInfo(payload)) {
        authorizedIdentity.current = identity;
        setBackendUserId(identity);
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
        if (timedOut && sessionRef.current?.user.id === identity && sessionRef.current.access_token === token) {
          const message = "La validación de la sesión autorizada agotó el tiempo de espera. Comprueba el backend y reintenta.";
          if (hasAuthorizedWorkspace) setSessionRefreshError(message);
          else setBackend({ kind: "error", message });
        }
        return;
      }
      if (sessionRef.current?.user.id !== identity || sessionRef.current.access_token !== token) return;
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
    const country = backend.data.paises.some((item) => item.codigo === selectedCountry) ? selectedCountry : backend.data.paises[0]?.codigo ?? "";
    setSelectedCountry(country);
    const companies = backend.data.accesos.filter((item) => item.pais_codigo === country);
    setSelectedCompanyId(companies.some((item) => item.empresa_id === selectedCompanyId) ? selectedCompanyId : companies[0]?.empresa_id ?? "");
  }, [backend]);

  async function handleSignIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!supabase) return;
    setFormError(""); setSubmitting(true);
    try { const { error } = await supabase.auth.signInWithPassword({ email, password }); if (error) setFormError(`No se pudo iniciar sesión: ${error.message}`); }
    catch (error: unknown) { setFormError(`No se pudo iniciar sesión: ${error instanceof Error ? error.message : "Error desconocido."}`); }
    finally { setSubmitting(false); }
  }
  async function handleSignOut() { if (!supabase) return; await supabase.auth.signOut(); }

  function handleCountryChange(country: "SV" | "GT") {
    setSelectedCountry(country); setSelectedCompanyId("");
    if (backend.kind !== "ready") return;
    const first = backend.data.accesos.find((item) => item.pais_codigo === country);
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
          {authRecoveryFailed && <button className="retry-button" type="button" onClick={() => setAuthRecoveryAttempt((attempt) => attempt + 1)}>Reintentar recuperación de sesión</button>}
          <button className="primary-button" disabled={submitting || !supabase} type="submit">{submitting ? "Ingresando…" : "Ingresar"}</button>
          <p className="no-signup">El acceso es por invitación. No hay registro público.</p>
        </form>
      </section>
    </> : backend.kind !== "ready" || backendUserId !== session.user.id ? <section className="session-error-layout">
      <header className="topbar login-topbar"><div className="brand"><img src={logoUrl} alt="ContaRed" /><span><strong>ContaRed</strong></span></div><button className="quiet-button" onClick={handleSignOut}>Volver a iniciar sesión</button></header>
      <div className={`notice ${backend.kind === "checking" || backendUserId !== session.user.id ? "warning" : "error"}`} role="alert">
        {backend.kind === "checking" || backendUserId !== session.user.id ? "Validando la sesión autorizada…" : backend.kind === "ready" ? "" : backend.message}
        {backend.kind === "error" && <button className="retry-button" type="button" onClick={() => setSessionRefresh((value) => value + 1)}>Reintentar</button>}
      </div>
    </section> : (() => {
      const companies = backend.data.accesos.filter((item) => item.pais_codigo === selectedCountry);
      const selectedAccess = companies.find((item) => item.empresa_id === selectedCompanyId) ?? companies[0];
      if (!selectedAccess) return <section className="session-error-layout"><div className="notice error">No hay una empresa autorizada en el país seleccionado.</div></section>;
      const title = activeNav === "Cuentas" ? "Cuentas y bancos" : activeNav;
      return <div className="workspace-shell">
        <NavigationSidebar active={activeNav} userName={backend.data.usuario.nombre} role={selectedAccess.rol} mobileOpen={mobileNavOpen} onChange={(section) => { workspaceScroll.current = window.scrollY; setVisitedModules((current) => new Set(current).add(section)); setActiveNav(section); }} onCloseMobile={() => setMobileNavOpen(false)} />
        <div className="main-column"><ContentHeader title={title} countries={backend.data.paises} companies={companies.map((item) => ({ empresa_id: item.empresa_id, empresa: item.empresa }))} countryCode={selectedCountry} companyId={selectedAccess.empresa_id} onCountryChange={handleCountryChange} onCompanyChange={(companyId) => setSelectedCompanyId(companyId)} onLogout={handleSignOut} onOpenMobile={() => setMobileNavOpen(true)} /><main className="content-area">
          {sessionRefreshError && <div className="notice warning" role="status"><span>{sessionRefreshError}</span><button type="button" className="retry-button" onClick={() => setSessionRefresh((value) => value + 1)}>Reintentar</button></div>}
          <div style={{ display: activeNav === "Cuentas" ? undefined : "none" }} aria-hidden={activeNav !== "Cuentas"}>
            {visitedModules.has("Cuentas") && <BankingModule key={`${session.user.id}-${selectedAccess.empresa_id}-${selectedAccess.pais_codigo}`} session={session} companyId={selectedAccess.empresa_id} countryCode={selectedAccess.pais_codigo} role={selectedAccess.rol} onViewAccount={(accountId) => { setHistoryAccountId(accountId); setHistoryBankId(""); workspaceScroll.current = window.scrollY; setVisitedModules((current) => new Set(current).add("Historial")); setActiveNav("Historial"); }} onViewBank={(bankId) => { setHistoryBankId(bankId); setHistoryAccountId(""); workspaceScroll.current = window.scrollY; setVisitedModules((current) => new Set(current).add("Historial")); setActiveNav("Historial"); }} />}
          </div>
          <div style={{ display: activeNav === "Nueva conciliación" ? undefined : "none" }} aria-hidden={activeNav !== "Nueva conciliación"}>
            {visitedModules.has("Nueva conciliación") && <DirectReconciliationModule key={`${session.user.id}-${selectedAccess.empresa_id}-${selectedAccess.pais_codigo}`} session={session} companyId={selectedAccess.empresa_id} companyName={selectedAccess.empresa} countryCode={selectedAccess.pais_codigo} countryName={selectedAccess.pais} role={selectedAccess.rol} mode="new" onSavedAccount={(accountId) => { setHistoryAccountId(accountId); setHistoryBankId(""); workspaceScroll.current = 0; setVisitedModules((current) => new Set(current).add("Historial")); setActiveNav("Historial"); }} />}
          </div>
          <div style={{ display: activeNav === "Historial" ? undefined : "none" }} aria-hidden={activeNav !== "Historial"}>
            {visitedModules.has("Historial") && <DirectReconciliationModule key={`history-${session.user.id}-${selectedAccess.empresa_id}-${selectedAccess.pais_codigo}-${historyAccountId}-${historyBankId}`} session={session} companyId={selectedAccess.empresa_id} companyName={selectedAccess.empresa} countryCode={selectedAccess.pais_codigo} countryName={selectedAccess.pais} role={selectedAccess.rol} mode="history" cuentaId={historyAccountId || undefined} bancoId={historyBankId || undefined} onBackToAccount={() => setActiveNav("Cuentas")} onBackToBank={() => setActiveNav("Cuentas")} />}
          </div>
        </main></div>
      </div>;
    })()}
  </main>;
}
