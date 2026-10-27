import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import type { Session } from "@supabase/supabase-js";
import "./ReconciliationModule.css";

type Role = "administrador" | "auxiliar" | "revisor" | "consulta";
type Side = "banco" | "libros";
type AnyRecord = Record<string, unknown>;
type Account = { id: string; banco: string; alias: string; numero: string; moneda_codigo: string; activa?: boolean };
type Reconciliation = AnyRecord & { id: string; cuenta_id?: string; periodo: string; fecha_corte: string; estado?: string };
type Movement = AnyRecord & { id: string; fecha: string; descripcion: string; monto: string; referencia?: string };
type ImportProfile = { id: string; nombre: string; version: number; configuracion: AnyRecord };
type Filters = { fecha_desde: string; fecha_hasta: string; referencia: string; descripcion: string };

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "");
const pageSize = 25;
const balanceFields = [
  ["saldo_inicial_banco", "Saldo inicial banco"],
  ["saldo_inicial_libros", "Saldo inicial libros"],
  ["saldo_final_banco", "Saldo final banco"],
  ["saldo_final_libros", "Saldo final libros"],
] as const;
const mappingFields = [
  ["fecha", "Fecha", true],
  ["referencia", "Referencia", false],
  ["descripcion", "Descripción", false],
  ["saldo", "Saldo de origen", false],
  ["fecha_valor", "Fecha valor", false],
  ["numero_transaccion", "Número de transacción", false],
  ["numero_cheque", "Número de cheque", false],
  ["codigo_tt", "Código TT", false],
] as const;
const signedAmountField = ["monto", "Monto con signo (+ entrada / − salida)", false] as const;
const directionAmountFields = [
  ["salida", "Salidas / cargos (monto positivo)", false],
  ["entrada", "Entradas / abonos (monto positivo)", false],
] as const;
const mappingHeaderHints: Record<string, string[]> = {
  fecha: ["fecha", "date"],
  referencia: ["referencia", "reference", "ref"],
  descripcion: ["descripcion", "description", "concepto", "detalle"],
  monto: ["monto", "amount", "importe", "valor"],
  salida: ["salida", "salidas", "egreso", "egresos", "cargo", "cargos", "debito", "debit", "outgoing"],
  entrada: ["entrada", "entradas", "ingreso", "ingresos", "abono", "abonos", "credito", "credit", "incoming"],
  saldo: ["saldo", "balance"],
  fecha_valor: ["fecha valor", "fecha_valor", "value date"],
  numero_transaccion: ["numero transaccion", "transaction", "transaccion"],
  numero_cheque: ["numero cheque", "cheque", "check"],
  codigo_tt: ["codigo tt", "codigo_tt"],
};

function record(value: unknown): AnyRecord {
  return typeof value === "object" && value !== null ? value as AnyRecord : {};
}
function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}
function listValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
function resultsFrom(value: unknown): AnyRecord[] {
  const data = record(value);
  return listValue(data.results ?? value).map(record);
}
function payloadError(value: unknown, fallback: string): string {
  const data = record(value);
  return stringValue(data.detail ?? data.message, fallback);
}
function apiUrl(path: string) {
  if (!apiBaseUrl) throw new Error("Falta configurar VITE_API_BASE_URL en frontend/.env.");
  return `${apiBaseUrl}${path}`;
}
function optionName(value: unknown): string {
  if (typeof value === "string") return value;
  const data = record(value);
  return stringValue(data.nombre ?? data.name ?? data.sheet ?? data.hoja);
}
function suggestedMapping(columns: AnyRecord[]): Record<string, string> {
  const normalize = (value: string) => value.toLocaleLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  return Object.fromEntries([...mappingFields, signedAmountField, ...directionAmountFields].map(([key]) => {
    const hints = mappingHeaderHints[key] ?? [key];
    const column = columns.find((candidate) => {
      const normalized = normalize(stringValue(candidate.encabezado));
      return hints.some((hint) => normalized.includes(normalize(hint)));
    });
    return [key, column ? stringValue(column.columna) : ""];
  }));
}
function displayAmount(value: unknown, currency: string): string {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value).trim();
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(text);
  if (!match) return "—";
  const negative = match[1] === "-";
  const integer = BigInt(match[2]).toLocaleString("es");
  const fraction = (match[3] ?? "").replace(/0+$/, "");
  const decimalSeparator = new Intl.NumberFormat("es").formatToParts(1.1).find((part) => part.type === "decimal")?.value ?? ",";
  const amount = `${negative ? "−" : ""}${integer}${fraction ? `${decimalSeparator}${fraction}` : ""}`;
  return currency && currency !== "—" ? `${amount} ${currency}` : amount;
}
function displayDate(value: unknown): string {
  if (typeof value !== "string" || !value) return "—";
  const date = new Date(`${value.slice(0, 10)}T00:00:00`);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString("es");
}
function monthOf(value: string): string {
  return value ? `${value.slice(0, 7)}-01` : "";
}
function lastDayOfMonth(value: string): string | undefined {
  if (!value) return undefined;
  const [year, month] = value.split("-").map(Number);
  const day = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return `${value}-${String(day).padStart(2, "0")}`;
}
function getApiItems(value: unknown): AnyRecord[] {
  return resultsFrom(value).map((item) => ({
    ...item,
    id: stringValue(item.id ?? item.cuenta_id),
  }));
}

function Dialog({ title, onClose, children, wide = false }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const timer = window.setTimeout(() => {
      const field = panelRef.current?.querySelector<HTMLElement>("input:not([disabled]), select:not([disabled])");
      (field ?? panelRef.current?.querySelector<HTMLElement>("button"))?.focus();
    }, 0);
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key === "Tab") {
        const focusable = panelRef.current?.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])");
        if (!focusable?.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", handleKey);
      window.setTimeout(() => openerRef.current?.focus(), 0);
    };
  }, []);

  return <div className="recon-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div className={`recon-dialog ${wide ? "recon-dialog-wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby="recon-dialog-title" ref={panelRef}>
      <header className="recon-dialog-header"><h2 id="recon-dialog-title">{title}</h2><button className="recon-icon-button" type="button" aria-label="Cerrar" onClick={onClose}>×</button></header>
      {children}
    </div>
  </div>;
}

