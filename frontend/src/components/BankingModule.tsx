import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import type { Session } from "@supabase/supabase-js";

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "");
type Bank = { id: string; nombre: string; activo: boolean };
type Account = { id: string; banco: string; banco_id?: string | null; alias: string; numero: string; numero_completo?: string | null; terminacion?: string | null; moneda_codigo: string; codigo_contable?: string | null; activa: boolean; banco_activo: boolean; pendiente_configuracion?: boolean };
type Currency = { codigo: string; nombre: string };
type BankingQueryData = {
  accounts: Account[];
  banks: Bank[];
  currencies: Currency[];
  totalPages: Record<"accounts" | "banks", number>;
  accountsLoaded: boolean;
  banksLoaded: boolean;
};
type ModalType = "bank-create" | "bank-edit" | "account-create" | "account-edit" | "delete-bank" | "delete-account" | "delete-catalog-account" | null;

function payloadMessage(payload: unknown, fallback: string) {
  if (typeof payload === "object" && payload !== null) {
    const record = payload as Record<string, unknown>;
    if (typeof record.detail === "string") return record.detail;
    if (typeof record.message === "string") return record.message;
  }
  return fallback;
}

function Modal({ title, size, opener, onClose, children }: { title: string; size: "bank" | "account" | "confirm"; opener: HTMLElement | null; onClose: () => void; children: ReactNode }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const firstField = panelRef.current?.querySelector<HTMLElement>("input:not([disabled]), select:not([disabled])");
      (firstField ?? closeButtonRef.current)?.focus();
    }, 0);
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault(); closeRef.current(); return;
      }
      if (event.key !== "Tab") return;
      const focusable = panelRef.current?.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])");
      if (!focusable?.length) return;
      const first = focusable[0]; const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", handleKey);
    return () => { window.clearTimeout(timer); document.removeEventListener("keydown", handleKey); window.setTimeout(() => opener?.focus(), 0); };
  }, []);

  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><div className={`modal-panel modal-${size}`} role="dialog" aria-modal="true" aria-labelledby="modal-title" ref={panelRef}><div className="modal-header"><h2 id="modal-title">{title}</h2><button ref={closeButtonRef} className="modal-close" aria-label="Cerrar" type="button" onClick={onClose}>×</button></div>{children}</div></div>;
}

