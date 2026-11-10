import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { Session } from "@supabase/supabase-js";
import "./UserAdministrationModule.css";

type CountryCode = "SV" | "GT";
type Role = "administrador" | "auxiliar" | "revisor" | "consulta";
type AdminScope = {
  empresa_id: string;
  empresa: string;
  pais_codigo: CountryCode;
  pais: string;
  rol: Role;
};
type UserAccess = { pais_codigo: CountryCode; pais: string; rol: Role; activo: boolean };
type ManagedUser = {
  id: string;
  nombre: string;
  correo: string;
  correo_pendiente: string | null;
  perfil_global_editable: boolean;
  perfil_activo: boolean;
  tiene_identidad_auth: boolean;
  cambio_clave_obligatorio: boolean;
  estado_invitacion: "pendiente" | "enviada" | "fallida" | "no_requerida";
  invitacion_enviada_en: string | null;
  accesos: UserAccess[];
};
type DeletionPreview = {
  usuario: { nombre: string; correo: string };
  accesos_afectados: (UserAccess & { empresa: string })[];
  tiene_accesos_fuera_del_ambito: boolean;
  tiene_identidad_auth: boolean;
  es_propia: boolean;
  puede_eliminar_perfil: boolean;
  motivo_bloqueo: string;
  referencias_historicas: boolean;
};
type Props = {
  session: Session;
  adminScopes: AdminScope[];
  initialCompanyId: string;
  initialCountryCode: CountryCode;
  onScopeChange: (companyId: string, countryCode: CountryCode) => void;
};
type DraftAccess = { enabled: boolean; rol: Role; exists: boolean };

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "");
const ROLE_DETAILS: { id: Role; label: string; description: string }[] = [
  { id: "administrador", label: "Administrador", description: "Administra usuarios, bancos, cuentas y conciliaciones dentro de sus empresas y países autorizados." },
  { id: "auxiliar", label: "Auxiliar de contabilidad", description: "Carga documentos, realiza análisis, recalcula y guarda conciliaciones; consulta y descarga reportes." },
  { id: "revisor", label: "Revisor", description: "Consulta resultados y evidencias, descarga reportes y puede confirmar o descartar propuestas de comparación." },
  { id: "consulta", label: "Consulta", description: "Consulta conciliaciones guardadas y descarga reportes; no inicia análisis ni modifica datos." },
];
const COUNTRY_NAMES: Record<CountryCode, string> = { SV: "El Salvador", GT: "Guatemala" };

function responseMessage(payload: unknown, fallback: string) {
  if (typeof payload !== "object" || payload === null) return fallback;
  const record = payload as Record<string, unknown>;
  if (typeof record.detail === "string") return record.detail;
  if (typeof record.message === "string") return record.message;
  if (typeof record.mensaje === "string") return record.mensaje;
  for (const value of Object.values(record)) {
    if (typeof value === "string") return value;
    if (Array.isArray(value) && typeof value[0] === "string") return value[0];
  }
  return fallback;
}

function userStatus(user: ManagedUser, country: "all" | CountryCode = "all") {
  const accesses = user.accesos.filter((access) => country === "all" || access.pais_codigo === country);
  if (!user.perfil_activo || !accesses.some((access) => access.activo)) return "inactivo";
  if (user.cambio_clave_obligatorio || user.estado_invitacion === "pendiente" || user.estado_invitacion === "fallida") return "pendiente";
  return "activo";
}