export default function ReconciliationModule({
  session, companyId, companyName, countryCode, countryName, role,
}: {
  session: Session;
  companyId: string;
  companyName: string;
  countryCode: "SV" | "GT";
  countryName: string;
  role: Role;
}) {
  const canEdit = role === "administrador" || role === "auxiliar";
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [items, setItems] = useState<Reconciliation[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [listReload, setListReload] = useState(0);
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<Reconciliation | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [detailReload, setDetailReload] = useState(0);
  const [createOpen, setCreateOpen] = useState(false);
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState("");
  const [existingReconciliation, setExistingReconciliation] = useState<Reconciliation | null>(null);
  const [createAccount, setCreateAccount] = useState("");
  const [createPeriod, setCreatePeriod] = useState("");
  const [createCutoff, setCreateCutoff] = useState("");
  const [createBalances, setCreateBalances] = useState<Record<string, string>>({});
  const [editingBalances, setEditingBalances] = useState(false);
  const [balanceDraft, setBalanceDraft] = useState<Record<string, string>>({});
  const [saveBusy, setSaveBusy] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [success, setSuccess] = useState("");
  const [activeSide, setActiveSide] = useState<Side>("banco");
  const [movementPage, setMovementPage] = useState<Record<Side, number>>({ banco: 1, libros: 1 });
  const [movementFilters, setMovementFilters] = useState<Record<Side, Filters>>({
    banco: { fecha_desde: "", fecha_hasta: "", referencia: "", descripcion: "" },
    libros: { fecha_desde: "", fecha_hasta: "", referencia: "", descripcion: "" },
  });
  const [movements, setMovements] = useState<Movement[]>([]);
  const [movementCount, setMovementCount] = useState(0);
  const [movementLoading, setMovementLoading] = useState(false);
  const [movementError, setMovementError] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [wizardBusy, setWizardBusy] = useState(false);
  const [wizardError, setWizardError] = useState("");
  const [preview, setPreview] = useState<AnyRecord | null>(null);
  const [previewId, setPreviewId] = useState("");
  const [inspectedColumns, setInspectedColumns] = useState<AnyRecord[]>([]);
  const [previewRows, setPreviewRows] = useState<unknown[][]>([]);
  const [profiles, setProfiles] = useState<ImportProfile[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [profileError, setProfileError] = useState("");
  const [origin, setOrigin] = useState<"banco" | "libros">("banco");
  const [amountMappingMode, setAmountMappingMode] = useState<"signed" | "separate">("signed");
  const [sheet, setSheet] = useState("");
  const [headerRow, setHeaderRow] = useState("1");
  const [dateFormat, setDateFormat] = useState("%d/%m/%Y");
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [validation, setValidation] = useState<AnyRecord | null>(null);
  const [excludedRows, setExcludedRows] = useState<number[]>([]);
  const [saveProfile, setSaveProfile] = useState(false);
  const [profileName, setProfileName] = useState("");
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState("");
  const [sourceMovement, setSourceMovement] = useState<Movement | null>(null);
  const uploadInput = useRef<HTMLInputElement>(null);
  const activeRequests = useRef(new Set<AbortController>());

  async function request(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<unknown> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${session.access_token}`);
    if (!(init.body instanceof FormData) && init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const controller = signal ? null : new AbortController();
    if (controller) activeRequests.current.add(controller);
    try {
      const response = await fetch(apiUrl(path), { ...init, headers, signal: signal ?? controller?.signal });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payloadError(payload, `El backend respondió con HTTP ${response.status}.`));
      return payload;
    } finally {
      if (controller) activeRequests.current.delete(controller);
    }
  }
  useEffect(() => () => { activeRequests.current.forEach((controller) => controller.abort()); }, []);
  useEffect(() => {
    if (!uploadOpen || !detail) return;
    const controller = new AbortController();
    setProfiles([]);
    setSelectedProfileId("");
    setProfileError("");
    void request(`/api/v1/conciliaciones/${encodeURIComponent(detail.id)}/perfiles/?origen=${origin}`, {}, controller.signal)
      .then((payload) => {
        if (!controller.signal.aborted) setProfiles(resultsFrom(payload) as ImportProfile[]);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setProfileError(error instanceof Error ? error.message : "No se pudieron consultar los perfiles de importación.");
      });
    return () => controller.abort();
  }, [uploadOpen, origin, detail?.id, session.access_token]);
  function scopeQuery() {
    return `empresa_id=${encodeURIComponent(companyId)}&pais_codigo=${countryCode}`;
  }
  const accountById = useMemo(() => new Map(accounts.map((account) => [account.id, account])), [accounts]);
  const selectedAccount = detail
    ? accountById.get(stringValue(detail.cuenta_id ?? record(detail.cuenta).id)) ?? null
    : null;
  const filteredItems = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return items;
    return items.filter((item) => {
      const account = accountById.get(stringValue(item.cuenta_id ?? record(item.cuenta).id));
      return [account?.banco, account?.alias, account?.numero, item.periodo, item.estado]
        .some((value) => stringValue(value).toLocaleLowerCase().includes(needle));
    });
  }, [items, query, accountById]);

  useEffect(() => {
    const controller = new AbortController();
    setListLoading(true);
    setListError("");
    setItems([]);
    setAccounts([]);
    setSelectedId("");
    setDetail(null);
    void Promise.all([
      request(`/api/v1/conciliaciones/?${scopeQuery()}&page_size=100`, {}, controller.signal),
      request(`/api/v1/conciliaciones/cuentas/?${scopeQuery()}&page_size=100`, {}, controller.signal),
    ]).then(([reconciliations, accountPayload]) => {
      if (controller.signal.aborted) return;
      const rows = getApiItems(reconciliations) as Reconciliation[];
      const accountRows = getApiItems(accountPayload).map((item) => ({
        id: stringValue(item.id),
        banco: stringValue(item.banco ?? item.banco_nombre),
        alias: stringValue(item.alias),
        numero: stringValue(item.numero),
        moneda_codigo: stringValue(item.moneda_codigo ?? item.moneda),
        activa: item.activa === undefined ? undefined : Boolean(item.activa),
      }));
      setItems(rows);
      setAccounts(accountRows);
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setListError(error instanceof Error ? error.message : "No se pudieron consultar las conciliaciones.");
    }).finally(() => {
      if (!controller.signal.aborted) setListLoading(false);
    });
    return () => controller.abort();
  }, [companyId, countryCode, session.access_token, listReload]);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    setDetail(null);
    setDetailLoading(true);
    setDetailError("");
    setMovementError("");
    setMovements([]);
    void request(`/api/v1/conciliaciones/${encodeURIComponent(selectedId)}/`, {}, controller.signal)
      .then((payload) => {
        if (!controller.signal.aborted) setDetail(record(payload) as Reconciliation);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setDetailError(error instanceof Error ? error.message : "No se pudo cargar el detalle.");
      })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selectedId, companyId, countryCode, session.access_token, detailReload]);

  useEffect(() => {
    if (!selectedId || !detail) return;
    const controller = new AbortController();
    const filters = movementFilters[activeSide];
    const params = new URLSearchParams({
      lado: activeSide,
      page: String(movementPage[activeSide]),
      page_size: String(pageSize),
    });
    Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
    setMovementLoading(true);
    setMovementError("");
    setMovements([]);
    void request(`/api/v1/conciliaciones/${encodeURIComponent(selectedId)}/movimientos/?${params}`, {}, controller.signal)
      .then((payload) => {
        if (controller.signal.aborted) return;
        const data = record(payload);
        setMovements(resultsFrom(payload) as Movement[]);
        setMovementCount(Number(data.count ?? 0));
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setMovementError(error instanceof Error ? error.message : "No se pudieron cargar los movimientos.");
      })
      .finally(() => { if (!controller.signal.aborted) setMovementLoading(false); });
    return () => controller.abort();
  }, [selectedId, detail, activeSide, movementPage, movementFilters, companyId, countryCode, session.access_token]);

  function resetWizard() {
    setWizardError("");
    setPreview(null);
    setPreviewId("");
    setValidation(null);
    setExcludedRows([]);
    setMapping({});
    setProfiles([]);
    setSelectedProfileId("");
    setInspectedColumns([]);
    setPreviewRows([]);
    setAmountMappingMode("signed");
    setSheet("");
    setHeaderRow("1");
    setSaveProfile(false);
    setProfileName("");
    setSelectedProfileId("");
  }
  function startCreate() {
    setCreateError("");
    setExistingReconciliation(null);
    setCreateAccount(accounts.find((account) => account.activa !== false)?.id ?? "");
    setCreatePeriod("");
    setCreateCutoff("");
    setCreateBalances({});
    setCreateOpen(true);
  }
  async function createReconciliation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!createPeriod || !createCutoff || !createAccount) return;
    setCreateBusy(true);
    setCreateError("");
    const body: Record<string, unknown> = {
      cuenta_id: createAccount,
      periodo: monthOf(createPeriod),
      fecha_corte: createCutoff,
    };
    balanceFields.forEach(([key]) => { body[key] = createBalances[key] === "" ? null : createBalances[key] ?? null; });
    try {
      const created = record(await request("/api/v1/conciliaciones/", {
        method: "POST",
        body: JSON.stringify(body),
      }));
      const id = stringValue(created.id ?? record(created.conciliacion).id);
      setCreateOpen(false);
      setSuccess("Borrador de conciliación creado.");
      const account = accountById.get(createAccount);
      setItems((current) => [...current, {
        ...created,
        id,
        cuenta_id: createAccount,
        cuenta_alias: account?.alias,
        periodo: body.periodo as string,
        fecha_corte: createCutoff,
        estado: "borrador",
      } as Reconciliation]);
      if (id) setSelectedId(id);
      else {
        const response = await request(`/api/v1/conciliaciones/?${scopeQuery()}&page_size=100`);
        setItems(getApiItems(response) as Reconciliation[]);
      }
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : "No se pudo crear el borrador.");
      if (error instanceof Error && error.message.toLocaleLowerCase().includes("ya existe")) {
        try {
          const response = await request(`/api/v1/conciliaciones/?${scopeQuery()}&cuenta_id=${encodeURIComponent(createAccount)}&page_size=100`);
          const match = getApiItems(response).find((item) => stringValue(item.periodo).slice(0, 7) === createPeriod);
          if (match) setExistingReconciliation(match as Reconciliation);
        } catch (lookupError) {
          setCreateError(`${error.message} No se pudo consultar la conciliación existente: ${lookupError instanceof Error ? lookupError.message : "Error de conexión."}`);
        }
      }
    } finally {
      setCreateBusy(false);
    }
  }
  function beginEditBalances() {
    if (!detail) return;
    setBalanceDraft(Object.fromEntries(balanceFields.map(([key]) => [key, stringValue(detail[key])])));
    setSaveError("");
    setEditingBalances(true);
  }
  async function saveBalances(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail) return;
    setSaveBusy(true);
    setSaveError("");
    const body = Object.fromEntries(balanceFields.map(([key]) => [key, balanceDraft[key] === "" ? null : balanceDraft[key]]));
    try {
      const updated = await request(`/api/v1/conciliaciones/${encodeURIComponent(detail.id)}/`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      setDetail(record(updated) as Reconciliation);
      setItems((current) => current.map((item) => item.id === detail.id ? { ...item, ...record(updated) } as Reconciliation : item));
      setEditingBalances(false);
      setSuccess("Saldos del borrador actualizados.");
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "No se pudieron actualizar los saldos.");
    } finally {
      setSaveBusy(false);
    }
  }

  async function uploadWorkbook(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const file = uploadInput.current?.files?.[0];
    if (!file || !detail) return;
    if (!/\.xlsx$/i.test(file.name)) {
      setWizardError("Selecciona un archivo .xlsx. No se admiten .xls ni otros formatos.");
      return;
    }
    setWizardBusy(true);
    setWizardError("");
    const body = new FormData();
    body.append("file", file);
    body.append("origen", origin);
    try {
      const result = record(await request(`/api/v1/conciliaciones/${encodeURIComponent(detail.id)}/previsualizaciones/`, { method: "POST", body }));
      const id = stringValue(result.preview_id ?? result.id);
      if (!id) throw new Error("La API no devolvió preview_id.");
      setPreview(result);
      setPreviewId(id);
      const sheets = listValue(result.sheets ?? result.hojas).map(optionName).filter(Boolean);
      const selectedSheet = stringValue(result.sheet ?? result.hoja) || sheets[0] || "";
      setSheet(selectedSheet);
      setHeaderRow("1");
      setMapping({});
      setInspectedColumns([]);
      setPreviewRows([]);
      setAmountMappingMode("signed");
      setValidation(null);
      setExcludedRows([]);
      if (selectedSheet) {
        const inspection = record(await request(
          `/api/v1/conciliaciones/${encodeURIComponent(detail.id)}/previsualizaciones/${encodeURIComponent(id)}/inspeccionar/`,
          { method: "POST", body: JSON.stringify({ sheet: selectedSheet, header_row: 1 }) },
        ));
        const columns = listValue(inspection.columnas).map(record);
        setInspectedColumns(columns);
        setPreviewRows(listValue(inspection.muestra_datos) as unknown[][]);
        setMapping(suggestedMapping(columns));
      }
    } catch (error) {
      setWizardError(error instanceof Error ? error.message : "No se pudo crear la vista previa.");
    } finally {
      setWizardBusy(false);
    }
  }
  async function inspectSelectedSheet() {
    if (!detail || !previewId || !sheet || !Number.isInteger(Number(headerRow))) return;
    setWizardBusy(true);
    setWizardError("");
    setValidation(null);
    try {
      const result = record(await request(
        `/api/v1/conciliaciones/${encodeURIComponent(detail.id)}/previsualizaciones/${encodeURIComponent(previewId)}/inspeccionar/`,
        { method: "POST", body: JSON.stringify({ sheet, header_row: Number(headerRow) }) },
      ));
      const columns = listValue(result.columnas).map(record);
      setInspectedColumns(columns);
      setPreviewRows(listValue(result.muestra_datos) as unknown[][]);
      setMapping(suggestedMapping(columns));
    } catch (error) {
      setInspectedColumns([]);
      setPreviewRows([]);
      setWizardError(error instanceof Error ? error.message : "No se pudieron leer las columnas de la hoja.");
    } finally {
      setWizardBusy(false);
    }
  }
  async function applySelectedProfile(profileId: string) {
    setSelectedProfileId(profileId);
    if (!profileId) return;
    const profile = profiles.find((item) => item.id === profileId);
    if (!profile || !previewId || !detail) return;
    const config = profile.configuracion;
    const profileSheet = stringValue(config.sheet);
    const profileHeaderRow = Number(config.header_row);
    if (!profileSheet || !Number.isInteger(profileHeaderRow) || profileHeaderRow < 1 || profileHeaderRow > 100) {
      setWizardError("El perfil guardado contiene una hoja o fila de encabezados no válida.");
      return;
    }
    const profileMapping = record(config.mapping);
    const nextMapping = Object.fromEntries(
      Object.entries(profileMapping).filter((entry): entry is [string, string] => typeof entry[1] === "string"),
    );
    setSheet(profileSheet);
    setHeaderRow(String(profileHeaderRow));
    setDateFormat(stringValue(config.date_format, "%d/%m/%Y"));
    setMapping(nextMapping);
    setAmountMappingMode(nextMapping.salida && nextMapping.entrada ? "separate" : "signed");
    setProfileName(profile.nombre);
    setValidation(null);
    setWizardBusy(true);
    setWizardError("");
    try {
      const result = record(await request(
        `/api/v1/conciliaciones/${encodeURIComponent(detail.id)}/previsualizaciones/${encodeURIComponent(previewId)}/inspeccionar/`,
        { method: "POST", body: JSON.stringify({ sheet: profileSheet, header_row: profileHeaderRow }) },
      ));
      setInspectedColumns(listValue(result.columnas).map(record));
      setPreviewRows(listValue(result.muestra_datos) as unknown[][]);
    } catch (error) {
      setInspectedColumns([]);
      setPreviewRows([]);
      setWizardError(error instanceof Error ? error.message : "No se pudo aplicar el perfil seleccionado.");
    } finally {
      setWizardBusy(false);
    }
  }
  async function validatePreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail || !previewId) return;
    const mappedColumns = mappingForRequest();
    if (origin === "libros" && !mappedColumns.monto) {
      setWizardError("Para importar libros, asigna una columna de monto con signo.");
      return;
    }
    if (origin === "banco" && amountMappingMode === "separate" && !(mappedColumns.salida && mappedColumns.entrada)) {
      setWizardError("Para el origen banco, asigna ambas columnas: salidas y entradas.");
      return;
    }
    if (origin === "banco" && amountMappingMode === "signed" && !mappedColumns.monto) {
      setWizardError("Asigna una columna de monto con signo o selecciona el mapeo separado de salidas y entradas.");
      return;
    }
    setWizardBusy(true);
    setWizardError("");
    const body = { sheet, header_row: Number(headerRow), mapping: mappedColumns, date_format: dateFormat };
    try {
      const result = record(await request(`/api/v1/conciliaciones/${encodeURIComponent(detail.id)}/previsualizaciones/${encodeURIComponent(previewId)}/validar/`, {
        method: "POST",
        body: JSON.stringify(body),
      }));
      setValidation(result);
      setExcludedRows([]);
    } catch (error) {
      setWizardError(error instanceof Error ? error.message : "No se pudo validar la vista previa.");
    } finally {
      setWizardBusy(false);
    }
  }
  function validationErrors(): unknown[] {
    return listValue(validation?.errors ?? validation?.errores ?? validation?.row_errors ?? validation?.errores_fila);
  }
  function validationWarnings(): unknown[] {
    return listValue(validation?.warnings ?? validation?.advertencias ?? validation?.row_warnings ?? validation?.avisos);
  }
  function outOfPeriodRows(): AnyRecord[] {
    if (!validation) return [];
    const rows = listValue(validation.exclusiones_requeridas ?? validation.exclusions ?? validation.exclusiones ?? validation.out_of_period_rows ?? validation.filas_fuera_periodo ?? validation.excluded_rows ?? validation.filas_excluidas);
    return rows.map((row) => typeof row === "number" ? { row } : record(row));
  }
  function rowNumber(item: AnyRecord): number {
    return Number(item.row ?? item.fila ?? item.row_number ?? item.excel_row ?? item.fila_excel);
  }
  function rowIssues(item: AnyRecord): string {
    const issues = listValue(item.errores);
    if (issues.length) {
      return issues.map((issue) => {
        const detail = record(issue);
        const field = stringValue(detail.campo);
        const message = stringValue(detail.mensaje ?? detail.message, "Revisa este valor.");
        return field ? `${field}: ${message}` : message;
      }).join("; ");
    }
    const field = stringValue(item.campo);
    const message = stringValue(item.mensaje ?? item.message ?? item.detail ?? item.error ?? item.descripcion, "");
    if (message) return field ? `${field}: ${message}` : message;
    const reason = stringValue(item.motivo);
    if (reason === "fuera_periodo") return "La fecha está fuera del período de conciliación.";
    if (reason === "posterior_fecha_corte") return `La fecha ${stringValue(item.fecha)} es posterior a la fecha de corte.`;
    return "Fila excluida por estar fuera del período.";
  }
  function rowDescription(item: AnyRecord): string {
    return rowIssues(item);
  }
  function signDirection(item: AnyRecord): string {
    const explicit = stringValue(item.direccion ?? item.direction ?? item.tipo_movimiento ?? item.naturaleza).toLocaleLowerCase();
    if (explicit.includes("entrada") || explicit.includes("ingreso") || explicit.includes("inflow")) return "Entrada (+)";
    if (explicit.includes("salida") || explicit.includes("egreso") || explicit.includes("outflow")) return "Salida (−)";
    const amount = Number(item.monto ?? item.monto_normalizado ?? item.amount);
    if (Number.isFinite(amount) && amount !== 0) return amount > 0 ? "Entrada (+)" : "Salida (−)";
    return "Sin monto normalizado";
  }
  function signAmount(item: AnyRecord): unknown {
    return item.monto ?? item.monto_normalizado ?? item.amount ?? item.importe;
  }
  function mappingForRequest(): Record<string, string> {
    const keys = amountMappingMode === "signed" || origin === "libros"
      ? [...mappingFields.map(([key]) => key), signedAmountField[0]]
      : [...mappingFields.map(([key]) => key), ...directionAmountFields.map(([key]) => key)];
    return Object.fromEntries(keys.filter((key) => mapping[key]?.trim()).map((key) => [key, mapping[key]]));
  }
  async function confirmImport() {
    if (!detail || !previewId || !validation) return;
    setConfirmBusy(true);
    setConfirmError("");
    const body: Record<string, unknown> = {
      mapping: mappingForRequest(), sheet, header_row: Number(headerRow), date_format: dateFormat, excluded_rows: excludedRows,
      save_profile: saveProfile,
    };
    if (selectedProfileId) body.profile_id = selectedProfileId;
    if (saveProfile) body.profile_name = profileName.trim();
    try {
      await request(`/api/v1/conciliaciones/${encodeURIComponent(detail.id)}/previsualizaciones/${encodeURIComponent(previewId)}/confirmar/`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      setUploadOpen(false);
      resetWizard();
      setSuccess("Importación confirmada. Se actualizarán los movimientos al volver a cargar la conciliación.");
      const refreshed = record(await request(`/api/v1/conciliaciones/${encodeURIComponent(detail.id)}/`));
      setDetail(refreshed as Reconciliation);
    } catch (error) {
      setConfirmError(error instanceof Error ? error.message : "No se pudo confirmar la importación.");
    } finally {
      setConfirmBusy(false);
    }
  }
  function setFilter(key: keyof Filters, value: string) {
    setMovementFilters((current) => ({ ...current, [activeSide]: { ...current[activeSide], [key]: value } }));
    setMovementPage((current) => ({ ...current, [activeSide]: 1 }));
  }
  function changeSide(side: Side) {
    setActiveSide(side);
    setMovementError("");
  }
  function accountFor(item: Reconciliation): Account | null {
    return accountById.get(stringValue(item.cuenta_id ?? record(item.cuenta).id)) ?? null;
  }
  function summaryValue(names: string[]): unknown {
    const summary = record(detail?.resumen ?? detail?.summary);
    for (const name of names) {
      if (summary[name] !== undefined && summary[name] !== null) return summary[name];
      if (detail?.[name] !== undefined && detail[name] !== null) return detail[name];
    }
    return undefined;
  }
  function summaryCard(label: string, value: unknown, currency: string, source?: unknown) {
    const isCount = label.toLowerCase().includes("movimiento") || label.toLowerCase().includes("carga");
    const count = Array.isArray(value) ? value.length : Number(value);
    const formattedValue = value === undefined || value === null || value === ""
      ? "—"
      : isCount
        ? Number.isFinite(count) ? count.toLocaleString("es") : "—"
        : displayAmount(value, currency);
    return <article className="recon-stat" key={label}>
      <span>{label}</span>
      <strong>{formattedValue}</strong>
      {source !== undefined && source !== null && <small>{sourceLabel(source)}</small>}
    </article>;
  }
  function sourceLabel(source: unknown): string {
    const normalized = stringValue(source).toLocaleLowerCase();
    if (normalized.includes("manual")) return "Captura manual";
    if (normalized.includes("excel") || normalized.includes("import")) return "Importado";
    if (normalized === "banco" || normalized === "libros" || normalized === "historico") return `Origen: ${normalized} · método no indicado`;
    return stringValue(source, "Origen no indicado");
  }
  const currency = selectedAccount?.moneda_codigo ?? stringValue(detail?.moneda_codigo ?? record(detail?.cuenta).moneda_codigo);
  const activeAccounts = accounts.filter((account) => account.activa !== false);
  const detailAccountName = selectedAccount
    ? `${selectedAccount.banco} · ${selectedAccount.alias} · ${selectedAccount.numero}`
    : stringValue(detail?.cuenta_alias ?? record(detail?.cuenta).alias ?? detail?.cuenta_id, "Cuenta");
  const summaryCounts = record(detail?.conteos ?? detail?.counts ?? detail?.resumen ?? detail?.summary);
  const movementPages = Math.max(1, Math.ceil(movementCount / pageSize));
  const sheets = preview ? listValue(preview.sheets ?? preview.hojas).map(optionName).filter(Boolean) : [];
  const headers = inspectedColumns;
  const validationSamples = listValue(validation?.movimientos ?? validation?.muestra_movimientos ?? validation?.sign_preview ?? validation?.vista_signos ?? validation?.normalized_rows ?? validation?.muestra);
  const signConvention = stringValue(validation?.convencion_signo);
  const hasValidationErrors = validationErrors().length > 0;
  const outRows = outOfPeriodRows();
  const allErrors = validationErrors();
  const warnings = validationWarnings();
  const unconfirmedExclusions = outRows.map(rowNumber).filter((number) => Number.isInteger(number) && number > 0 && !excludedRows.includes(number));
  function movementEmptyMessage(): string {
    const sideLoads = listValue(detail?.cargas).map(record).filter((load) => load.origen === activeSide);
    if (sideLoads.length === 0) return `Todavía no hay una carga de ${activeSide} para esta conciliación.`;
    if (sideLoads.some((load) => load.estado === "pendiente")) return `La importación de ${activeSide} está pendiente de validación o confirmación.`;
    if (sideLoads.some((load) => load.estado === "rechazada")) return `La última carga de ${activeSide} fue rechazada. Revisa el archivo y vuelve a importarlo.`;
    return `No hay movimientos de ${activeSide} para los filtros seleccionados.`;
  }

  return <section className="reconciliation-module" aria-label="Conciliaciones">
    {selectedId ? <>
      <div className="recon-back-row"><button type="button" className="recon-text-button" onClick={() => { setSelectedId(""); setDetail(null); setDetailError(""); }}>← Volver a conciliaciones</button>
        {canEdit && detail?.estado !== "en_revision" && <button className="primary-button recon-action" type="button" onClick={() => { resetWizard(); setUploadOpen(true); }}>Importar movimientos</button>}
      </div>
      {detailLoading && <div className="notice warning" role="status">Cargando detalle de conciliación…</div>}
      {detailError && <div className="notice error" role="alert"><strong>No se pudo cargar el detalle.</strong><span>{detailError}</span><button type="button" className="retry-button" onClick={() => setDetailReload((value) => value + 1)}>Reintentar</button></div>}
      {success && <div className="notice success" role="status">{success}</div>}
      {!detailLoading && detail && <>
        <header className="recon-detail-head">
          <div className="recon-detail-title"><p className="eyebrow">CONCILIACIÓN · {stringValue(detail.estado, "borrador")}</p><h2>{detailAccountName}</h2></div>
          <dl className="recon-context">
            <div><dt>Empresa</dt><dd>{companyName}</dd></div><div><dt>País</dt><dd>{countryName} ({countryCode})</dd></div>
            <div><dt>Cuenta</dt><dd>{detailAccountName}</dd></div><div><dt>Moneda</dt><dd>{currency}</dd></div>
            <div><dt>Período</dt><dd>{displayDate(detail.periodo)}</dd></div><div><dt>Fecha de corte</dt><dd>{displayDate(detail.fecha_corte)}</dd></div>
          </dl>
        </header>
        <section className="recon-summary">
          <div className="recon-section-title"><div><h3>Resumen</h3><p>Totales registrados para este borrador; los saldos son opcionales.</p></div>
            {canEdit && detail.estado !== "en_revision" && !editingBalances && <button type="button" className="secondary-button" onClick={beginEditBalances}>Editar saldos</button>}
          </div>
          <div className="recon-stats-grid">
            {summaryCard("Cargas", summaryValue(["cantidad_cargas", "total_cargas", "cargas"]), currency, "Importadas")}
            {summaryCard("Movimientos banco", summaryValue(["movimientos_banco", "cantidad_movimientos_banco", "conteo_banco"]) ?? summaryCounts.movimientos_banco, currency, summaryValue(["origen_banco"]))}
            {summaryCard("Movimientos libros", summaryValue(["movimientos_libros", "cantidad_movimientos_libros", "conteo_libros"]) ?? summaryCounts.movimientos_libros, currency, summaryValue(["origen_libros"]))}
            {summaryCard("Ingresos banco", summaryValue(["ingresos_banco", "total_ingresos_banco"]), currency, summaryValue(["origen_ingresos_banco"]))}
            {summaryCard("Salidas banco", summaryValue(["salidas_banco", "egresos_banco", "total_salidas_banco"]), currency, summaryValue(["origen_salidas_banco"]))}
            {summaryCard("Ingresos libros", summaryValue(["ingresos_libros", "total_ingresos_libros"]), currency, summaryValue(["origen_ingresos_libros"]))}
            {summaryCard("Salidas libros", summaryValue(["salidas_libros", "egresos_libros", "total_salidas_libros"]), currency, summaryValue(["origen_salidas_libros"]))}
            {balanceFields.map(([key, label]) => summaryCard(label, detail[key], currency, record(detail.balance_sources ?? detail.origen_saldos)[key]))}
          </div>
          {listValue(detail.cargas).length > 0 && <div className="recon-load-list"><h4>Cargas realizadas</h4><ul>{listValue(detail.cargas).map((value) => {
            const load = record(value);
            return <li key={stringValue(load.id)}><strong>{stringValue(load.nombre_archivo, "Carga manual")}</strong><span>{stringValue(load.origen)} · {stringValue(load.estado)} · {stringValue(load.movimientos, "0")} movimientos</span></li>;
          })}</ul></div>}
          {editingBalances && <form className="recon-balance-form" onSubmit={(event) => void saveBalances(event)}>
            <h4>Saldos del borrador</h4>
            <div className="recon-balance-grid">{balanceFields.map(([key, label]) => <label key={key}>{label}<input type="number" step="0.0001" value={balanceDraft[key] ?? ""} onChange={(event) => setBalanceDraft((current) => ({ ...current, [key]: event.target.value }))} /></label>)}</div>
            {saveError && <div className="notice error" role="alert">{saveError}</div>}
            <div className="recon-form-actions"><button type="button" className="secondary-button" onClick={() => setEditingBalances(false)}>Cancelar</button><button type="submit" className="primary-button" disabled={saveBusy}>{saveBusy ? "Guardando…" : "Guardar saldos"}</button></div>
          </form>}
        </section>
        <section className="recon-movements">
          <div className="recon-section-title"><div><h3>Movimientos</h3><p>Consulta separada por origen. No se proponen ni confirman cruces.</p></div></div>
          <div className="recon-side-tabs" role="tablist" aria-label="Lado de movimientos">
            {(["banco", "libros"] as Side[]).map((side) => <button key={side} role="tab" aria-selected={activeSide === side} className={activeSide === side ? "active" : ""} type="button" onClick={() => changeSide(side)}>{side === "banco" ? "Banco" : "Libros"}</button>)}
          </div>
          <div className="recon-filters">
            <label>Desde<input type="date" value={movementFilters[activeSide].fecha_desde} onChange={(event) => setFilter("fecha_desde", event.target.value)} /></label>
            <label>Hasta<input type="date" value={movementFilters[activeSide].fecha_hasta} onChange={(event) => setFilter("fecha_hasta", event.target.value)} /></label>
            <label>Referencia<input value={movementFilters[activeSide].referencia} onChange={(event) => setFilter("referencia", event.target.value)} placeholder="Buscar referencia" /></label>
            <label>Descripción<input value={movementFilters[activeSide].descripcion} onChange={(event) => setFilter("descripcion", event.target.value)} placeholder="Buscar descripción" /></label>
          </div>
          {movementLoading && <div className="notice warning" role="status">Cargando movimientos…</div>}
          {movementError && <div className="notice error" role="alert"><strong>Error al consultar movimientos.</strong><span>{movementError}</span></div>}
          {!movementLoading && !movementError && movements.length === 0 && <div className="notice empty">{movementEmptyMessage()}</div>}
          {!movementLoading && !movementError && movements.length > 0 && <div className="data-table recon-movement-table"><table><thead><tr><th>Fecha</th><th>Referencia</th><th>Descripción</th><th>Monto</th><th>Origen</th><th>Detalle de origen</th></tr></thead><tbody>
            {movements.map((movement) => {
              const load = record(movement.carga ?? movement.carga_info ?? movement.fuente);
              const method = movement.metodo ?? load.metodo ?? load.origen ?? movement.origen;
              const fileName = movement.nombre_archivo ?? movement.archivo ?? load.nombre_archivo;
              return <tr key={movement.id}><td>{displayDate(movement.fecha)}</td><td>{stringValue(movement.referencia ?? movement.numero_transaccion ?? movement.numero_cheque, "—")}</td><td>{stringValue(movement.descripcion, "—")}</td><td>{displayAmount(movement.monto, currency)}</td><td>{sourceLabel(method ?? (fileName ? "importado" : movement.origen))}</td><td><button type="button" className="row-action" onClick={() => setSourceMovement(movement)}>Ver origen</button></td></tr>;
            })}
          </tbody></table></div>}
          {!movementLoading && !movementError && movementCount > 0 && <div className="pagination"><span>{movementCount.toLocaleString("es")} movimientos · Página {movementPage[activeSide]} de {movementPages}</span><button type="button" className="secondary-button" disabled={movementPage[activeSide] <= 1} onClick={() => setMovementPage((current) => ({ ...current, [activeSide]: current[activeSide] - 1 }))}>Anterior</button><button type="button" className="secondary-button" disabled={movementPage[activeSide] >= movementPages} onClick={() => setMovementPage((current) => ({ ...current, [activeSide]: current[activeSide] + 1 }))}>Siguiente</button></div>}
        </section>
      </>}
    </> : <>
      <div className="recon-list-heading"><div><p className="eyebrow">CONTROL FINANCIERO</p><h2>Conciliaciones</h2><p>Consulta borradores de la empresa y país seleccionados.</p></div>{canEdit && <button type="button" className="primary-button recon-action" onClick={startCreate}>Nueva conciliación</button>}</div>
      {success && <div className="notice success" role="status">{success}</div>}
      <div className="recon-list-toolbar"><label>Buscar cuenta o período<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Banco, alias, número o AAAA-MM" /></label></div>
      {listLoading && <div className="notice warning" role="status">Cargando conciliaciones…</div>}
      {listError && <div className="notice error" role="alert"><strong>No se pudieron cargar las conciliaciones.</strong><span>{listError}</span><button type="button" className="retry-button" onClick={() => setListReload((value) => value + 1)}>Volver a consultar</button></div>}
      {!listLoading && !listError && filteredItems.length === 0 && <div className="notice empty">{items.length ? "No hay resultados para la búsqueda." : "Todavía no hay conciliaciones para esta empresa y país."}</div>}
      {!listLoading && !listError && filteredItems.length > 0 && <div className="data-table recon-list-table"><table><thead><tr><th>Cuenta</th><th>Moneda</th><th>Período</th><th>Fecha de corte</th><th>Estado</th><th></th></tr></thead><tbody>
        {filteredItems.map((item) => {
          const account = accountFor(item);
          return <tr key={item.id}><td><strong>{account?.alias ?? stringValue(item.cuenta_alias ?? record(item.cuenta).alias, "Cuenta")}</strong><small>{account ? `${account.banco} · ${account.numero}` : stringValue(item.cuenta_id)}</small></td><td>{account?.moneda_codigo ?? stringValue(item.moneda_codigo ?? record(item.cuenta).moneda_codigo, "—")}</td><td>{displayDate(item.periodo)}</td><td>{displayDate(item.fecha_corte)}</td><td><span className="status-muted">{stringValue(item.estado, "borrador")}</span></td><td><button className="row-action" type="button" onClick={() => { setSuccess(""); setSelectedId(item.id); }}>Abrir</button></td></tr>;
        })}
      </tbody></table></div>}
    </>}

    {createOpen && <Dialog title="Nueva conciliación" onClose={() => setCreateOpen(false)}>
      <form className="recon-form" onSubmit={(event) => void createReconciliation(event)}>
        <p className="recon-helper">Se creará como borrador. Ingresa únicamente los saldos que ya estén disponibles.</p>
        <label>Cuenta bancaria<select required value={createAccount} onChange={(event) => setCreateAccount(event.target.value)}><option value="">Selecciona una cuenta</option>{activeAccounts.map((account) => <option key={account.id} value={account.id}>{account.alias} · {account.banco} · {account.numero} ({account.moneda_codigo})</option>)}</select></label>
        {activeAccounts.length === 0 && <div className="notice warning">No hay cuentas activas disponibles en esta empresa y país.</div>}
        <div className="recon-form-grid"><label>Período<input required type="month" value={createPeriod} onChange={(event) => setCreatePeriod(event.target.value)} /></label><label>Fecha de corte<input required type="date" min={monthOf(createPeriod)} max={lastDayOfMonth(createPeriod)} value={createCutoff} onChange={(event) => setCreateCutoff(event.target.value)} /></label></div>
        <fieldset className="recon-fieldset"><legend>Saldos opcionales</legend><div className="recon-balance-grid">{balanceFields.map(([key, label]) => <label key={key}>{label}<input type="number" step="0.0001" value={createBalances[key] ?? ""} onChange={(event) => setCreateBalances((current) => ({ ...current, [key]: event.target.value }))} /></label>)}</div></fieldset>
        {createError && <div className="notice error" role="alert">{createError}</div>}
        {existingReconciliation && <button type="button" className="secondary-button recon-existing-link" onClick={() => { setCreateOpen(false); setSelectedId(existingReconciliation.id); setSuccess(""); }}>Abrir conciliación existente para {displayDate(existingReconciliation.periodo)}</button>}
        <div className="recon-form-actions"><button type="button" className="secondary-button" onClick={() => setCreateOpen(false)}>Cancelar</button><button type="submit" className="primary-button" disabled={createBusy || activeAccounts.length === 0}>{createBusy ? "Creando…" : "Crear borrador"}</button></div>
      </form>
    </Dialog>}

    {uploadOpen && <Dialog title="Importar movimientos desde Excel" wide onClose={() => { setUploadOpen(false); resetWizard(); }}>
      <div className="recon-wizard-steps" aria-label="Pasos de importación"><span className={!preview ? "current" : "complete"}>1. Archivo</span><span className={preview ? "current" : ""}>2. Hoja y mapeo</span><span className={validation ? "current" : ""}>3. Validar y confirmar</span></div>
      {!preview ? <form className="recon-form" onSubmit={(event) => void uploadWorkbook(event)}>
        <p className="recon-helper">El original se envía al backend para vista previa y validación. Solo se permite .xlsx.</p>
        <label>Origen<select value={origin} onChange={(event) => { const next = event.target.value as "banco" | "libros"; setOrigin(next); if (next === "libros") setAmountMappingMode("signed"); }}><option value="banco">Banco</option><option value="libros">Libros</option></select></label>
        <label>Archivo .xlsx<input ref={uploadInput} type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required /></label>
        {wizardError && <div className="notice error" role="alert">{wizardError}</div>}
        <div className="recon-form-actions"><button type="button" className="secondary-button" onClick={() => { setUploadOpen(false); resetWizard(); }}>Cancelar</button><button type="submit" className="primary-button" disabled={wizardBusy}>{wizardBusy ? "Preparando vista previa…" : "Continuar"}</button></div>
      </form> : <form className="recon-form" onSubmit={(event) => void validatePreview(event)}>
        <p className="recon-helper">Configura la hoja y las columnas. La validación y el resumen provienen del backend; nada se importa hasta confirmar.</p>
        {profiles.length > 0 && <label>Perfil reutilizable<select value={selectedProfileId} onChange={(event) => void applySelectedProfile(event.target.value)}><option value="">Sin perfil</option>{profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.nombre} · v{profile.version}</option>)}</select></label>}
        {profileError && <div className="notice error" role="alert">No se pudieron cargar los perfiles: {profileError}</div>}
        <div className="recon-form-grid"><label>Hoja<select required value={sheet} onChange={(event) => { setSelectedProfileId(""); setSheet(event.target.value); setValidation(null); setInspectedColumns([]); setPreviewRows([]); setMapping({}); }}>{sheets.length ? sheets.map((name) => <option key={name} value={name}>{name}</option>) : <option value={sheet}>{sheet || "Elige hoja"}</option>}</select></label><label>Fila de encabezados<input type="number" min="1" max="100" step="1" required value={headerRow} onChange={(event) => { setSelectedProfileId(""); setHeaderRow(event.target.value); setValidation(null); setInspectedColumns([]); setPreviewRows([]); setMapping({}); }} /></label></div>
        <div className="recon-form-actions recon-inspect-action"><button type="button" className="secondary-button" disabled={wizardBusy || !sheet || !headerRow} onClick={() => void inspectSelectedSheet()}>{wizardBusy ? "Leyendo hoja…" : "Leer columnas y muestra"}</button></div>
        <label>Formato de fecha<select value={dateFormat} onChange={(event) => { setSelectedProfileId(""); setDateFormat(event.target.value); setValidation(null); }}><option value="%d/%m/%Y">DD/MM/AAAA</option><option value="%m/%d/%Y">MM/DD/AAAA</option><option value="%Y-%m-%d">AAAA-MM-DD</option><option value="%d-%m-%Y">DD-MM-AAAA</option></select></label>
        <fieldset className="recon-fieldset"><legend>Mapeo de columnas</legend>
          <div className="recon-amount-mode"><strong>Columnas de monto</strong>
            <label><input type="radio" name="amount-mapping-mode" checked={amountMappingMode === "signed"} onChange={() => { setSelectedProfileId(""); setAmountMappingMode("signed"); setMapping((current) => ({ ...current, salida: "", entrada: "" })); setValidation(null); }} />Una columna con signo: monto positivo = entrada, negativo = salida.</label>
            {origin === "banco" && <label><input type="radio" name="amount-mapping-mode" checked={amountMappingMode === "separate"} onChange={() => { setSelectedProfileId(""); setAmountMappingMode("separate"); setMapping((current) => ({ ...current, monto: "" })); setValidation(null); }} />Dos columnas: una para salidas y otra para entradas. El importe de cada columna debe ser positivo.</label>}
          </div>
          <p className="recon-map-hint">Asigna columnas separadas para representar fielmente los encabezados del banco. No combines ambos modos. Libros requiere una única columna con signo.</p>
          <div className="recon-map-grid">{[...mappingFields, ...(amountMappingMode === "signed" || origin === "libros" ? [signedAmountField] : directionAmountFields)].map(([key, label, required]) => <label key={key}>{label}{required && <span className="required-mark"> · obligatorio</span>}<select required={required} value={mapping[key] ?? ""} onChange={(event) => { setSelectedProfileId(""); setMapping((current) => ({ ...current, [key]: event.target.value })); setValidation(null); setWizardError(""); }}><option value="">No asignar</option>{headers.map((column) => <option key={`${key}-${column.columna}`} value={stringValue(column.columna)}>{stringValue(column.columna)} · {stringValue(column.encabezado, "(sin encabezado)")}</option>)}</select></label>)}</div></fieldset>
        {previewRows.length > 0 && <div className="recon-preview-samples"><h4>Filas de muestra del archivo</h4><pre>{JSON.stringify(previewRows.slice(0, 4), null, 2)}</pre></div>}
        {wizardError && <div className="notice error" role="alert">{wizardError}</div>}
        <div className="recon-form-actions"><button type="button" className="secondary-button" onClick={() => { setPreview(null); setPreviewId(""); setValidation(null); }}>Cambiar archivo</button><button type="submit" className="primary-button" disabled={wizardBusy || !sheet || !mapping.fecha || (origin === "libros" && !mapping.monto) || (origin === "banco" && amountMappingMode === "signed" && !mapping.monto) || (origin === "banco" && amountMappingMode === "separate" && !(mapping.salida && mapping.entrada))}>{wizardBusy ? "Validando…" : "Validar archivo"}</button></div>
      </form>}
      {validation && <section className="recon-validation" aria-live="polite">
        <h3>Resultado de validación</h3>
        <div className="recon-validation-summary"><span>Filas analizadas: <strong>{stringValue(validation.filas_analizadas, "—")}</strong></span><span>Filas válidas: <strong>{stringValue(validation.movimientos_validos ?? validation.valid_rows ?? validation.filas_validas ?? validation.count_valid, String(validationSamples.length))}</strong></span><span>Filas con errores: <strong>{allErrors.length}</strong></span><span>Advertencias: <strong>{warnings.length}</strong></span><span>Fuera de período/corte: <strong>{outRows.length}</strong></span></div>
        {allErrors.length > 0 && <div className="recon-validation-errors"><h4>Errores que requieren corrección</h4><ul>{allErrors.slice(0, 50).map((error, index) => <li key={index}>{typeof error === "string" ? error : `Fila ${rowNumber(record(error)) || "—"} · ${rowDescription(record(error))}`}</li>)}</ul></div>}
        {warnings.length > 0 && <div className="recon-validation-warnings"><h4>Advertencias</h4><ul>{warnings.slice(0, 50).map((warning, index) => <li key={index}>{typeof warning === "string" ? warning : `Fila ${rowNumber(record(warning)) || "—"} · ${rowDescription(record(warning))}`}</li>)}</ul></div>}
        {outRows.length > 0 && <div className="recon-out-period"><h4>Filas fuera del período</h4><p>Selecciona explícitamente cada fila que se excluirá. Las filas no seleccionadas impiden confirmar la importación.</p>{outRows.map((row, index) => {
          const number = rowNumber(row);
          return <label key={`${number}-${index}`}><input type="checkbox" checked={excludedRows.includes(number)} onChange={(event) => setExcludedRows((current) => event.target.checked ? [...new Set([...current, number])] : current.filter((value) => value !== number))} />Excluir fila {number}: {rowDescription(row)}</label>;
        })}</div>}
        {validationSamples.length > 0 && <div className="recon-sign-preview"><h4>Vista previa de signos devuelta por el backend</h4><p className="recon-sign-legend">{signConvention && <span>Convención del backend: {signConvention} · </span>}<span className="recon-sign-in">Entrada (+)</span><span className="recon-sign-out">Salida (−)</span>· positivo representa entrada; negativo, salida.</p><div className="data-table recon-sign-table"><table><thead><tr><th>Fila</th><th>Dirección</th><th>Fecha</th><th>Referencia</th><th>Descripción</th><th>Monto normalizado</th></tr></thead><tbody>{validationSamples.slice(0, 10).map((sample, index) => {
          const item = record(sample);
          const direction = signDirection(item);
          const amount = signAmount(item);
          return <tr key={index}><td>{stringValue(item.row ?? item.fila ?? item.row_number ?? item.fila_origen, "—")}</td><td><span className={direction.startsWith("Entrada") ? "recon-sign-in" : direction.startsWith("Salida") ? "recon-sign-out" : ""}>{direction}</span></td><td>{displayDate(item.fecha ?? item.date)}</td><td>{stringValue(item.referencia ?? item.reference, "—")}</td><td>{stringValue(item.descripcion ?? item.description, "—")}</td><td>{amount === undefined ? "—" : displayAmount(amount, currency)}</td></tr>;
        })}</tbody></table></div></div>}
        {validationSamples.length === 0 && <p className="recon-helper">La API no devolvió filas normalizadas para mostrar la vista previa de signos.</p>}
        <div className="recon-profile-option"><label><input type="checkbox" checked={saveProfile} onChange={(event) => setSaveProfile(event.target.checked)} />Guardar esta configuración como perfil reutilizable</label>{saveProfile && <label>Nombre del perfil<input required value={profileName} onChange={(event) => setProfileName(event.target.value)} maxLength={100} /></label>}</div>
        {confirmError && <div className="notice error" role="alert">{confirmError}</div>}
        {hasValidationErrors && <div className="notice warning">Corrige el mapeo o el archivo y vuelve a validar antes de confirmar.</div>}
        {unconfirmedExclusions.length > 0 && <p className="recon-exclusion-required" role="status">Selecciona la exclusión de todas las filas fuera de período para continuar ({unconfirmedExclusions.length} pendiente{unconfirmedExclusions.length === 1 ? "" : "s"}).</p>}
        <div className="recon-form-actions"><button type="button" className="secondary-button" onClick={() => { setValidation(null); setConfirmError(""); }}>Volver al mapeo</button><button type="button" className="primary-button" disabled={confirmBusy || hasValidationErrors || unconfirmedExclusions.length > 0 || (saveProfile && !profileName.trim())} onClick={() => void confirmImport()}>{confirmBusy ? "Confirmando…" : "Confirmar importación"}</button></div>
      </section>}
    </Dialog>}

    {sourceMovement && <Dialog title="Detalle del origen del movimiento" wide onClose={() => setSourceMovement(null)}>
      <div className="recon-source-detail">
        <dl><div><dt>Fecha</dt><dd>{displayDate(sourceMovement.fecha)}</dd></div><div><dt>Monto normalizado</dt><dd>{displayAmount(sourceMovement.monto, currency)}</dd></div><div><dt>Referencia</dt><dd>{stringValue(sourceMovement.referencia, "—")}</dd></div><div><dt>Descripción</dt><dd>{stringValue(sourceMovement.descripcion, "—")}</dd></div><div><dt>Hoja</dt><dd>{stringValue(sourceMovement.hoja_origen, "MANUAL")}</dd></div><div><dt>Fila original</dt><dd>{stringValue(sourceMovement.fila_origen, "—")}</dd></div></dl>
        <h3>Datos originales de la fila</h3><pre>{JSON.stringify(record(sourceMovement.datos_originales), null, 2)}</pre>
        <h3>Archivo de origen</h3><pre>{JSON.stringify(record(sourceMovement.fuente), null, 2)}</pre>
      </div>
    </Dialog>}
  </section>;
}