export default function BankingModule({ session, companyId, countryCode, role, onViewAccount, onViewBank }: { session: Session; companyId: string; countryCode: "SV" | "GT"; role: "administrador" | "auxiliar" | "revisor" | "consulta"; onViewAccount?: (accountId: string) => void; onViewBank?: (bankId: string) => void }) {
  const [activeTab, setActiveTab] = useState<"accounts" | "banks">("accounts");
  const [queryCache, setQueryCache] = useState<Record<string, BankingQueryData>>({});
  const [search, setSearch] = useState(""); const [activeFilter, setActiveFilter] = useState<"all" | "true" | "false">("all");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false); const [saving, setSaving] = useState(false);
  const [queryError, setQueryError] = useState(""); const [operationError, setOperationError] = useState(""); const [success, setSuccess] = useState("");
  const [modal, setModal] = useState<ModalType>(null); const openerRef = useRef<HTMLElement | null>(null);
  const [editingBankId, setEditingBankId] = useState(""); const [bankName, setBankName] = useState(""); const [bankActive, setBankActive] = useState(true);
  const [editingAccountId, setEditingAccountId] = useState(""); const [accountBankId, setAccountBankId] = useState(""); const [accountCurrency, setAccountCurrency] = useState(""); const [accountNumber, setAccountNumber] = useState(""); const [accountAlias, setAccountAlias] = useState(""); const [accountLedgerCode, setAccountLedgerCode] = useState(""); const [accountActive, setAccountActive] = useState(true);
  const [editingCatalogId, setEditingCatalogId] = useState("");
  const [deleteLabel, setDeleteLabel] = useState("");
  const [deleteDetails, setDeleteDetails] = useState<{ banco: string; alias: string; numero: string; moneda: string } | null>(null);
  const generation = useRef(0); const previousSearch = useRef(search);
  const accessToken = useRef(session.access_token);
  accessToken.current = session.access_token;
  const isAdmin = role === "administrador";
  const queryKey = JSON.stringify([session.user.id, companyId, countryCode, page, activeFilter, search]);
  const queryData = queryCache[queryKey];
  const accounts = queryData?.accounts ?? [];
  const banks = queryData?.banks ?? [];
  const currencies = queryData?.currencies ?? [];
  const totalPages = queryData?.totalPages[activeTab] ?? 1;
  const loaded = activeTab === "accounts" ? Boolean(queryData?.accountsLoaded) : Boolean(queryData?.banksLoaded);

  async function apiRequest(path: string, options: RequestInit = {}) {
    if (!apiBaseUrl) throw new Error("Falta configurar VITE_API_BASE_URL en frontend/.env.");
    const response = await fetch(`${apiBaseUrl}${path}`, { ...options, headers: { Authorization: `Bearer ${accessToken.current}`, "Content-Type": "application/json", ...(options.headers ?? {}) } });
    const payload = await response.json().catch(() => null); if (!response.ok) throw new Error(`${path}: ${payloadMessage(payload, `HTTP ${response.status}`)}`); return payload;
  }

  async function load() {
    const current = ++generation.current; setLoading(true); setQueryError("");
    const suffix = `empresa_id=${encodeURIComponent(companyId)}&pais_codigo=${countryCode}&page=${page}&page_size=25&active=${activeFilter === "all" ? "" : activeFilter}&search=${encodeURIComponent(search)}`;
    const requests = [["cuentas", apiRequest(`/api/v1/cuentas/?${suffix}`)], ["bancos", apiRequest(`/api/v1/bancos/?${suffix}`)], ["monedas", apiRequest(`/api/v1/monedas/?empresa_id=${encodeURIComponent(companyId)}&pais_codigo=${countryCode}`)]] as const;
    const settled = await Promise.allSettled(requests.map((item) => item[1])); if (current !== generation.current) return;
    const failures = settled.map((result, index) => ({ result, name: requests[index][0] })).filter((item) => item.result.status === "rejected");
    if (failures.length) { const first = failures[0].result; const detail = first.status === "rejected" && first.reason instanceof Error ? first.reason.message : "Error de conexión al backend."; setQueryError(`Error al consultar ${failures[0].name}: ${detail}`); }
    settled.forEach((result, index) => {
      if (result.status !== "fulfilled") return;
      if (index === 0) {
        const data = result.value as { results: Account[]; count: number };
        setQueryCache((currentCache) => {
          const currentData = currentCache[queryKey] ?? { accounts: [], banks: [], currencies: [], totalPages: { accounts: 1, banks: 1 }, accountsLoaded: false, banksLoaded: false };
          return { ...currentCache, [queryKey]: { ...currentData, accounts: data.results ?? [], accountsLoaded: true, totalPages: { ...currentData.totalPages, accounts: Math.max(1, Math.ceil((data.count ?? 0) / 25)) } } };
        });
      } else if (index === 1) {
        const data = result.value as { results: Bank[]; count: number };
        setQueryCache((currentCache) => {
          const currentData = currentCache[queryKey] ?? { accounts: [], banks: [], currencies: [], totalPages: { accounts: 1, banks: 1 }, accountsLoaded: false, banksLoaded: false };
          return { ...currentCache, [queryKey]: { ...currentData, banks: data.results ?? [], banksLoaded: true, totalPages: { ...currentData.totalPages, banks: Math.max(1, Math.ceil((data.count ?? 0) / 25)) } } };
        });
      } else {
        const currencies = (result.value as Currency[]) ?? [];
        setQueryCache((currentCache) => {
          const currentData = currentCache[queryKey] ?? { accounts: [], banks: [], currencies: [], totalPages: { accounts: 1, banks: 1 }, accountsLoaded: false, banksLoaded: false };
          return { ...currentCache, [queryKey]: { ...currentData, currencies } };
        });
      }
    });
    setLoading(false);
  }

  useEffect(() => {
    const delay = previousSearch.current === search ? 0 : 300;
    const timer = window.setTimeout(() => { previousSearch.current = search; void load(); }, delay);
    return () => { window.clearTimeout(timer); generation.current += 1; };
  }, [page, search, activeFilter, activeTab, companyId, countryCode]);

  const openModal = useCallback((type: ModalType, trigger?: HTMLElement | null) => { openerRef.current = trigger ?? document.activeElement as HTMLElement; setModal(type); }, []);
  function closeModalAndRestore() { setModal(null); }

  function openBankCreate(trigger: HTMLElement) { setEditingBankId(""); setBankName(""); setBankActive(true); openModal("bank-create", trigger); }
  function openBankEdit(bank: Bank, trigger: HTMLElement) { setEditingBankId(bank.id); setBankName(bank.nombre); setBankActive(bank.activo); openModal("bank-edit", trigger); }
  function openAccountCreate(trigger: HTMLElement) { setEditingAccountId(""); setAccountNumber(""); setAccountAlias(""); setAccountLedgerCode(""); setAccountBankId(""); setAccountCurrency(""); setAccountActive(true); openModal("account-create", trigger); }
  function openAccountEdit(account: Account, trigger: HTMLElement) { setEditingAccountId(account.id); setAccountAlias(account.alias); setAccountLedgerCode(account.codigo_contable ?? ""); setAccountActive(account.activa); setAccountBankId(account.banco_id ?? ""); setAccountNumber(account.numero); setAccountCurrency(account.moneda_codigo); openModal("account-edit", trigger); }
  function openDelete(type: "delete-bank" | "delete-account" | "delete-catalog-account", label: string, trigger: HTMLElement, details?: { banco: string; alias: string; numero: string; moneda: string }) {
    setDeleteLabel(label);
    setDeleteDetails(details ?? null);
    openModal(type, trigger);
  }

  async function saveBank(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setSaving(true); setSuccess(""); setOperationError("");
    try { if (modal === "bank-edit") await apiRequest(`/api/v1/bancos/${editingBankId}/`, { method: "PATCH", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, nombre: bankName, activo: bankActive }) }); else await apiRequest("/api/v1/bancos/", { method: "POST", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, nombre: bankName }) }); closeModalAndRestore(); setSuccess(modal === "bank-edit" ? "Banco actualizado correctamente." : "Banco registrado correctamente."); await load(); }
    catch (error) { setOperationError(error instanceof Error ? error.message : "No se pudo guardar el banco."); } finally { setSaving(false); }
  }
  async function saveAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setSaving(true); setSuccess(""); setOperationError("");
    try { if (modal === "account-edit") await apiRequest(`/api/v1/cuentas/${editingAccountId}/`, { method: "PATCH", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, alias: accountAlias, activa: accountActive, codigo_contable: accountLedgerCode.trim() || null }) }); else await apiRequest("/api/v1/cuentas/", { method: "POST", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, banco_id: accountBankId, moneda_codigo: accountCurrency, numero: accountNumber, alias: accountAlias, codigo_contable: accountLedgerCode.trim() || null }) }); closeModalAndRestore(); setSuccess(modal === "account-edit" ? "Cuenta actualizada correctamente." : "Cuenta registrada correctamente."); await load(); }
    catch (error) { setOperationError(error instanceof Error ? error.message : "No se pudo guardar la cuenta."); } finally { setSaving(false); }
  }
  async function completeCatalogAccount(account: Account) {
    if (!isAdmin) return;
    let moneda = account.moneda_codigo === "Pendiente" ? "" : account.moneda_codigo;
    if (!moneda) {
      moneda = window.prompt("Selecciona la moneda indispensable (GTQ o USD):", "")?.trim().toUpperCase() ?? "";
      if (!["GTQ", "USD"].includes(moneda)) {
        setOperationError("No se activó la cuenta: falta seleccionar GTQ o USD.");
        return;
      }
    }
    setSaving(true); setSuccess(""); setOperationError("");
    try {
      await apiRequest("/api/v1/cuentas/", {
        method: "POST",
        body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, catalogo_id: account.id, moneda_codigo: moneda }),
      });
      setSuccess(`Cuenta ${account.alias} incorporada o reutilizada sin duplicados.`);
      await load();
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : "No se pudo incorporar la cuenta autorizada.");
    } finally {
      setSaving(false);
    }
  }
  async function patchBank(bank: Bank, changes: { nombre?: string; activo?: boolean }) { setSaving(true); setSuccess(""); setOperationError(""); try { await apiRequest(`/api/v1/bancos/${bank.id}/`, { method: "PATCH", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, ...changes }) }); setSuccess("Banco actualizado correctamente."); await load(); } catch (error) { setOperationError(error instanceof Error ? error.message : "No se pudo actualizar el banco."); } finally { setSaving(false); } }
  async function patchAccount(account: Account, changes: { alias?: string; activa?: boolean }) { setSaving(true); setSuccess(""); setOperationError(""); try { await apiRequest(`/api/v1/cuentas/${account.id}/`, { method: "PATCH", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, ...changes }) }); setSuccess("Cuenta actualizada correctamente."); await load(); } catch (error) { setOperationError(error instanceof Error ? error.message : "No se pudo actualizar la cuenta."); } finally { setSaving(false); } }
  async function confirmDelete() {
    const type = modal;
    setSaving(true); setSuccess(""); setOperationError("");
    try {
      if (type === "delete-bank") {
        await apiRequest(`/api/v1/bancos/${editingBankId}/?empresa_id=${encodeURIComponent(companyId)}&pais_codigo=${countryCode}`, { method: "DELETE" });
        setSuccess("Banco eliminado correctamente.");
      } else if (type === "delete-catalog-account") {
        await apiRequest(`/api/v1/catalogo-cuentas/${editingCatalogId}/retiro/?empresa_id=${encodeURIComponent(companyId)}&pais_codigo=${countryCode}`, { method: "DELETE" });
        setSuccess("Entrada retirada correctamente para esta empresa.");
      } else if (type === "delete-account") {
        await apiRequest(`/api/v1/cuentas/${editingAccountId}/?empresa_id=${encodeURIComponent(companyId)}&pais_codigo=${countryCode}`, { method: "DELETE" });
        setSuccess("Cuenta eliminada correctamente.");
      }
      closeModalAndRestore();
      await load();
    } catch (error) {
      setOperationError(error instanceof Error ? error.message : "No se pudo eliminar el registro.");
    } finally {
      setSaving(false);
    }
  }

  return <div className="bank-module">
    <div className="module-tabs" role="tablist"><button role="tab" aria-selected={activeTab === "accounts"} className={activeTab === "accounts" ? "active" : ""} onClick={() => { setActiveTab("accounts"); setPage(1); }}>Cuentas bancarias</button><button role="tab" aria-selected={activeTab === "banks"} className={activeTab === "banks" ? "active" : ""} onClick={() => { setActiveTab("banks"); setPage(1); }}>Bancos</button></div>
    <div className="table-toolbar"><div className="table-toolbar-search"><label><span>Búsqueda</span><input aria-label="Buscar" placeholder={activeTab === "accounts" ? "Cuenta, banco o alias" : "Nombre del banco"} value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); }} /></label><label><span>Estado</span><select aria-label="Estado" value={activeFilter} onChange={(event) => { setActiveFilter(event.target.value as "all" | "true" | "false"); setPage(1); }}><option value="all">Todos</option><option value="true">Activos</option><option value="false">Inactivos</option></select></label></div>{isAdmin && <button className="primary-button toolbar-button" type="button" onClick={(event) => activeTab === "accounts" ? openAccountCreate(event.currentTarget) : openBankCreate(event.currentTarget)}>{activeTab === "accounts" ? "Nueva cuenta" : "Nuevo banco"}</button>}</div>
    {success && <div className="notice success" role="status">{success}</div>}
    {loading && !loaded && <div className="notice warning" role="status">Cargando información…</div>}
    {loading && loaded && <small role="status">Actualizando…</small>}
    {queryError && <div className="notice error" role="alert"><strong>No se pudieron actualizar los datos.</strong><span>{queryError}</span><button className="retry-button" type="button" onClick={() => void load()}>Reintentar</button></div>}
    {operationError && <div className="notice error" role="alert"><strong>No se pudo completar la operación.</strong><span>{operationError}</span></div>}
    {loaded && activeTab === "accounts" && (accounts.length === 0 ? <div className="notice empty">No hay cuentas que coincidan con los filtros.</div> : <div className="data-table"><table><thead><tr><th>Banco</th><th>Alias</th><th>Número</th><th>Moneda</th><th>Estado</th><th>Acciones</th></tr></thead><tbody>{accounts.map((account) => <tr key={account.id}><td>{account.banco}{!account.banco_activo && <small className="table-note">Banco inactivo</small>}</td><td>{account.alias}</td><td>{account.numero_completo ?? (account.terminacion ? `Terminación ${account.terminacion}` : account.numero)}</td><td>{account.moneda_codigo}</td><td><span className={account.pendiente_configuracion ? "status-muted" : account.activa ? "status-ready" : "status-muted"}>{account.pendiente_configuracion ? "Pendiente de configuración" : account.activa ? "Activa" : "Inactiva"}</span></td><td><div className="row-actions">{!account.id.startsWith("catalogo:") && <button className="row-action" type="button" onClick={() => onViewAccount?.(account.id)}>Ver conciliaciones</button>}    {isAdmin && (account.id.startsWith("catalogo:")
      ? <><button className="row-action" type="button" disabled={saving || Boolean(account.terminacion)} onClick={() => void completeCatalogAccount(account)}>{account.terminacion ? "Pendiente de número completo" : account.moneda_codigo === "Pendiente" ? "Completar configuración" : "Activar cuenta"}</button><button className="row-action danger" type="button" disabled={saving} onClick={(event) => { setEditingCatalogId(account.id.replace("catalogo:", "")); openDelete("delete-catalog-account", `Entrada de catálogo ${account.alias}`, event.currentTarget, { banco: account.banco, alias: account.alias, numero: account.numero_completo ?? account.terminacion ?? account.numero, moneda: account.moneda_codigo }); }}>Eliminar</button></>
      : <><button className="row-action" type="button" onClick={(event) => openAccountEdit(account, event.currentTarget)}>Editar</button><button className="row-action" type="button" disabled={Boolean(account.pendiente_configuracion) || (!account.activa && !account.banco_activo)} onClick={() => void patchAccount(account, { activa: !account.activa })}>{account.activa ? "Desactivar" : "Activar cuenta"}</button><button className="row-action danger" type="button" onClick={(event) => { setEditingAccountId(account.id); openDelete("delete-account", `Cuenta ${account.numero} · ${account.alias}`, event.currentTarget, { banco: account.banco, alias: account.alias, numero: account.numero_completo ?? account.numero, moneda: account.moneda_codigo }); }}>Eliminar</button></>)}</div></td></tr>)}</tbody></table></div>)}
    {loaded && activeTab === "banks" && (banks.length === 0 ? <div className="notice empty">No hay bancos que coincidan con los filtros.</div> : <div className="data-table"><table><thead><tr><th>Nombre</th><th>Estado</th><th>Acciones</th></tr></thead><tbody>{banks.map((bank) => <tr key={bank.id}><td>{bank.nombre}</td><td><span className={bank.activo ? "status-ready" : "status-muted"}>{bank.activo ? "Activo" : "Inactivo"}</span></td><td><div className="row-actions"><button className="row-action" type="button" onClick={() => onViewBank?.(bank.id)}>Ver conciliaciones</button>{isAdmin && <><button className="row-action" type="button" onClick={(event) => openBankEdit(bank, event.currentTarget)}>Editar</button><button className="row-action" type="button" onClick={() => void patchBank(bank, { activo: !bank.activo })}>{bank.activo ? "Desactivar" : "Activar"}</button><button className="row-action danger" type="button" onClick={(event) => { setEditingBankId(bank.id); openDelete("delete-bank", bank.nombre, event.currentTarget); }}>Eliminar</button></>}</div></td></tr>)}</tbody></table></div>)}
    {loaded && <div className="pagination"><button className="quiet-button" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>Anterior</button><span>Página {page} de {totalPages}</span><button className="quiet-button" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>Siguiente</button></div>}

    {(modal === "bank-create" || modal === "bank-edit") && <Modal title={modal === "bank-create" ? "Nuevo banco" : "Editar banco"} size="bank" opener={openerRef.current} onClose={closeModalAndRestore}><form className="modal-form" onSubmit={saveBank}><label htmlFor="bank-name">Nombre del banco<input id="bank-name" required value={bankName} onChange={(event) => setBankName(event.target.value)} /></label>{modal === "bank-edit" && <label>Estado<select value={bankActive ? "true" : "false"} onChange={(event) => setBankActive(event.target.value === "true")}><option value="true">Activo</option><option value="false">Inactivo</option></select></label>}<div className="modal-actions"><button className="quiet-button" type="button" onClick={closeModalAndRestore}>Cancelar</button><button className="primary-button compact" disabled={saving} type="submit">Guardar</button></div></form></Modal>}
    {(modal === "account-create" || modal === "account-edit") && <Modal title={modal === "account-create" ? "Nueva cuenta bancaria" : "Editar cuenta"} size="account" opener={openerRef.current} onClose={closeModalAndRestore}><form className="modal-form account-form-grid" onSubmit={saveAccount}>{modal === "account-create" ? <><label htmlFor="account-bank">Banco<select id="account-bank" required value={accountBankId} onChange={(event) => setAccountBankId(event.target.value)}><option value="">Selecciona banco activo</option>{banks.filter((bank) => bank.activo).map((bank) => <option key={bank.id} value={bank.id}>{bank.nombre}</option>)}</select></label><label htmlFor="account-currency">Moneda<select id="account-currency" required value={accountCurrency} onChange={(event) => setAccountCurrency(event.target.value)}><option value="">Selecciona moneda</option>{currencies.map((currency) => <option key={currency.codigo} value={currency.codigo}>{currency.codigo} · {currency.nombre}</option>)}</select></label><label htmlFor="account-number">Número de cuenta bancaria<input id="account-number" required inputMode="text" value={accountNumber} onChange={(event) => setAccountNumber(event.target.value)} /></label></> : <><div className="read-only-field"><span>Banco</span><strong>{accounts.find((item) => item.id === editingAccountId)?.banco}</strong></div><div className="read-only-field"><span>Número de cuenta bancaria</span><strong>{accountNumber}</strong></div><div className="read-only-field"><span>Moneda</span><strong>{accountCurrency}</strong></div></>}<label htmlFor="account-alias">Alias<input id="account-alias" required value={accountAlias} onChange={(event) => setAccountAlias(event.target.value)} /></label><label htmlFor="account-ledger-code">Código de cuenta contable (opcional)<input id="account-ledger-code" inputMode="text" value={accountLedgerCode} onChange={(event) => setAccountLedgerCode(event.target.value)} /><small>No es el número de cuenta bancaria. Debe corresponder al código usado en el mayor contable.</small></label>{modal === "account-edit" && <label htmlFor="account-active">Estado<select id="account-active" value={accountActive ? "true" : "false"} onChange={(event) => setAccountActive(event.target.value === "true")}><option value="true">Activa</option><option value="false">Inactiva</option></select></label>}<div className="modal-actions"><button className="quiet-button" type="button" onClick={closeModalAndRestore}>Cancelar</button><button className="primary-button compact" disabled={saving} type="submit">Guardar</button></div></form></Modal>}
    {(modal === "delete-bank" || modal === "delete-account" || modal === "delete-catalog-account") && <Modal title="Confirmar eliminación" size="confirm" opener={openerRef.current} onClose={closeModalAndRestore}><p>{modal === "delete-catalog-account" ? "¿Retirar" : "¿Eliminar"} <strong>{deleteLabel}</strong>?</p>{deleteDetails && <dl className="delete-account-details"><div><dt>Banco</dt><dd>{deleteDetails.banco}</dd></div><div><dt>Alias</dt><dd>{deleteDetails.alias}</dd></div><div><dt>Número</dt><dd>{deleteDetails.numero}</dd></div><div><dt>Moneda</dt><dd>{deleteDetails.moneda}</dd></div></dl>}<p className="delete-warning">{modal === "delete-catalog-account" ? "La entrada se retirará solo para esta empresa y país. No se modifica el catálogo compartido." : "Solo se permitirá si el registro no tiene información asociada."}</p><div className="modal-actions"><button className="quiet-button" type="button" onClick={closeModalAndRestore}>Cancelar</button><button className="danger-button" disabled={saving} type="button" onClick={() => void confirmDelete()}>{modal === "delete-catalog-account" ? "Retirar" : "Eliminar"}</button></div></Modal>}
  </div>;
}