export default function UserAdministrationModule({ session, adminScopes, initialCompanyId, initialCountryCode, onScopeChange }: Props) {
  const initialScope = adminScopes.find((scope) => scope.empresa_id === initialCompanyId && scope.pais_codigo === initialCountryCode) ?? adminScopes[0];
  const [companyId, setCompanyId] = useState(initialScope?.empresa_id ?? "");
  const [countryCode, setCountryCode] = useState<CountryCode>(initialScope?.pais_codigo ?? "GT");
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [operationError, setOperationError] = useState("");
  const [success, setSuccess] = useState("");
  const [search, setSearch] = useState("");
  const [countryFilter, setCountryFilter] = useState<"all" | CountryCode>("all");
  const [roleFilter, setRoleFilter] = useState<"all" | Role>("all");
  const [statusFilter, setStatusFilter] = useState<"all" | "activo" | "inactivo" | "pendiente">("all");
  const [modal, setModal] = useState<"create" | "edit" | "temporary" | "delete" | null>(null);
  const [editingUser, setEditingUser] = useState<ManagedUser | null>(null);
  const [deletionPreview, setDeletionPreview] = useState<DeletionPreview | null>(null);
  const [confirmPermanentDeletion, setConfirmPermanentDeletion] = useState(false);
  const [retireCountries, setRetireCountries] = useState<CountryCode[]>([]);
  const [drafts, setDrafts] = useState<Record<string, DraftAccess>>({});
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [credentialPassword, setCredentialPassword] = useState("");
  const [showCredentialPassword, setShowCredentialPassword] = useState(false);
  const generation = useRef(0);
  const requestInFlight = useRef(false);
  const accessToken = useRef(session.access_token);
  accessToken.current = session.access_token;
  const scopesForCompany = adminScopes.filter((scope) => scope.empresa_id === companyId);
  const authorizedCountries = [...new Set(scopesForCompany.map((scope) => scope.pais_codigo))];
  const companies = [...new Map(adminScopes.map((scope) => [scope.empresa_id, scope])).values()];
  const adminScopeKey = adminScopes.map((scope) => `${scope.empresa_id}:${scope.pais_codigo}`).join("|");

  useEffect(() => {
    if (!modal) {
      setCredentialPassword("");
      setShowCredentialPassword(false);
    }
  }, [modal]);

  async function apiRequest(path: string, options: RequestInit = {}) {
    if (!apiBaseUrl) throw new Error("Falta configurar VITE_API_BASE_URL en frontend/.env.");
    const response = await fetch(`${apiBaseUrl}${path}`, {
      ...options,
      headers: {
        Authorization: `Bearer ${accessToken.current}`,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers ?? {}),
      },
    });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(responseMessage(payload, `La solicitud falló (HTTP ${response.status}).`));
    }
    return payload;
  }

  async function loadUsers() {
    const current = ++generation.current;
    setLoading(true);
    setLoadError("");
    const query = new URLSearchParams({ empresa_id: companyId, pais_codigo: countryCode });
    try {
      const payload = await apiRequest(`/api/v1/administracion/usuarios/?${query}`);
      if (current !== generation.current) return;
      const rows = typeof payload === "object" && payload !== null
        ? (payload as { usuarios?: unknown }).usuarios
        : null;
      if (!Array.isArray(rows)) throw new Error("El backend devolvió una lista de usuarios inválida.");
      setUsers(rows as ManagedUser[]);
    } catch (error: unknown) {
      if (current === generation.current) {
        setLoadError(error instanceof Error ? error.message : "No se pudo consultar la lista de usuarios.");
      }
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }

  useEffect(() => {
    if (!companyId || !authorizedCountries.includes(countryCode)) {
      const next = scopesForCompany[0] ?? adminScopes[0];
      if (next) {
        setCompanyId(next.empresa_id);
        setCountryCode(next.pais_codigo);
        onScopeChange(next.empresa_id, next.pais_codigo);
      }
      return;
    }
    void loadUsers();
  }, [companyId, countryCode, session.access_token, adminScopeKey]);

  useEffect(() => setCountryFilter("all"), [companyId]);

  const filteredUsers = useMemo(() => users.filter((user) => {
    const term = search.trim().toLocaleLowerCase();
    const matchesText = !term || user.nombre.toLocaleLowerCase().includes(term) || user.correo.toLocaleLowerCase().includes(term);
    const visibleAccesses = user.accesos.filter((access) => countryFilter === "all" || access.pais_codigo === countryFilter);
    const matchesRole = roleFilter === "all" || visibleAccesses.some((access) => access.rol === roleFilter);
    const matchesCountry = visibleAccesses.length > 0;
    return matchesText && matchesRole && matchesCountry && (statusFilter === "all" || userStatus(user, countryFilter) === statusFilter);
  }), [users, search, roleFilter, countryFilter, statusFilter]);

  function startCreate(user?: ManagedUser) {
    setName("");
    setEmail("");
    setEditingUser(user ?? null);
    setCredentialPassword("");
    setShowCredentialPassword(false);
    setDrafts(Object.fromEntries(authorizedCountries.map((country) => [
      country,
      {
        enabled: Boolean(user?.accesos.find((access) => access.pais_codigo === country)?.activo),
        rol: user?.accesos.find((access) => access.pais_codigo === country)?.rol ?? "consulta",
        exists: Boolean(user?.accesos.find((access) => access.pais_codigo === country)),
      },
    ])));
    setOperationError("");
    if (user) {
      setName(user.nombre);
      setEmail(user.correo);
    }
    setModal("create");
  }

  function startTemporaryPassword(user: ManagedUser) {
    setEditingUser(user);
    setCredentialPassword("");
    setShowCredentialPassword(false);
    setOperationError("");
    setModal("temporary");
  }

  function generateTemporaryPassword() {
    const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%_-";
    const random = new Uint8Array(20);
    window.crypto.getRandomValues(random);
    setCredentialPassword(Array.from(random, (value) => alphabet[value % alphabet.length]).join(""));
    setShowCredentialPassword(true);
  }

  function startEdit(user: ManagedUser) {
    setEditingUser(user);
    setName(user.nombre);
    setEmail(user.correo);
    setDrafts(Object.fromEntries(authorizedCountries.map((country) => {
      const current = user.accesos.find((access) => access.pais_codigo === country);
      return [country, {
        enabled: Boolean(current?.activo),
        rol: current?.rol ?? "consulta",
        exists: Boolean(current),
      }];
    })));
    setOperationError("");
    setModal("edit");
  }

  function updateDraft(country: CountryCode, value: Partial<DraftAccess>) {
    setDrafts((current) => ({ ...current, [country]: { ...current[country], ...value } }));
  }

  async function saveUserCreation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requestInFlight.current) return;
    const accesses = authorizedCountries
      .filter((country) => drafts[country]?.enabled)
      .map((country) => ({ pais_codigo: country, rol: drafts[country].rol }));
    if (!accesses.length) {
      setOperationError("Selecciona al menos un país.");
      return;
    }
    requestInFlight.current = true;
    setSaving(true);
    setOperationError("");
    setSuccess("");
    try {
      const response = await apiRequest("/api/v1/administracion/usuarios/", {
        method: "POST",
        body: JSON.stringify({
          empresa_id: companyId,
          pais_codigo: countryCode,
          nombre: name,
          correo: email,
          contrasena_inicial: credentialPassword,
          ...(editingUser ? { usuario_id: editingUser.id } : {}),
          accesos: accesses,
        }),
      });
      setSuccess(responseMessage(response, "Se creó la cuenta y se guardaron sus accesos."));
      setModal(null);
      await loadUsers();
    } catch (error: unknown) {
      setOperationError(error instanceof Error ? error.message : "No se pudo crear la cuenta.");
      await loadUsers();
    } finally {
      setCredentialPassword("");
      requestInFlight.current = false;
      setSaving(false);
    }
  }

  async function assignTemporaryPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editingUser || requestInFlight.current) return;
    requestInFlight.current = true;
    setSaving(true);
    setOperationError("");
    setSuccess("");
    try {
      const query = new URLSearchParams({ empresa_id: companyId, pais_codigo: countryCode });
      const response = await apiRequest(
        `/api/v1/administracion/usuarios/${editingUser.id}/clave-temporal/?${query}`,
        {
          method: "POST",
          body: JSON.stringify({
            empresa_id: companyId,
            pais_codigo: countryCode,
            contrasena_temporal: credentialPassword,
          }),
        },
      );
      setSuccess(responseMessage(response, "Se asignó la contraseña temporal."));
      setModal(null);
      await loadUsers();
    } catch (error: unknown) {
      setOperationError(error instanceof Error ? error.message : "No se pudo asignar la contraseña temporal.");
    } finally {
      setCredentialPassword("");
      requestInFlight.current = false;
      setSaving(false);
    }
  }

  async function saveUserChanges(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editingUser || requestInFlight.current) return;
    const accesses = authorizedCountries
      .filter((country) => drafts[country]?.enabled || drafts[country]?.exists)
      .map((country) => ({
        pais_codigo: country,
        rol: drafts[country].rol,
        activo: drafts[country].enabled,
      }));
    const body: {
      empresa_id: string;
      pais_codigo: CountryCode;
      accesos?: { pais_codigo: CountryCode; rol: Role; activo: boolean }[];
      nombre?: string;
      correo?: string;
    } = { empresa_id: companyId, pais_codigo: countryCode };
    const currentAccesses = new Map(editingUser.accesos.map((access) => [access.pais_codigo, access]));
    const accessChanges = accesses.some((access) => {
      const current = currentAccesses.get(access.pais_codigo);
      return !current || current.rol !== access.rol || current.activo !== access.activo;
    }) || editingUser.accesos.some((access) =>
      authorizedCountries.includes(access.pais_codigo) && !accesses.some((next) => next.pais_codigo === access.pais_codigo),
    );
    if (accessChanges) body.accesos = accesses;
    if (editingUser.perfil_global_editable) {
      if (name.trim() !== editingUser.nombre) body.nombre = name;
      if (email.trim().toLocaleLowerCase() !== editingUser.correo.trim().toLocaleLowerCase()) body.correo = email;
    }
    if (body.accesos === undefined && body.nombre === undefined && body.correo === undefined) {
      setSuccess("No hay cambios para guardar.");
      setModal(null);
      return;
    }
    requestInFlight.current = true;
    setSaving(true);
    setOperationError("");
    setSuccess("");
    try {
      const query = new URLSearchParams({ empresa_id: companyId, pais_codigo: countryCode });
      const response = await apiRequest(`/api/v1/administracion/usuarios/${editingUser.id}/?${query}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      const pendingEmail = typeof response === "object" && response !== null
        ? (response as { correo_pendiente?: unknown }).correo_pendiente
        : null;
      setSuccess(typeof pendingEmail === "string"
        ? `Se guardaron los cambios. El cambio de correo a ${pendingEmail} está pendiente de confirmación.`
        : responseMessage(response, "Se guardaron los cambios del usuario."));
      setModal(null);
      await loadUsers();
    } catch (error: unknown) {
      setOperationError(error instanceof Error ? error.message : "No se pudieron guardar los cambios del usuario.");
    } finally {
      requestInFlight.current = false;
      setSaving(false);
    }
  }

  async function startDelete(user: ManagedUser) {
    if (requestInFlight.current) return;
    setEditingUser(user);
    setDeletionPreview(null);
    setConfirmPermanentDeletion(false);
    setRetireCountries([]);
    setOperationError("");
    setModal("delete");
    requestInFlight.current = true;
    setSaving(true);
    try {
      const query = new URLSearchParams({ empresa_id: companyId, pais_codigo: countryCode });
      const payload = await apiRequest(`/api/v1/administracion/usuarios/${user.id}/eliminacion/?${query}`);
      const preview = payload as DeletionPreview;
      if (!preview || !Array.isArray(preview.accesos_afectados)) {
        throw new Error("El backend devolvió una vista previa de eliminación inválida.");
      }
      setDeletionPreview(preview);
      setRetireCountries(preview.accesos_afectados.filter((access) => access.activo).map((access) => access.pais_codigo));
    } catch (error: unknown) {
      setOperationError(error instanceof Error ? error.message : "No se pudo consultar el alcance de la operación.");
    } finally {
      requestInFlight.current = false;
      setSaving(false);
    }
  }

  async function performDeletion(action: "retirar_accesos" | "eliminar_perfil") {
    if (!editingUser || requestInFlight.current) return;
    if (action === "eliminar_perfil" && (!deletionPreview?.puede_eliminar_perfil || !confirmPermanentDeletion)) return;
    if (action === "retirar_accesos" && !retireCountries.length) {
      setOperationError("Selecciona al menos un acceso activo para retirar.");
      return;
    }
    requestInFlight.current = true;
    setSaving(true);
    setOperationError("");
    setSuccess("");
    try {
      const query = new URLSearchParams({ empresa_id: companyId, pais_codigo: countryCode });
      const response = await apiRequest(`/api/v1/administracion/usuarios/${editingUser.id}/eliminacion/?${query}`, {
        method: "DELETE",
        body: JSON.stringify({
          accion: action,
          ...(action === "retirar_accesos" ? { paises: retireCountries } : { confirmar: true }),
        }),
      });
      setSuccess(responseMessage(response, "Se completó la operación."));
      setModal(null);
      await loadUsers();
    } catch (error: unknown) {
      setOperationError(error instanceof Error ? error.message : "No se pudo completar la operación.");
    } finally {
      requestInFlight.current = false;
      setSaving(false);
    }
  }

  async function toggleAccess(user: ManagedUser, access: UserAccess) {
    if (requestInFlight.current) return;
    const active = !access.activo;
    requestInFlight.current = true;
    setSaving(true);
    setOperationError("");
    setSuccess("");
    try {
      const query = new URLSearchParams({ empresa_id: companyId, pais_codigo: countryCode });
      const response = await apiRequest(`/api/v1/administracion/usuarios/${user.id}/accesos/?${query}`, {
        method: "PATCH",
        body: JSON.stringify({
          empresa_id: companyId,
          pais_codigo: countryCode,
          accesos: [{ pais_codigo: access.pais_codigo, rol: access.rol, activo: active }],
        }),
      });
      setSuccess(responseMessage(response, `${active ? "Se reactivó" : "Se desactivó"} el acceso a ${access.pais}.`));
      await loadUsers();
    } catch (error: unknown) {
      setOperationError(error instanceof Error ? error.message : "No se pudo cambiar el acceso.");
    } finally {
      requestInFlight.current = false;
      setSaving(false);
    }
  }

  async function sendPasswordReset(user: ManagedUser) {
    if (requestInFlight.current) return;
    requestInFlight.current = true;
    setSaving(true);
    setOperationError("");
    setSuccess("");
    try {
      const query = new URLSearchParams({ empresa_id: companyId, pais_codigo: countryCode });
      const response = await apiRequest(
        `/api/v1/administracion/usuarios/${user.id}/restablecer-clave/?${query}`,
        {
          method: "POST",
          body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode }),
        },
      );
      setSuccess(responseMessage(response, "Se envió el enlace para restablecer la contraseña."));
    } catch (error: unknown) {
      setOperationError(error instanceof Error ? error.message : "No se pudo enviar el enlace de recuperación.");
    } finally {
      requestInFlight.current = false;
      setSaving(false);
    }
  }

  const roleDescriptions = (
    <section className="user-admin-role-help" aria-label="¿Qué permite cada rol?">
      <h3>¿Qué permite cada rol?</h3>
      <ul>{ROLE_DETAILS.map((role) => (
        <li key={role.id}><strong>{role.label}:</strong> {role.description}</li>
      ))}</ul>
    </section>
  );

  const roleOptions = (value: Role, onChange: (role: Role) => void, id: string) => (
    <select id={id} value={value} onChange={(event) => onChange(event.target.value as Role)}>
      {ROLE_DETAILS.map((role) => <option key={role.id} value={role.id}>{role.label}</option>)}
    </select>
  );

  return (
    <section className="user-admin-module">
      <header className="user-admin-header">
        <div><p className="eyebrow">ACCESOS Y PERMISOS</p><h2>Administración de usuarios</h2><p>Los permisos se administran por empresa y país.</p></div>
        <button className="primary-button" type="button" onClick={() => startCreate()}>Crear usuario</button>
      </header>

      <div className="user-admin-scope">
        <label>Empresa
          <select value={companyId} onChange={(event) => {
            const nextCompany = event.target.value;
            setCompanyId(nextCompany);
            const nextScope = adminScopes.find((scope) => scope.empresa_id === nextCompany);
            if (nextScope) {
              setCountryCode(nextScope.pais_codigo);
              onScopeChange(nextCompany, nextScope.pais_codigo);
            }
          }}>
            {companies.map((company) => <option key={company.empresa_id} value={company.empresa_id}>{company.empresa}</option>)}
          </select>
        </label>
        <label>Administrar país
          <select value={countryCode} onChange={(event) => {
            const nextCountry = event.target.value as CountryCode;
            setCountryCode(nextCountry);
            onScopeChange(companyId, nextCountry);
          }}>
            {scopesForCompany.map((scope) => <option key={scope.pais_codigo} value={scope.pais_codigo}>{scope.pais}</option>)}
          </select>
        </label>
      </div>

      {operationError && <p className="notice error" role="alert">{operationError}</p>}
      {success && <p className="notice success" role="status">{success}</p>}
      {loadError && <p className="notice error" role="alert">{loadError}<button type="button" className="retry-button" onClick={() => void loadUsers()}>Reintentar</button></p>}

      <div className="user-admin-filters">
        <label>Buscar por nombre o correo<input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Nombre o correo" /></label>
        <label>País<select value={countryFilter} onChange={(event) => setCountryFilter(event.target.value as "all" | CountryCode)}><option value="all">Todos los autorizados</option>{authorizedCountries.map((country) => <option key={country} value={country}>{COUNTRY_NAMES[country]}</option>)}</select></label>
        <label>Rol<select value={roleFilter} onChange={(event) => setRoleFilter(event.target.value as "all" | Role)}><option value="all">Todos</option>{ROLE_DETAILS.map((role) => <option key={role.id} value={role.id}>{role.label}</option>)}</select></label>
        <label>Estado<select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)}><option value="all">Todos</option><option value="activo">Activo</option><option value="inactivo">Inactivo</option><option value="pendiente">Configuración pendiente</option></select></label>
      </div>

      <div className="user-admin-table-wrap">
        <table className="user-admin-table">
          <thead><tr><th>Nombre</th><th>Correo</th><th>Accesos por país y rol</th><th>Estado</th><th>Acciones</th></tr></thead>
          <tbody>
            {loading ? <tr><td colSpan={5}>Cargando usuarios…</td></tr> : filteredUsers.length === 0 ? <tr><td colSpan={5}>No hay usuarios para los filtros seleccionados.</td></tr> : filteredUsers.map((user) => (
              <tr key={user.id}>
                <td>{user.nombre}</td>
                <td>
                  <div className="user-admin-email">
                    <span>{user.correo || "Sin correo registrado"}</span>
                    {user.correo_pendiente && <small>Cambio pendiente de confirmación: {user.correo_pendiente}</small>}
                  </div>
                </td>
                <td><div className="user-admin-access-list">{user.accesos.filter((access) => countryFilter === "all" || access.pais_codigo === countryFilter).map((access) => (
                  <div className="user-admin-access" key={access.pais_codigo}>
                    <span><strong>{access.pais}</strong><small>{ROLE_DETAILS.find((role) => role.id === access.rol)?.label ?? access.rol} · {access.activo ? "Activo" : "Desactivado"}</small></span>
                    <button className="quiet-button" disabled={saving || !user.perfil_activo} type="button" onClick={() => void toggleAccess(user, access)}>{access.activo ? "Desactivar acceso" : "Reactivar acceso"}</button>
                  </div>
                ))}</div></td>
                <td><span className={`user-admin-status ${userStatus(user, countryFilter)}`}>{!user.perfil_activo ? "Perfil inactivo" : userStatus(user, countryFilter) === "activo" ? "Activo" : userStatus(user, countryFilter) === "inactivo" ? "Acceso inactivo" : user.cambio_clave_obligatorio ? "Cambio de contraseña pendiente" : user.tiene_identidad_auth ? "Cuenta por configurar" : "Creación pendiente"}</span></td>
                <td><div className="user-admin-actions">
                  <button className="quiet-button" type="button" disabled={saving || !user.perfil_activo} onClick={() => startEdit(user)}>Editar usuario</button>
                  {user.perfil_activo && (user.estado_invitacion === "pendiente" || user.estado_invitacion === "fallida") && (
                    user.tiene_identidad_auth
                      ? <button className="quiet-button" type="button" disabled={saving || !user.perfil_global_editable} onClick={() => startTemporaryPassword(user)}>Asignar contraseña temporal</button>
                      : <button className="quiet-button" type="button" disabled={saving || !user.perfil_global_editable} onClick={() => startCreate(user)}>Completar creación de cuenta</button>
                  )}
                  {!user.perfil_global_editable && user.perfil_activo && (user.estado_invitacion === "pendiente" || user.estado_invitacion === "fallida") && <small className="user-admin-scope-notice">Acción de cuenta no disponible: hay accesos fuera de tu ámbito.</small>}
                  <button className="quiet-button user-admin-danger" type="button" disabled={saving} onClick={() => void startDelete(user)}>Eliminar usuario</button>
                </div></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {modal && <div className="user-admin-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !saving) setModal(null); }}>
        <section className="user-admin-modal" role="dialog" aria-modal="true" aria-labelledby="user-admin-modal-title">
          <header><h2 id="user-admin-modal-title">{modal === "create" ? "Crear usuario" : modal === "edit" ? "Editar usuario" : modal === "temporary" ? "Asignar contraseña temporal" : "Eliminar usuario"}</h2><button className="quiet-button" type="button" disabled={saving} onClick={() => setModal(null)} aria-label="Cerrar">×</button></header>
          {modal === "delete" ? <div className="user-admin-delete-form">
            {editingUser && <p className="user-admin-editing"><strong>{deletionPreview?.usuario.nombre ?? editingUser.nombre}</strong> · {deletionPreview?.usuario.correo || editingUser.correo || "Sin correo registrado"}</p>}
            {!deletionPreview && !operationError && <p role="status">Consultando accesos y registros vinculados…</p>}
            {deletionPreview && <>
              <p>Alcance de esta operación:</p>
              {deletionPreview.accesos_afectados.length
                ? <ul>{deletionPreview.accesos_afectados.map((access) => <li key={`${access.empresa}:${access.pais_codigo}`}>{access.empresa} · {access.pais} · {ROLE_DETAILS.find((role) => role.id === access.rol)?.label ?? access.rol} · {access.activo ? "Activo" : "Desactivado"}</li>)}</ul>
                : <p>No hay accesos dentro de tu ámbito que puedan modificarse.</p>}
              <p className="user-admin-scope-notice" role="note">Retirar accesos solo los desactiva en los países seleccionados; conserva otros accesos y el historial financiero.</p>
              {deletionPreview.accesos_afectados.some((access) => access.activo) && <fieldset className="user-admin-delete-countries">
                <legend>Accesos que se desactivarán</legend>
                {deletionPreview.accesos_afectados.filter((access) => access.activo).map((access) => (
                  <label key={`${access.empresa}:${access.pais_codigo}`}>
                    <input
                      type="checkbox"
                      checked={retireCountries.includes(access.pais_codigo)}
                      onChange={(event) => setRetireCountries((current) => event.target.checked
                        ? [...current, access.pais_codigo]
                        : current.filter((country) => country !== access.pais_codigo))}
                    />
                    {access.empresa} · {access.pais}
                  </label>
                ))}
              </fieldset>}
              {deletionPreview.puede_eliminar_perfil && <fieldset className="user-admin-delete-countries" disabled={saving}>
                <legend>Eliminación definitiva</legend>
                <p>Se eliminarán la cuenta de acceso y todos sus permisos. Esta acción es irreversible.
                  {deletionPreview.referencias_historicas && " El historial conservará el nombre asociado a las operaciones anteriores."}
                </p>
                <label>
                  <input
                    type="checkbox"
                    checked={confirmPermanentDeletion}
                    onChange={(event) => setConfirmPermanentDeletion(event.target.checked)}
                  />
                  Confirmo que quiero eliminar definitivamente este usuario.
                </label>
              </fieldset>}
              {deletionPreview.motivo_bloqueo && <p className="user-admin-scope-notice" role="note">{deletionPreview.motivo_bloqueo}</p>}
              {operationError && <p className="notice error" role="alert">{operationError}</p>}
              <footer>
                <button className="quiet-button" type="button" disabled={saving} onClick={() => setModal(null)}>Cancelar</button>
                {deletionPreview.accesos_afectados.some((access) => access.activo) && <button className="quiet-button" type="button" disabled={saving || !retireCountries.length} onClick={() => void performDeletion("retirar_accesos")}>Desactivar accesos seleccionados</button>}
                {deletionPreview.puede_eliminar_perfil && <button className="primary-button user-admin-danger" type="button" disabled={saving || !confirmPermanentDeletion} onClick={() => void performDeletion("eliminar_perfil")}>Eliminar definitivamente</button>}
              </footer>
            </>}
            {!deletionPreview && operationError && <>
                <p className="notice error" role="alert">{operationError}</p>
                <footer>
                  <button className="quiet-button" type="button" disabled={saving} onClick={() => setModal(null)}>Cancelar</button>
                  <button className="quiet-button" type="button" disabled={saving} onClick={() => editingUser && void startDelete(editingUser)}>Reintentar</button>
                </footer>
            </>}
          </div> : modal === "temporary" ? <form onSubmit={assignTemporaryPassword}>
            <p className="user-admin-editing"><strong>{editingUser?.nombre}</strong> · {editingUser?.correo || "Sin correo registrado"}</p>
            <p className="user-admin-scope-notice" role="note">La contraseña se asigna a la identidad existente y deberá cambiarse al ingresar. No se mostrarán ni se recuperarán contraseñas actuales.</p>
            <label>Contraseña temporal
              <input autoFocus required minLength={8} maxLength={128} autoComplete="new-password" type={showCredentialPassword ? "text" : "password"} value={credentialPassword} onChange={(event) => setCredentialPassword(event.target.value)} />
            </label>
            <div className="user-admin-password-actions">
              <button className="quiet-button" type="button" onClick={() => setShowCredentialPassword((visible) => !visible)}>{showCredentialPassword ? "Ocultar contraseña" : "Mostrar contraseña"}</button>
              <button className="quiet-button" type="button" onClick={generateTemporaryPassword}>Generar contraseña temporal</button>
            </div>
            {operationError && <p className="notice error" role="alert">{operationError}</p>}
            <footer><button className="quiet-button" type="button" disabled={saving} onClick={() => setModal(null)}>Cancelar</button><button className="primary-button" type="submit" disabled={saving}>{saving ? "Asignando…" : "Asignar contraseña temporal"}</button></footer>
          </form> : <form onSubmit={modal === "create" ? saveUserCreation : saveUserChanges}>
            {modal === "create" ? <>
              {editingUser
                ? <p className="user-admin-editing"><strong>{name}</strong> · {email}</p>
                : <>
                  <label>Nombre<input autoFocus required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
                  <label>Correo electrónico<input required type="email" maxLength={320} value={email} onChange={(event) => setEmail(event.target.value)} /></label>
                </>}
              <label>Contraseña inicial
                <input required minLength={8} maxLength={128} autoComplete="new-password" type={showCredentialPassword ? "text" : "password"} value={credentialPassword} onChange={(event) => setCredentialPassword(event.target.value)} />
              </label>
              <div className="user-admin-password-actions">
                <button className="quiet-button" type="button" onClick={() => setShowCredentialPassword((visible) => !visible)}>{showCredentialPassword ? "Ocultar contraseña" : "Mostrar contraseña"}</button>
                <button className="quiet-button" type="button" onClick={generateTemporaryPassword}>Generar contraseña temporal</button>
              </div>
            </> : <>
              {editingUser?.perfil_global_editable ? <>
                <label>Nombre<input autoFocus required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>
                <label>Correo electrónico<input required type="email" maxLength={320} value={email} onChange={(event) => setEmail(event.target.value)} /></label>
              </> : <>
                <p className="user-admin-editing">{editingUser?.nombre} · {editingUser?.correo || "Sin correo registrado"}</p>
                <p className="user-admin-scope-notice" role="note">No puedes cambiar el nombre ni el correo global porque este usuario también tiene accesos fuera de tu ámbito. Puedes editar únicamente los accesos autorizados.</p>
              </>}
              {editingUser?.correo_pendiente && <p className="user-admin-editing">Cambio de correo pendiente de confirmación: {editingUser.correo_pendiente}</p>}
              {editingUser && !editingUser.perfil_global_editable && <p className="user-admin-scope-notice" role="note">El restablecimiento de contraseña no está disponible porque la identidad tiene accesos fuera de tu ámbito.</p>}
            </>}
            {roleDescriptions}
            <fieldset className="user-admin-country-roles"><legend>Accesos autorizados</legend>
              {authorizedCountries.map((country) => {
                const draft = drafts[country] ?? { enabled: false, rol: "consulta" as Role, exists: false };
                return <div className="user-admin-country-role" key={country}>
                  <label className="user-admin-country-check"><input type="checkbox" checked={draft.enabled} onChange={(event) => updateDraft(country, { enabled: event.target.checked })} /><strong>{COUNTRY_NAMES[country]}</strong><span>Acceso {draft.enabled ? "activo" : "inactivo"}</span></label>
                  <label>Rol por país{roleOptions(draft.rol, (rol) => updateDraft(country, { rol }), `user-role-${country}`)}</label>
                  <small>{ROLE_DETAILS.find((role) => role.id === draft.rol)?.description}</small>
                </div>;
              })}
            </fieldset>
            {modal === "edit" && editingUser?.perfil_global_editable && <button className="quiet-button user-admin-reset-password" type="button" disabled={saving} onClick={() => void sendPasswordReset(editingUser)}>Enviar enlace para restablecer contraseña</button>}
            {operationError && <p className="notice error" role="alert">{operationError}</p>}
            <footer><button className="quiet-button" type="button" disabled={saving} onClick={() => setModal(null)}>Cancelar</button><button className="primary-button" type="submit" disabled={saving}>{saving ? "Guardando…" : modal === "create" ? "Crear cuenta" : "Guardar cambios"}</button></footer>
          </form>}
        </section>
      </div>}
    </section>
  );
}
