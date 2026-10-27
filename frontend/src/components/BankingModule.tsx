import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import type { Session } from "@supabase/supabase-js";

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "");
type Bank = { id: string; nombre: string; activo: boolean };
type Account = { id: string; banco: string; banco_id?: string; alias: string; numero: string; moneda_codigo: string; activa: boolean; banco_activo: boolean };
type Currency = { codigo: string; nombre: string };
type ModalType = "bank-create" | "bank-edit" | "account-create" | "account-edit" | "delete-bank" | "delete-account" | null;

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

export default function BankingModule({ session, companyId, countryCode, role }: { session: Session; companyId: string; countryCode: "SV" | "GT"; role: "administrador" | "auxiliar" | "revisor" | "consulta" }) {
  const [activeTab, setActiveTab] = useState<"accounts" | "banks">("accounts");
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [banks, setBanks] = useState<Bank[]>([]);
  const [currencies, setCurrencies] = useState<Currency[]>([]);
  const [search, setSearch] = useState(""); const [activeFilter, setActiveFilter] = useState<"all" | "true" | "false">("all");
  const [page, setPage] = useState(1); const [totalPages, setTotalPages] = useState(1);
  const [loading, setLoading] = useState(false); const [queryError, setQueryError] = useState(""); const [success, setSuccess] = useState("");
  const [modal, setModal] = useState<ModalType>(null); const openerRef = useRef<HTMLElement | null>(null);
  const [editingBankId, setEditingBankId] = useState(""); const [bankName, setBankName] = useState(""); const [bankActive, setBankActive] = useState(true);
  const [editingAccountId, setEditingAccountId] = useState(""); const [accountBankId, setAccountBankId] = useState(""); const [accountCurrency, setAccountCurrency] = useState(""); const [accountNumber, setAccountNumber] = useState(""); const [accountAlias, setAccountAlias] = useState(""); const [accountActive, setAccountActive] = useState(true);
  const [deleteLabel, setDeleteLabel] = useState(""); const generation = useRef(0);
  const isAdmin = role === "administrador";

  async function apiRequest(path: string, options: RequestInit = {}) {
    if (!apiBaseUrl) throw new Error("Falta configurar VITE_API_BASE_URL en frontend/.env.");
    const response = await fetch(`${apiBaseUrl}${path}`, { ...options, headers: { Authorization: `Bearer ${session.access_token}`, "Content-Type": "application/json", ...(options.headers ?? {}) } });
    const payload = await response.json().catch(() => null); if (!response.ok) throw new Error(`${path}: ${payloadMessage(payload, `HTTP ${response.status}`)}`); return payload;
  }

  async function load() {
    const current = ++generation.current; setLoading(true); setQueryError(""); setAccounts([]); setBanks([]);
    const suffix = `empresa_id=${encodeURIComponent(companyId)}&pais_codigo=${countryCode}&page=${page}&page_size=25&active=${activeFilter === "all" ? "" : activeFilter}&search=${encodeURIComponent(search)}`;
    const requests = [["cuentas", apiRequest(`/api/v1/cuentas/?${suffix}`)], ["bancos", apiRequest(`/api/v1/bancos/?${suffix}`)], ["monedas", apiRequest(`/api/v1/monedas/?empresa_id=${encodeURIComponent(companyId)}&pais_codigo=${countryCode}`)]] as const;
    const settled = await Promise.allSettled(requests.map((item) => item[1])); if (current !== generation.current) return;
    const failures = settled.map((result, index) => ({ result, name: requests[index][0] })).filter((item) => item.result.status === "rejected");
    if (failures.length) { const first = failures[0].result; const detail = first.status === "rejected" && first.reason instanceof Error ? first.reason.message : "Error de conexión al backend."; setQueryError(`Error al consultar ${failures[0].name}: ${detail}`); }
    settled.forEach((result, index) => { if (result.status !== "fulfilled") return; if (index === 0) { const data = result.value as { results: Account[]; count: number }; setAccounts(data.results ?? []); if (activeTab === "accounts") setTotalPages(Math.max(1, Math.ceil((data.count ?? 0) / 25))); } if (index === 1) { const data = result.value as { results: Bank[]; count: number }; setBanks(data.results ?? []); if (activeTab === "banks") setTotalPages(Math.max(1, Math.ceil((data.count ?? 0) / 25))); } if (index === 2) setCurrencies((result.value as Currency[]) ?? []); });
    setLoading(false);
  }

  useEffect(() => { generation.current += 1; setAccounts([]); setBanks([]); setCurrencies([]); setQueryError(""); setPage(1); void load(); }, [companyId, countryCode]);
  useEffect(() => { void load(); }, [page, search, activeFilter, activeTab]);

  const openModal = useCallback((type: ModalType, trigger?: HTMLElement | null) => { openerRef.current = trigger ?? document.activeElement as HTMLElement; setModal(type); }, []);
  function closeModalAndRestore() { setModal(null); }

  function openBankCreate(trigger: HTMLElement) { setEditingBankId(""); setBankName(""); setBankActive(true); openModal("bank-create", trigger); }
  function openBankEdit(bank: Bank, trigger: HTMLElement) { setEditingBankId(bank.id); setBankName(bank.nombre); setBankActive(bank.activo); openModal("bank-edit", trigger); }
  function openAccountCreate(trigger: HTMLElement) { setEditingAccountId(""); setAccountNumber(""); setAccountAlias(""); setAccountBankId(""); setAccountCurrency(""); setAccountActive(true); openModal("account-create", trigger); }
  function openAccountEdit(account: Account, trigger: HTMLElement) { setEditingAccountId(account.id); setAccountAlias(account.alias); setAccountActive(account.activa); setAccountBankId(account.banco_id ?? ""); setAccountNumber(account.numero); setAccountCurrency(account.moneda_codigo); openModal("account-edit", trigger); }
  function openDelete(type: "delete-bank" | "delete-account", label: string, trigger: HTMLElement) { setDeleteLabel(label); openModal(type, trigger); }

  async function saveBank(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setLoading(true); setSuccess(""); setQueryError("");
    try { if (modal === "bank-edit") await apiRequest(`/api/v1/bancos/${editingBankId}/`, { method: "PATCH", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, nombre: bankName, activo: bankActive }) }); else await apiRequest("/api/v1/bancos/", { method: "POST", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, nombre: bankName }) }); closeModalAndRestore(); setSuccess(modal === "bank-edit" ? "Banco actualizado correctamente." : "Banco registrado correctamente."); await load(); }
    catch (error) { setQueryError(error instanceof Error ? error.message : "No se pudo guardar el banco."); } finally { setLoading(false); }
  }
  async function saveAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setLoading(true); setSuccess(""); setQueryError("");
    try { if (modal === "account-edit") await apiRequest(`/api/v1/cuentas/${editingAccountId}/`, { method: "PATCH", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, alias: accountAlias, activa: accountActive }) }); else await apiRequest("/api/v1/cuentas/", { method: "POST", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, banco_id: accountBankId, moneda_codigo: accountCurrency, numero: accountNumber, alias: accountAlias }) }); closeModalAndRestore(); setSuccess(modal === "account-edit" ? "Cuenta actualizada correctamente." : "Cuenta registrada correctamente."); await load(); }
    catch (error) { setQueryError(error instanceof Error ? error.message : "No se pudo guardar la cuenta."); } finally { setLoading(false); }
  }
  async function patchBank(bank: Bank, changes: { nombre?: string; activo?: boolean }) { setLoading(true); setSuccess(""); setQueryError(""); try { await apiRequest(`/api/v1/bancos/${bank.id}/`, { method: "PATCH", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, ...changes }) }); setSuccess("Banco actualizado correctamente."); await load(); } catch (error) { setQueryError(error instanceof Error ? error.message : "No se pudo actualizar el banco."); } finally { setLoading(false); } }
  async function patchAccount(account: Account, changes: { alias?: string; activa?: boolean }) { setLoading(true); setSuccess(""); setQueryError(""); try { await apiRequest(`/api/v1/cuentas/${account.id}/`, { method: "PATCH", body: JSON.stringify({ empresa_id: companyId, pais_codigo: countryCode, ...changes }) }); setSuccess("Cuenta actualizada correctamente."); await load(); } catch (error) { setQueryError(error instanceof Error ? error.message : "No se pudo actualizar la cuenta."); } finally { setLoading(false); } }
  async function confirmDelete() { const type = modal; setLoading(true); setSuccess(""); setQueryError(""); try { if (type === "delete-bank") await apiRequest(`/api/v1/bancos/${editingBankId}/?empresa_id=${encodeURIComponent(companyId)}&pais_codigo=${countryCode}`, { method: "DELETE" }); if (type === "delete-account") await apiRequest(`/api/v1/cuentas/${editingAccountId}/?empresa_id=${encodeURIComponent(companyId)}&pais_codigo=${countryCode}`, { method: "DELETE" }); closeModalAndRestore(); setSuccess("Registro eliminado correctamente."); await load(); } catch (error) { setQueryError(error instanceof Error ? error.message : "No se pudo eliminar el registro."); } finally { setLoading(false); } }

  return <div className="bank-module">
    <div className="module-tabs" role="tablist"><button role="tab" aria-selected={activeTab === "accounts"} className={activeTab === "accounts" ? "active" : ""} onClick={() => { setActiveTab("accounts"); setPage(1); }}>Cuentas bancarias</button><button role="tab" aria-selected={activeTab === "banks"} className={activeTab === "banks" ? "active" : ""} onClick={() => { setActiveTab("banks"); setPage(1); }}>Bancos</button></div>
    <div className="table-toolbar"><div className="table-toolbar-search"><label><span>Búsqueda</span><input aria-label="Buscar" placeholder={activeTab === "accounts" ? "Cuenta, banco o alias" : "Nombre del banco"} value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); }} /></label><label><span>Estado</span><select aria-label="Estado" value={activeFilter} onChange={(event) => { setActiveFilter(event.target.value as "all" | "true" | "false"); setPage(1); }}><option value="all">Todos</option><option value="true">Activos</option><option value="false">Inactivos</option></select></label></div>{isAdmin && <button className="primary-button toolbar-button" type="button" onClick={(event) => activeTab === "accounts" ? openAccountCreate(event.currentTarget) : openBankCreate(event.currentTarget)}>{activeTab === "accounts" ? "Nueva cuenta" : "Nuevo banco"}</button>}</div>
    {success && <div className="notice success" role="status">{success}</div>}{loading && <div className="notice warning" role="status">Cargando información…</div>}{queryError && <div className="notice error" role="alert"><strong>No se pudo completar la operación.</strong><span>{queryError}</span>{!modal && <button className="retry-button" type="button" onClick={() => void load()}>Reintentar</button>}</div>}
    {!loading && !queryError && activeTab === "accounts" && (accounts.length === 0 ? <div className="notice empty">No hay cuentas que coincidan con los filtros.</div> : <div className="data-table"><table><thead><tr><th>Banco</th><th>Alias</th><th>Número</th><th>Moneda</th><th>Estado</th>{isAdmin && <th>Acciones</th>}</tr></thead><tbody>{accounts.map((account) => <tr key={account.id}><td>{account.banco}{!account.banco_activo && <small className="table-note">Banco inactivo</small>}</td><td>{account.alias}</td><td>{account.numero}</td><td>{account.moneda_codigo}</td><td><span className={account.activa ? "status-ready" : "status-muted"}>{account.activa ? "Activa" : "Inactiva"}</span></td>{isAdmin && <td><div className="row-actions"><button className="row-action" type="button" onClick={(event) => openAccountEdit(account, event.currentTarget)}>Editar</button><button className="row-action" type="button" disabled={!account.activa && !account.banco_activo} onClick={() => void patchAccount(account, { activa: !account.activa })}>{account.activa ? "Desactivar" : "Activar"}</button><button className="row-action danger" type="button" onClick={(event) => { setEditingAccountId(account.id); openDelete("delete-account", `Cuenta ${account.numero} · ${account.alias}`, event.currentTarget); }}>Eliminar</button></div></td>}</tr>)}</tbody></table></div>)}
    {!loading && !queryError && activeTab === "banks" && (banks.length === 0 ? <div className="notice empty">No hay bancos que coincidan con los filtros.</div> : <div className="data-table"><table><thead><tr><th>Nombre</th><th>Estado</th>{isAdmin && <th>Acciones</th>}</tr></thead><tbody>{banks.map((bank) => <tr key={bank.id}><td>{bank.nombre}</td><td><span className={bank.activo ? "status-ready" : "status-muted"}>{bank.activo ? "Activo" : "Inactivo"}</span></td>{isAdmin && <td><div className="row-actions"><button className="row-action" type="button" onClick={(event) => openBankEdit(bank, event.currentTarget)}>Editar</button><button className="row-action" type="button" onClick={() => void patchBank(bank, { activo: !bank.activo })}>{bank.activo ? "Desactivar" : "Activar"}</button><button className="row-action danger" type="button" onClick={(event) => { setEditingBankId(bank.id); openDelete("delete-bank", bank.nombre, event.currentTarget); }}>Eliminar</button></div></td>}</tr>)}</tbody></table></div>)}
    {!loading && !queryError && <div className="pagination"><button className="quiet-button" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>Anterior</button><span>Página {page} de {totalPages}</span><button className="quiet-button" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>Siguiente</button></div>}

    {(modal === "bank-create" || modal === "bank-edit") && <Modal title={modal === "bank-create" ? "Nuevo banco" : "Editar banco"} size="bank" opener={openerRef.current} onClose={closeModalAndRestore}><form className="modal-form" onSubmit={saveBank}><label htmlFor="bank-name">Nombre del banco<input id="bank-name" required value={bankName} onChange={(event) => setBankName(event.target.value)} /></label>{modal === "bank-edit" && <label>Estado<select value={bankActive ? "true" : "false"} onChange={(event) => setBankActive(event.target.value === "true")}><option value="true">Activo</option><option value="false">Inactivo</option></select></label>}<div className="modal-actions"><button className="quiet-button" type="button" onClick={closeModalAndRestore}>Cancelar</button><button className="primary-button compact" disabled={loading} type="submit">Guardar</button></div></form></Modal>}
    {(modal === "account-create" || modal === "account-edit") && <Modal title={modal === "account-create" ? "Nueva cuenta bancaria" : "Editar cuenta"} size="account" opener={openerRef.current} onClose={closeModalAndRestore}><form className="modal-form account-form-grid" onSubmit={saveAccount}>{modal === "account-create" ? <><label htmlFor="account-bank">Banco<select id="account-bank" required value={accountBankId} onChange={(event) => setAccountBankId(event.target.value)}><option value="">Selecciona banco activo</option>{banks.filter((bank) => bank.activo).map((bank) => <option key={bank.id} value={bank.id}>{bank.nombre}</option>)}</select></label><label htmlFor="account-currency">Moneda<select id="account-currency" required value={accountCurrency} onChange={(event) => setAccountCurrency(event.target.value)}><option value="">Selecciona moneda</option>{currencies.map((currency) => <option key={currency.codigo} value={currency.codigo}>{currency.codigo} · {currency.nombre}</option>)}</select></label><label htmlFor="account-number">Número de cuenta<input id="account-number" required inputMode="text" value={accountNumber} onChange={(event) => setAccountNumber(event.target.value)} /></label></> : <><div className="read-only-field"><span>Banco</span><strong>{accounts.find((item) => item.id === editingAccountId)?.banco}</strong></div><div className="read-only-field"><span>Número</span><strong>{accountNumber}</strong></div><div className="read-only-field"><span>Moneda</span><strong>{accountCurrency}</strong></div></>}<label htmlFor="account-alias">Alias<input id="account-alias" required value={accountAlias} onChange={(event) => setAccountAlias(event.target.value)} /></label>{modal === "account-edit" && <label htmlFor="account-active">Estado<select id="account-active" value={accountActive ? "true" : "false"} onChange={(event) => setAccountActive(event.target.value === "true")}><option value="true">Activa</option><option value="false">Inactiva</option></select></label>}<div className="modal-actions"><button className="quiet-button" type="button" onClick={closeModalAndRestore}>Cancelar</button><button className="primary-button compact" disabled={loading} type="submit">Guardar</button></div></form></Modal>}
    {(modal === "delete-bank" || modal === "delete-account") && <Modal title="Confirmar eliminación" size="confirm" opener={openerRef.current} onClose={closeModalAndRestore}><p>¿Eliminar <strong>{deleteLabel}</strong>?</p><p className="delete-warning">Solo se permitirá si el registro no tiene información asociada.</p><div className="modal-actions"><button className="quiet-button" type="button" onClick={closeModalAndRestore}>Cancelar</button><button className="danger-button" disabled={loading} type="button" onClick={() => void confirmDelete()}>Eliminar</button></div></Modal>}
  </div>;
}
