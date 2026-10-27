import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { createClient, type Session, type SupabaseClient } from "@supabase/supabase-js";
import logoUrl from "../../backend/img/logo.jpg";
import NavigationSidebar, { type Section } from "./components/NavigationSidebar";
import ContentHeader from "./components/ContentHeader";
import BankingModule from "./components/BankingModule";
import ReconciliationModule from "./components/ReconciliationModule";

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
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const [backend, setBackend] = useState<BackendState>({ kind: "checking" });
  const [selectedCountry, setSelectedCountry] = useState<"SV" | "GT" | "">("");
  const [selectedCompanyId, setSelectedCompanyId] = useState("");
  const [activeNav, setActiveNav] = useState<Section>("Cuentas");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  useEffect(() => {
    if (!supabase) { setAuthLoading(false); setBackend({ kind: "error", message: "Falta configurar Supabase en frontend/.env." }); return; }
    let active = true;
    const subscription = supabase.auth.onAuthStateChange((_event, nextSession) => { if (active) setSession(nextSession); });
    void supabase.auth.getSession().then(({ data, error }) => {
      if (!active) return;
      if (error) setFormError(`No se pudo recuperar la sesión: ${error.message}`);
      setSession(data.session); setAuthLoading(false);
    }).catch((error: unknown) => { if (active) { setFormError(`No se pudo recuperar la sesión: ${error instanceof Error ? error.message : "Error desconocido."}`); setAuthLoading(false); } });
    return () => { active = false; subscription.data.subscription.unsubscribe(); };
  }, []);

  useEffect(() => {
    if (!session) { setBackend({ kind: "checking" }); setSelectedCountry(""); setSelectedCompanyId(""); return; }
    if (!apiBaseUrl) { setBackend({ kind: "error", message: "Falta configurar VITE_API_BASE_URL en frontend/.env." }); return; }
    const controller = new AbortController();
    setBackend({ kind: "checking" });
    void fetch(`${apiBaseUrl}/api/v1/session/`, { headers: { Authorization: `Bearer ${session.access_token}` }, signal: controller.signal }).then(async (response) => {
      const payload: unknown = await response.json().catch(() => null);
      if (response.ok && isSessionInfo(payload)) setBackend({ kind: "ready", data: payload });
      else { const code = payloadCode(payload) ?? "request_failed"; setBackend({ kind: "blocked", errorCode: code, message: backendError(code, payloadMessage(payload) ?? `El backend respondió con HTTP ${response.status}.`) }); }
    }).catch((error: unknown) => { if (!(error instanceof DOMException && error.name === "AbortError")) setBackend({ kind: "error", message: `No fue posible consultar el backend: ${error instanceof Error ? error.message : "Error de conexión desconocido."}` }); });
    return () => controller.abort();
  }, [session]);

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
      <section className="login-layout"><div className="intro"><p className="eyebrow">CONTROL FINANCIERO</p><h1>Conciliación clara, cuenta por cuenta.</h1><p>Un espacio de trabajo para comparar libros y bancos de El Salvador y Guatemala, con permisos por empresa y país.</p></div><form className="login-card" onSubmit={handleSignIn}><h2>Iniciar sesión</h2><p className="muted">Usa la cuenta autorizada por tu administrador.</p><label htmlFor="email">Correo electrónico</label><input id="email" autoComplete="username" type="email" required value={email} onChange={(event) => setEmail(event.target.value)} /><label htmlFor="password">Contraseña</label><input id="password" autoComplete="current-password" type="password" required value={password} onChange={(event) => setPassword(event.target.value)} />{formError && <p className="notice error" role="alert">{formError}</p>}<button className="primary-button" disabled={submitting || !supabase} type="submit">{submitting ? "Ingresando…" : "Ingresar"}</button><p className="no-signup">El acceso es por invitación. No hay registro público.</p></form></section>
    </> : backend.kind !== "ready" ? <section className="session-error-layout"><header className="topbar login-topbar"><div className="brand"><img src={logoUrl} alt="ContaRed" /><span><strong>ContaRed</strong></span></div><button className="quiet-button" onClick={handleSignOut}>Cerrar sesión</button></header><div className={`notice ${backend.kind === "checking" ? "warning" : "error"}`} role="alert">{backend.kind === "checking" ? "Validando la sesión autorizada…" : backend.message}</div></section> : (() => {
      const companies = backend.data.accesos.filter((item) => item.pais_codigo === selectedCountry);
      const selectedAccess = companies.find((item) => item.empresa_id === selectedCompanyId) ?? companies[0];
      if (!selectedAccess) return <section className="session-error-layout"><div className="notice error">No hay una empresa autorizada en el país seleccionado.</div></section>;
      const title = activeNav === "Cuentas" ? "Cuentas y bancos" : activeNav;
      return <div className="workspace-shell">
        <NavigationSidebar active={activeNav} userName={backend.data.usuario.nombre} role={selectedAccess.rol} mobileOpen={mobileNavOpen} onChange={setActiveNav} onCloseMobile={() => setMobileNavOpen(false)} />
        <div className="main-column"><ContentHeader title={title} countries={backend.data.paises} companies={companies.map((item) => ({ empresa_id: item.empresa_id, empresa: item.empresa }))} countryCode={selectedCountry} companyId={selectedAccess.empresa_id} onCountryChange={handleCountryChange} onCompanyChange={(companyId) => setSelectedCompanyId(companyId)} onLogout={handleSignOut} onOpenMobile={() => setMobileNavOpen(true)} /><main className="content-area">
          {activeNav === "Cuentas" ? <BankingModule key={`${selectedAccess.empresa_id}-${selectedAccess.pais_codigo}`} session={session} companyId={selectedAccess.empresa_id} countryCode={selectedAccess.pais_codigo} role={selectedAccess.rol} /> : activeNav === "Conciliaciones" ? <ReconciliationModule key={`${selectedAccess.empresa_id}-${selectedAccess.pais_codigo}`} session={session} companyId={selectedAccess.empresa_id} companyName={selectedAccess.empresa} countryCode={selectedAccess.pais_codigo} countryName={selectedAccess.pais} role={selectedAccess.rol} /> : <section className="pending-section"><h2>{title}</h2><p>Esta sección estará disponible en una próxima etapa.</p></section>}
        </main></div>
      </div>;
    })()}
  </main>;
}
