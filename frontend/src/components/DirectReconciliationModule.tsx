import { useEffect, useRef, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import "./DirectReconciliationModule.css";
import ReconciliationDashboard, { type SourceLocation, type SourceRow } from "./ReconciliationDashboard";

type Props = {
  session: Session;
  companyId: string;
  companyName: string;
  countryCode: "GT" | "SV";
  countryName: string;
  role: string;
  mode: "new" | "history";
  cuentaId?: string;
  initialAccountId?: string;
  bancoId?: string;
  onBackToAccount?: () => void;
  onBackToBank?: () => void;
  onSavedAccount?: (accountId: string) => void;
};

type DocumentFile = { name: string; size: number; lastModified: number };
type Stage = { name: string; status: "pendiente" | "en_curso" | "completo" | "fallido" };
type AccountOption = {
  id: string;
  banco_id: string;
  banco: string;
  alias: string;
  numero: string;
  numero_terminacion?: string | null;
  numero_completo?: string | null;
  moneda_codigo: string;
  activa: boolean;
  banco_activo: boolean;
  numero_completo_verificado?: boolean;
  incorporacion_provisional?: boolean;
  codigo_contable?: string | null;
};
type AccountLinkSuggestion = { numero_completo: string; terminacion: string; cuenta_id: string; requiere_confirmacion: boolean };
type DashboardReturnState = {
  jobId: string;
  accountId: string;
  result: Record<string, any>;
  account: AccountOption | null;
  stage: "idle" | "clarifications" | "complete";
  period: string;
};

function accountNumberLabel(account: AccountOption) {
  const full = account.numero_completo ?? (account.numero_completo_verificado ? account.numero : null);
  if (full) return `Número completo ${full}`;
  const ending = account.numero_terminacion ?? (/^\d{4}$/.test(account.numero) ? account.numero : null);
  return ending ? `Terminación ${ending}` : `Número ${account.numero}`;
}

function isCuscatlanAccount(account: AccountOption | null) {
  const identity = `${account?.banco ?? ""} ${account?.alias ?? ""}`
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
  return identity.includes("cuscat") || identity.includes("cusca 2509");
}

function structuredErrorMessage(payload: unknown): string | null {
  if (typeof payload === "string") return payload;
  if (Array.isArray(payload)) {
    const messages = payload.map(structuredErrorMessage).filter((value): value is string => Boolean(value));
    return messages.length ? messages.join(" ") : null;
  }
  if (typeof payload !== "object" || payload === null) return null;
  const record = payload as Record<string, unknown>;
  for (const key of ["detail", "message", "error", "cuenta_id", "files", "pais_codigo"]) {
    const message = structuredErrorMessage(record[key]);
    if (message) return message;
  }
  const messages = Object.values(record).map(structuredErrorMessage).filter((value): value is string => Boolean(value));
  return messages.length ? messages.join(" ") : null;
}

function formatStateLabel(value: unknown) {
  switch (String(value ?? "")) {
    case "finalizado_con_pendientes": return "Guardada con pendientes";
    case "finalizado":
    case "preparado": return "Guardada";
    case "aprobado": return "Aprobada";
    case "analizando":
    case "recibiendo": return "En progreso";
    case "requiere_aclaraciones": return "Requiere aclaraciones";
    case "fallido": return "Fallida";
    default: return "Estado no disponible";
  }
}

function formatJobDate(value: unknown) {
  if (!value) return "Fecha no disponible";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return "Fecha no disponible";
  return new Intl.DateTimeFormat("es-SV", {
    timeZone: "America/El_Salvador",
    day: "2-digit",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatJobPeriod(value: unknown) {
  if (!value) return "Período no disponible";
  const text = String(value);
  const match = text.match(/^(\d{4})-(\d{2})(?:-|$)/);
  if (!match) return text;
  const names = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"];
  const month = Number(match[2]);
  return month >= 1 && month <= 12 ? `${names[month - 1]} de ${match[1]}` : text;
}

export default function DirectReconciliationModule(props: Props) {
  const [documents, setDocuments] = useState<DocumentFile[]>([]);
  const [documentFiles, setDocumentFiles] = useState<File[]>([]);
  const [historyJobs, setHistoryJobs] = useState<Record<string, unknown>[]>([]);
  const [activeJobId, setActiveJobId] = useState("");
  const [activeJobAccountId, setActiveJobAccountId] = useState("");
  const [apiError, setApiError] = useState("");
  const [activeResult, setActiveResult] = useState<Record<string, any> | null>(null);
  const [dashboardReturnState, setDashboardReturnState] = useState<DashboardReturnState | null>(null);
  const [accounts, setAccounts] = useState<AccountOption[]>([]);
  const [selectedAccount, setSelectedAccount] = useState<AccountOption | null>(null);
  const [accountLinkSuggestion, setAccountLinkSuggestion] = useState<AccountLinkSuggestion | null>(null);
  const [accountSearch, setAccountSearch] = useState("");
  const [accountModalOpen, setAccountModalOpen] = useState(false);
  const [accountLoading, setAccountLoading] = useState(false);
  const [accountError, setAccountError] = useState("");
  const [accountReload, setAccountReload] = useState(0);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [selectedPeriod, setSelectedPeriod] = useState(() => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  });
  const [dashboardTab, setDashboardTab] = useState<"resumen" | "proceso" | "depositos" | "cheques">("resumen");
  const [stage, setStage] = useState<"idle" | "analyzing" | "clarifications" | "complete">("idle");
  const [stages, setStages] = useState<Stage[]>([
    { name: "Recibir archivos", status: "pendiente" },
    { name: "Identificar hojas y datos", status: "pendiente" },
    { name: "Cruzar movimientos", status: "pendiente" },
    { name: "Preparar informe", status: "pendiente" },
  ]);
  const fileInput = useRef<HTMLInputElement>(null);
  const replaceInput = useRef<HTMLInputElement>(null);
  const closureReplaceInput = useRef<HTMLInputElement>(null);
  const closureReplacementName = useRef<string | undefined>(undefined);
  const replacingIndex = useRef<number | null>(null);
  const storageKey = `contared-direct-${props.session.user.id}-${props.companyId}-${props.countryCode}`;
  const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "");
  const authHeaders = { Authorization: `Bearer ${props.session.access_token}` };

  useEffect(() => {
    if (!apiBaseUrl || props.mode !== "new") return;
    let active = true;
    setAccountLoading(true);
    setAccountError("");
    void (async () => {
      try {
        const allAccounts: AccountOption[] = [];
        let page = 1;
        let count = 0;
        do {
          const params = new URLSearchParams({
            empresa_id: props.companyId,
            pais_codigo: props.countryCode,
            active: "true",
            page: String(page),
            page_size: "100",
          });
          const response = await fetch(`${apiBaseUrl}/api/v1/cuentas/?${params}`, { headers: authHeaders });
          if (!response.ok) throw new Error(await responseError(response, `No se pudieron cargar las cuentas autorizadas (HTTP ${response.status}).`));
          const payload = await response.json() as { count?: number; results?: AccountOption[] };
          allAccounts.push(...(payload.results ?? []).filter((item) =>
            !item.id.startsWith("catalogo:") && item.activa && item.banco_activo && Boolean(item.moneda_codigo)));
          count = payload.count ?? allAccounts.length;
          page += 1;
        } while (allAccounts.length < count && page <= 100);
        if (!active) return;
        setAccounts(allAccounts);
        const requested = allAccounts.find((item) => item.id === props.initialAccountId);
        setSelectedAccount((current) => requested ?? current ?? null);
      } catch (error: unknown) {
        if (active) setAccountError(error instanceof Error ? error.message : "No se pudieron cargar las cuentas autorizadas.");
      } finally {
        if (active) setAccountLoading(false);
      }
    })();
    return () => { active = false; };
  }, [apiBaseUrl, props.mode, props.companyId, props.countryCode, props.session.access_token, props.initialAccountId, accountReload]);

  useEffect(() => {
    if (!apiBaseUrl || props.mode !== "history") return;
    const params = new URLSearchParams({ empresa_id: props.companyId, pais_codigo: props.countryCode, historial: "true" });
    if (props.cuentaId) params.set("cuenta_id", props.cuentaId);
    if (props.bancoId) params.set("banco_id", props.bancoId);
    void fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/?${params}`, { headers: authHeaders })
      .then(async (response) => {
        if (!response.ok) throw new Error(await responseError(response, `No se pudo cargar el historial (HTTP ${response.status}).`));
        const payload = await response.json() as { trabajos: Record<string, unknown>[] };
        setHistoryJobs(payload.trabajos ?? []);
      })
      .catch((error: unknown) => setApiError(error instanceof Error ? error.message : "No se pudo cargar el historial."));
  }, [apiBaseUrl, props.mode, props.companyId, props.countryCode, props.session.access_token, props.cuentaId, props.bancoId]);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(storageKey);
      if (saved) {
        const value = JSON.parse(saved) as { documents?: DocumentFile[]; stage?: typeof stage };
        if (Array.isArray(value.documents)) setDocuments(value.documents);
        if (value.stage) setStage(value.stage);
      }
    } catch {
      localStorage.removeItem(storageKey);
    }
  }, [storageKey]);

  useEffect(() => {
    if (!apiBaseUrl || props.mode !== "new") return;
    const params = new URLSearchParams({ empresa_id: props.companyId, pais_codigo: props.countryCode });
    if (props.initialAccountId) params.set("cuenta_id", props.initialAccountId);
    void fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/?${params}`, { headers: authHeaders })
      .then(async (response) => {
        if (!response.ok) throw new Error(await responseError(response, `No se pudo recuperar el último trabajo (HTTP ${response.status}).`));
        const payload = await response.json() as { trabajos: Record<string, unknown>[] };
        const latest = payload.trabajos?.[0];
        if (!latest) return;
        setActiveJobId(String(latest.id));
        setActiveJobAccountId(String(latest.cuenta_id ?? ""));
        void loadJob(String(latest.id), String(latest.cuenta_id ?? "")).catch((error: unknown) => setApiError(error instanceof Error ? error.message : "No se pudo recuperar el resultado."));
        const estado = String(latest.estado);
        setStage(estado === "preparado" ? "complete" : estado === "requiere_aclaraciones" ? "clarifications" : "idle");
      })
      .catch((error: unknown) => setApiError(error instanceof Error ? error.message : "No se pudo recuperar el último trabajo."));
  }, [apiBaseUrl, props.mode, props.companyId, props.countryCode, props.session.access_token, props.initialAccountId]);

  useEffect(() => {
    localStorage.setItem(storageKey, JSON.stringify({ documents, stage }));
  }, [documents, stage, storageKey]);

  async function responseError(response: Response, fallback: string) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const payload = await response.json().catch(() => null) as Record<string, unknown> | null;
      const detail = structuredErrorMessage(payload);
      if (detail) return detail;
    }
    return fallback;
  }

  async function jsonResponse(
    response: Response,
    fallback: string,
  ): Promise<Record<string, unknown>> {
    const contentType = response.headers.get("content-type") || "sin Content-Type";
    const body = await response.text();
    let payload: unknown;
    try {
      payload = JSON.parse(body);
    } catch {
      const endpoint = response.url ? new URL(response.url).pathname : "endpoint de análisis";
      const responseType = contentType.split(";", 1)[0];
      throw new Error(
        `El servidor respondió con contenido no JSON a POST ${endpoint} (HTTP ${response.status}, ${responseType}). La cuenta y los archivos siguen seleccionados. Verifica la URL del backend y revisa su registro para conocer el error.`,
      );
    }
    if (!response.ok) {
      throw new Error(structuredErrorMessage(payload) ?? fallback);
    }
    if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
      throw new Error(`El servidor devolvió una respuesta JSON no válida (HTTP ${response.status}). La cuenta y los archivos siguen seleccionados.`);
    }
    return payload as Record<string, unknown>;
  }

  async function loadJob(jobId: string, requestedAccountId?: string) {
    if (!apiBaseUrl) return;
    setApiError("");
    const params = new URLSearchParams({ empresa_id: props.companyId, pais_codigo: props.countryCode });
    if (requestedAccountId) params.set("cuenta_id", requestedAccountId);
    const response = await fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${jobId}/?${params}`, { headers: authHeaders });
    if (!response.ok) throw new Error(await responseError(response, `No se pudo recuperar el trabajo (HTTP ${response.status}).`));
    const job = await response.json() as { id: string; cuenta_id?: string; cuenta_seleccionada?: AccountOption; vinculacion_cuenta?: AccountLinkSuggestion | null; resultado?: Record<string, any>; estado: string };
    setActiveJobId(job.id);
    setActiveJobAccountId(job.cuenta_id ?? "");
    if (job.cuenta_seleccionada) setSelectedAccount(job.cuenta_seleccionada);
    setAccountLinkSuggestion(job.vinculacion_cuenta ?? null);
    setActiveResult(job.resultado ?? {});
    const savedPeriod = job.resultado?.contexto_resultado?.periodo;
    if (typeof savedPeriod === "string" && /^\d{4}-\d{2}$/.test(savedPeriod)) setSelectedPeriod(savedPeriod);
    setStage(job.estado === "preparado" ? "complete" : job.estado === "requiere_aclaraciones" ? "clarifications" : "idle");
  }

  async function openPreviousReconciliation(jobId: string, accountId: string) {
    if (activeResult && !dashboardReturnState) {
      setDashboardReturnState({
        jobId: activeJobId,
        accountId: activeJobAccountId,
        result: activeResult,
        account: selectedAccount,
        stage: stage === "complete" ? "complete" : stage === "clarifications" ? "clarifications" : "idle",
        period: selectedPeriod,
      });
    }
    try {
      await loadJob(jobId, accountId);
    } catch (error: unknown) {
      setDashboardReturnState(null);
      setApiError(error instanceof Error ? error.message : "No se pudo abrir la conciliación anterior.");
    }
  }

  function returnFromPreviousReconciliation() {
    if (dashboardReturnState) {
      setActiveJobId(dashboardReturnState.jobId);
      setActiveJobAccountId(dashboardReturnState.accountId);
      setActiveResult(dashboardReturnState.result);
      setSelectedAccount(dashboardReturnState.account);
      setSelectedPeriod(dashboardReturnState.period);
      setStage(dashboardReturnState.stage);
      setDashboardReturnState(null);
      return;
    }
    setActiveResult(null);
    setActiveJobId("");
    setActiveJobAccountId("");
  }

  function addFiles(files: FileList | null) {
    if (!files) return;
    const accepted = Array.from(files)
      .filter((file) => /\.(xlsx|xls)$/i.test(file.name))
      .map((file) => ({ name: file.name, size: file.size, lastModified: file.lastModified }));
    if (!accepted.length) return;
    const actualFiles = Array.from(files).filter((file) => /\.(xlsx|xls)$/i.test(file.name));
    setDocumentFiles((current) => {
      const byName = new Map(current.map((item) => [item.name, item]));
      actualFiles.forEach((item) => byName.set(item.name, item));
      return Array.from(byName.values());
    });
    setDocuments((current) => {
      const byName = new Map(current.map((item) => [item.name, item]));
      accepted.forEach((item) => byName.set(item.name, item));
      return Array.from(byName.values());
    });
    setStage("idle");
  }

  function replaceFile(index: number, files: FileList | null) {
    if (!files?.[0]) return;
    const file = files[0];
    if (!/\.(xlsx|xls)$/i.test(file.name)) return;
    setDocumentFiles((current) => current.map((item, itemIndex) => itemIndex === index ? file : item));
    setDocuments((current) => current.map((item, itemIndex) =>
      itemIndex === index ? { name: file.name, size: file.size, lastModified: file.lastModified } : item));
    setStage("idle");
  }

  function chooseAccount(account: AccountOption) {
    if (activeJobId && activeJobAccountId !== account.id) {
      if (!window.confirm("El resultado analizado no se reasignará. Para usar esta cuenta debes iniciar un análisis nuevo con los documentos actuales. ¿Continuar?")) return;
      setActiveJobId("");
      setActiveJobAccountId("");
      setActiveResult(null);
      setAccountLinkSuggestion(null);
      setStage("idle");
      setStages([
        { name: "Recibir archivos", status: "pendiente" },
        { name: "Identificar hojas y datos", status: "pendiente" },
        { name: "Cruzar movimientos", status: "pendiente" },
        { name: "Preparar informe", status: "pendiente" },
      ]);
    }
    setSelectedAccount(account);
    setAccountModalOpen(false);
    setAccountSearch("");
    setApiError("");
  }

  async function analyze() {
    if (!selectedAccount) {
      setAccountModalOpen(true);
      setApiError("Elige una cuenta autorizada antes de iniciar el análisis.");
      return;
    }
    if (!documentFiles.length) {
      setApiError("Selecciona los documentos originales antes de iniciar el análisis.");
      return;
    }
    if (!apiBaseUrl) {
      setApiError("Falta configurar VITE_API_BASE_URL. No se inició un análisis.");
      return;
    }
    setStage("analyzing");
    setApiError("");
    try {
      const form = new FormData();
      form.append("empresa_id", props.companyId);
      form.append("pais_codigo", props.countryCode);
      form.append("cuenta_id", selectedAccount.id);
      if (isCuscatlanAccount(selectedAccount)) form.append("periodo", selectedPeriod);
      documentFiles.forEach((file) => form.append("files", file));
      const response = await fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/`, {
        method: "POST",
        headers: { ...authHeaders, Accept: "application/json" },
        body: form,
      });
      const payload = await jsonResponse(response, `El servidor rechazó el análisis (HTTP ${response.status}).`);
      const job = payload as {
        id: string;
        cuenta_id: string;
        cuenta_seleccionada?: AccountOption;
        vinculacion_cuenta?: AccountLinkSuggestion | null;
        estado: string;
        progreso: { etapa: string; estado: Stage["status"] }[];
        resultado?: Record<string, any>;
      };
      setActiveJobId(job.id);
      setActiveJobAccountId(job.cuenta_id);
      if (job.cuenta_seleccionada) setSelectedAccount(job.cuenta_seleccionada);
      setAccountLinkSuggestion(job.vinculacion_cuenta ?? null);
      setActiveResult(job.resultado ?? null);
      const savedPeriod = job.resultado?.contexto_resultado?.periodo;
      if (typeof savedPeriod === "string" && /^\d{4}-\d{2}$/.test(savedPeriod)) setSelectedPeriod(savedPeriod);
      setStages((current) => current.map((item, index) => {
        const server = job.progreso[index];
        return server ? { name: item.name, status: server.estado } : item;
      }));
      setStage(job.estado === "preparado" ? "complete" : job.estado === "requiere_aclaraciones" ? "clarifications" : "idle");
      if (job.estado === "fallido") setApiError("El análisis falló. El diagnóstico quedó guardado en el servidor.");
    } catch (error: unknown) {
      setStage("idle");
      setApiError(error instanceof Error ? error.message : "No se pudo iniciar el análisis.");
    }
  }

  function downloadWorkbook() {
    if (!activeJobId || !apiBaseUrl) return;
    const params = new URLSearchParams({
      empresa_id: props.companyId,
      pais_codigo: props.countryCode,
      formato: "xlsx",
    });
    if (activeJobAccountId) params.set("cuenta_id", activeJobAccountId);
    void fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${activeJobId}/reporte/?${params}`, { headers: authHeaders })
      .then(async (response) => {
        if (!response.ok) throw new Error(await responseError(response, `No se pudo descargar el informe (HTTP ${response.status}).`));
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `Conciliacion-${props.countryCode}-${props.companyName}.xlsx`;
        link.click();
        URL.revokeObjectURL(url);
      })
      .catch((error: unknown) => setApiError(error instanceof Error ? error.message : "No se pudo descargar el informe."));
  }

  function downloadPdf() {
    if (!activeJobId || !apiBaseUrl) return;
    const params = new URLSearchParams({
      empresa_id: props.companyId,
      pais_codigo: props.countryCode,
      formato: "pdf",
    });
    if (activeJobAccountId) params.set("cuenta_id", activeJobAccountId);
    void fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${activeJobId}/reporte/?${params}`, { headers: authHeaders })
      .then(async (response) => {
        if (!response.ok) throw new Error(await responseError(response, `No se pudo descargar el PDF (HTTP ${response.status}).`));
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `Conciliacion-${props.countryCode}-${props.companyName}.pdf`;
        link.click();
        URL.revokeObjectURL(url);
      })
      .catch((error: unknown) => setApiError(error instanceof Error ? error.message : "No se pudo descargar el PDF."));
  }

  async function readSourceRow(source: SourceLocation): Promise<SourceRow> {
    if (!activeJobId || !activeJobAccountId || !apiBaseUrl) throw new Error("La fila de origen no está disponible.");
    const params = new URLSearchParams({
      empresa_id: props.companyId,
      pais_codigo: props.countryCode,
      cuenta_id: activeJobAccountId,
      hoja: source.hoja,
      fila: String(source.fila),
    });
    const response = await fetch(
      `${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${activeJobId}/archivos/${encodeURIComponent(source.archivo)}/?${params}`,
      { headers: { ...authHeaders, Accept: "application/json" } },
    );
    if (!response.ok) throw new Error(await responseError(response, "No se pudo leer la fila de origen."));
    return await response.json() as SourceRow;
  }

  async function downloadSource(filename: string) {
    if (!activeJobId || !activeJobAccountId || !apiBaseUrl) return;
    const params = new URLSearchParams({ empresa_id: props.companyId, pais_codigo: props.countryCode, cuenta_id: activeJobAccountId });
    const response = await fetch(
      `${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${activeJobId}/archivos/${encodeURIComponent(filename)}/?${params}`,
      { headers: authHeaders },
    );
    if (!response.ok) throw new Error(await responseError(response, `No se pudo abrir el documento de origen (HTTP ${response.status}).`));
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  }

  async function saveToHistory() {
    if (!activeJobId || !apiBaseUrl) return;
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setApiError("");
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${activeJobId}/`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          empresa_id: props.companyId,
          pais_codigo: props.countryCode,
          cuenta_id: activeJobAccountId,
          accion: "finalizar",
        }),
      });
      if (!response.ok) throw new Error(await responseError(response, `No se pudo guardar el trabajo (HTTP ${response.status}).`));
      const savedJob = await response.json() as { cuenta_id?: string };
      setDocumentFiles([]);
      setDocuments([]);
      setActiveResult(null);
      setActiveJobId("");
      setActiveJobAccountId("");
      setStage("idle");
      localStorage.removeItem(storageKey);
      if (savedJob.cuenta_id) props.onSavedAccount?.(savedJob.cuenta_id);
    } catch (error: unknown) {
      setApiError(error instanceof Error ? error.message : "No se pudo guardar el trabajo.");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  async function recalculateJob(jobId = activeJobId, accountId = activeJobAccountId, replacementFiles?: File[], replacementName?: string) {
    if (!jobId || !apiBaseUrl) return;
    if (!window.confirm("Se sustituirá el resultado visible solo cuando termine el recálculo. El resultado anterior se conservará si ocurre un fallo.")) return;
    setApiError("");
    setStage("analyzing");
    try {
      const form = new FormData();
      form.append("empresa_id", props.companyId);
      form.append("pais_codigo", props.countryCode);
      form.append("cuenta_id", accountId);
      form.append("accion", "recalcular");
      if (isCuscatlanAccount(selectedAccount)) form.append("periodo", selectedPeriod);
      if (replacementFiles) {
        replacementFiles.forEach((file) => form.append("files", file));
        if (replacementName) form.append("reemplazar_archivo", replacementName);
      } else if (props.mode === "new") documentFiles.forEach((file) => form.append("files", file));
      const response = await fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${jobId}/`, {
        method: "POST",
        headers: authHeaders,
        body: form,
      });
      if (!response.ok) throw new Error(await responseError(response, `No se pudo recalcular (HTTP ${response.status}).`));
      const job = await response.json() as { id: string; cuenta_id?: string; cuenta_seleccionada?: AccountOption; vinculacion_cuenta?: AccountLinkSuggestion | null; resultado?: Record<string, any>; estado: string };
      setActiveJobId(job.id);
      setActiveJobAccountId(job.cuenta_id ?? accountId);
      if (job.cuenta_seleccionada) setSelectedAccount(job.cuenta_seleccionada);
      setAccountLinkSuggestion(job.vinculacion_cuenta ?? null);
      setActiveResult(job.resultado ?? {});
      const savedPeriod = job.resultado?.contexto_resultado?.periodo;
      if (typeof savedPeriod === "string" && /^\d{4}-\d{2}$/.test(savedPeriod)) setSelectedPeriod(savedPeriod);
      setStage(job.estado === "requiere_aclaraciones" ? "clarifications" : "complete");
    } catch (error: unknown) {
      setStage("idle");
      setApiError(error instanceof Error ? error.message : "El recálculo falló; se conservó el resultado anterior.");
    }

  }

  async function resolveClosingBalance(resolution: { tipo: "confirmar" | "manual"; saldo?: string; motivo?: string }) {
    if (!activeJobId || !apiBaseUrl) return;
    setApiError("");
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${activeJobId}/`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          empresa_id: props.companyId,
          pais_codigo: props.countryCode,
          cuenta_id: activeJobAccountId,
          accion: "resolver_cierre",
          tipo: resolution.tipo,
          saldo_confirmado: resolution.saldo,
          motivo: resolution.motivo,
          moneda: selectedAccount?.moneda_codigo,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response, `No se pudo guardar la resolución del cierre (HTTP ${response.status}).`));
      const job = await response.json() as { id: string; estado: string; resultado?: Record<string, any> };
      setActiveResult(job.resultado ?? {});
      setStage(job.estado === "requiere_aclaraciones" ? "clarifications" : "complete");
      setHistoryJobs((current) => current.map((item) => String(item.id) === activeJobId ? { ...item, estado: job.estado, resultado: job.resultado } : item));
    } catch (error: unknown) {
      setApiError(error instanceof Error ? error.message : "No se pudo guardar la resolución del cierre.");
    }
  }

  function openClosingFileReplacement(filename?: string) {
    if (props.mode === "new" && filename) {
      const index = documents.findIndex((item) => item.name === filename);
      if (index >= 0) {
        replacingIndex.current = index;
        replaceInput.current?.click();
        return;
      }
    }
    closureReplacementName.current = filename;
    closureReplaceInput.current?.click();
  }

  async function confirmObservation(index: number, code: string) {
    if (!activeJobId || !activeJobAccountId || !apiBaseUrl || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setApiError("");
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${activeJobId}/`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          empresa_id: props.companyId,
          pais_codigo: props.countryCode,
          cuenta_id: activeJobAccountId,
          accion: "confirmar_revision",
          indice: index,
          codigo: code,
        }),
      });
      const job = await jsonResponse(response, "No se pudo guardar la confirmación de revisión.");
      setActiveResult(job.resultado as Record<string, any>);
    } catch (error: unknown) {
      setApiError(error instanceof Error ? error.message : "No se pudo guardar la confirmación de revisión.");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  async function confirmAccountNumberLink() {
    if (!activeJobId || !activeJobAccountId || !accountLinkSuggestion || !apiBaseUrl) return;
    const confirmed = window.confirm(
      `¿Confirmas vincular la cuenta activa por terminación ${accountLinkSuggestion.terminacion} con el número completo ${accountLinkSuggestion.numero_completo}? No se creará otra cuenta.`
    );
    if (!confirmed) return;
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${activeJobId}/`, {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          empresa_id: props.companyId,
          pais_codigo: props.countryCode,
          cuenta_id: activeJobAccountId,
          accion: "vincular_numero",
        }),
      });
      if (!response.ok) throw new Error(await responseError(response, `No se pudo vincular el número (HTTP ${response.status}).`));
      const job = await response.json() as { cuenta_seleccionada?: AccountOption; vinculacion_cuenta?: AccountLinkSuggestion | null };
      if (job.cuenta_seleccionada) {
        setSelectedAccount(job.cuenta_seleccionada);
        setAccounts((current) => current.map((item) => item.id === job.cuenta_seleccionada?.id ? job.cuenta_seleccionada! : item));
      }
      setAccountLinkSuggestion(job.vinculacion_cuenta ?? null);
      setApiError("");
    } catch (error: unknown) {
      setApiError(error instanceof Error ? error.message : "No se pudo vincular el número completo.");
    }
  }

  async function discardOrDeleteJob(jobId = activeJobId, fromHistory = false) {
    if (!jobId || !apiBaseUrl) return;
    const scopeText = fromHistory
      ? "Este trabajo se eliminará junto con sus archivos exclusivos. No afecta cuentas, bancos ni otros períodos."
      : "Se quitará el borrador actual y sus archivos exclusivos. El resultado finalizado anterior, si existe, no se modificará.";
    if (!window.confirm(scopeText)) return;
    try {
      const params = new URLSearchParams({ empresa_id: props.companyId, pais_codigo: props.countryCode });
      const response = await fetch(`${apiBaseUrl}/api/v1/conciliaciones-directas/trabajos/${jobId}/?${params}`, {
        method: "DELETE",
        headers: authHeaders,
      });
      if (!response.ok) throw new Error(await responseError(response, `No se pudo eliminar el trabajo (HTTP ${response.status}).`));
      if (!fromHistory) {
        setDocumentFiles([]);
        setDocuments([]);
        setActiveJobId("");
        setActiveResult(null);
        setStage("idle");
        localStorage.removeItem(storageKey);
      } else {
        setHistoryJobs((current) => current.filter((item) => String(item.id) !== jobId));
      }
    } catch (error: unknown) {
      setApiError(error instanceof Error ? error.message : "No se pudo eliminar el trabajo.");
    }
  }

  if (props.mode === "history") {
    return <section className="direct-module">
      <header><p className="eyebrow">CONCILIACIONES</p><h1>Conciliaciones de la cuenta o banco</h1>{props.cuentaId && <button type="button" className="quiet-button" onClick={props.onBackToAccount}>Volver a la cuenta</button>}{props.bancoId && <button type="button" className="quiet-button" onClick={props.onBackToBank}>Volver al banco</button>}</header>
      {apiError && <div className="notice error" role="alert"><span>{apiError}</span><button type="button" className="quiet-button" onClick={() => window.location.reload()}>Reintentar</button></div>}
      <input ref={closureReplaceInput} hidden type="file" accept=".xlsx,.xls" onChange={(event) => {
        const file = event.target.files?.[0];
        if (file) void recalculateJob(activeJobId, activeJobAccountId, [file], closureReplacementName.current).catch((error: unknown) => setApiError(error instanceof Error ? error.message : "No se pudo reemplazar el archivo."));
        event.currentTarget.value = "";
      }} />
      {activeResult && <ReconciliationDashboard
        result={activeResult}
        job={historyJobs.find((item) => String(item.id) === activeJobId)}
        companyName={props.companyName}
        countryCode={props.countryCode}
        saved={true}
        onBackToHistory={returnFromPreviousReconciliation}
        onBackLabel={dashboardReturnState ? "Volver a la conciliación" : undefined}
        onDownloadExcel={downloadWorkbook}
        onDownloadPdf={downloadPdf}
        onPrint={() => window.print()}
        onRecalculate={() => void recalculateJob(activeJobId)}
        canEdit={props.role === "administrador" || props.role === "auxiliar"}
        onResolveClosingBalance={resolveClosingBalance}
        onConfirmObservation={confirmObservation}
        onReplaceClosingFile={openClosingFileReplacement}
        onOpenPreviousReconciliation={(jobId, accountId) => void openPreviousReconciliation(jobId, accountId)}
        onOpenSource={(filename) => void downloadSource(filename).catch((error: unknown) => setApiError(error instanceof Error ? error.message : "No se pudo abrir el documento de origen."))}
        onReadSourceRow={readSourceRow}
      />}
      <div className="direct-card">
        {!historyJobs.length ? <p>No hay trabajos registrados en el servidor para este usuario, empresa y país.</p> : <ul className="document-list">
          {historyJobs.map((job) => <li key={String(job.id)}>
            {(() => {
              const context = (job.contexto as Record<string, unknown> | undefined) ?? {};
              const selected = (job.cuenta_seleccionada as AccountOption | undefined) ?? undefined;
              const bank = String(selected?.banco ?? context.banco ?? "Banco no disponible");
              const account = selected ? `${selected.alias} · ${accountNumberLabel(selected)}` : String(context.cuenta ?? context.numero_cuenta ?? "Cuenta no disponible");
              const period = formatJobPeriod(job.periodo_sugerido ?? context.periodo ?? context.periodo_declarado);
              const responsible = typeof job.responsable_nombre === "string" && job.responsable_nombre.trim()
                ? job.responsable_nombre
                : "Responsable no disponible";
              const date = formatJobDate(job.finalizado_en ?? job.actualizado_en);
              return <span className="history-job-identification"><strong>{bank} · {account}</strong><small>{period}</small><small>{formatStateLabel(job.estado)} · {responsible} · {date}</small></span>;
            })()}
            <span className="document-actions">
              <button type="button" className="secondary-button" onClick={() => void loadJob(String(job.id), String(job.cuenta_id ?? "")).catch((error: unknown) => setApiError(error instanceof Error ? error.message : "No se pudo abrir el trabajo."))}>Abrir dashboard</button>
              <button type="button" className="quiet-button" onClick={() => void recalculateJob(String(job.id), String(job.cuenta_id ?? ""))}>Recalcular</button>
              <button type="button" className="danger-button" onClick={() => void discardOrDeleteJob(String(job.id), true)}>Eliminar</button>
            </span>
          </li>)}
        </ul>}
      </div>
    </section>;
  }

  return <section className="direct-module">
    {apiError && <div className="notice error" role="alert">
      <span>{apiError}</span>
      {activeJobId && activeResult && <button type="button" className="quiet-button" disabled={saving} onClick={() => void saveToHistory()}>{saving ? "Guardando…" : "Reintentar guardar"}</button>}
      {accountError && <button type="button" className="quiet-button" onClick={() => setAccountReload((value) => value + 1)}>Reintentar cargar cuentas</button>}
    </div>}
    <header className="direct-header">
      <div><p className="eyebrow">NUEVA CONCILIACIÓN · {props.countryName}</p><h1>Analiza los documentos juntos</h1></div>
      <span className={`analysis-status ${stage}`}>{stage === "complete" ? "Análisis terminado" : stage === "clarifications" ? "Requiere aclaraciones" : stage === "analyzing" ? "Analizando" : "No iniciado"}</span>
    </header>

    <div className="direct-card account-selection">
      <div>
        <h2>Cuenta de la conciliación</h2>
        {selectedAccount ? <>
          <strong>{selectedAccount.banco} · {selectedAccount.alias}</strong>
          <span>{accountNumberLabel(selectedAccount)} · {selectedAccount.moneda_codigo}</span>
          <small>Configuración: {selectedAccount.incorporacion_provisional ? "Activa; falta verificar el número completo" : "Activa"} · Código contable: {selectedAccount.codigo_contable || "No asociado"}</small>
        </> : <span>Elige una cuenta activa autorizada para esta empresa y país antes de iniciar el análisis.</span>}
      </div>
      <button type="button" className={selectedAccount ? "quiet-button" : "primary-button"} disabled={accountLoading || stage === "analyzing"} onClick={() => { setAccountModalOpen(true); setAccountError(""); }}>
        {selectedAccount ? "Cambiar cuenta" : "Elegir cuenta"}
      </button>
    </div>

    <div className="direct-card drop-zone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); addFiles(event.dataTransfer.files); }}>
      <input ref={fileInput} hidden type="file" accept=".xlsx,.xls" multiple onChange={(event) => addFiles(event.target.files)} />
      <strong>Suelta aquí el estado, mayor y documentos históricos</strong>
      <span>.xlsx o .xls · todos los archivos a la vez</span>
      <button type="button" className="secondary-button" onClick={() => fileInput.current?.click()}>Seleccionar documentos</button>
    </div>

    {documents.length > 0 && <div className="direct-card">
      <h2>Documentos recibidos</h2>
      {isCuscatlanAccount(selectedAccount) && <label className="account-search">
        Mes y año a conciliar
        <input type="month" value={selectedPeriod} onChange={(event) => setSelectedPeriod(event.target.value)} required />
      </label>}
      <ul className="document-list">
        {documents.map((document, index) => <li key={`${document.name}-${document.lastModified}`}>
          <span><strong>{document.name}</strong><small>{(document.size / 1024).toFixed(1)} KiB</small></span>
          <span className="document-actions">
            <input ref={replaceInput} hidden type="file" accept=".xlsx,.xls" onChange={(event) => replaceFile(replacingIndex.current ?? index, event.target.files)} />
            <button type="button" className="quiet-button" onClick={() => { replacingIndex.current = index; replaceInput.current?.click(); }}>Reemplazar</button>
            <button type="button" className="danger-button" onClick={() => { setDocuments((current) => current.filter((_, itemIndex) => itemIndex !== index)); setDocumentFiles((current) => current.filter((_, itemIndex) => itemIndex !== index)); setStage("idle"); }}>Quitar</button>
          </span>
        </li>)}
      </ul>
      {!selectedAccount && <p className="empty-copy">Selecciona una cuenta para habilitar el análisis.</p>}
      <button type="button" className="primary-button analyze-button" disabled={stage === "analyzing" || !selectedAccount || (isCuscatlanAccount(selectedAccount) && !selectedPeriod)} onClick={() => void analyze()}>{stage === "analyzing" ? "Analizando…" : "Analizar y conciliar"}</button>
    </div>}

    {accountModalOpen && <div className="account-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setAccountModalOpen(false); }} onKeyDown={(event) => { if (event.key === "Escape") setAccountModalOpen(false); }}>
      <section className="account-modal" role="dialog" aria-modal="true" aria-labelledby="account-modal-title">
        <header><div><p className="eyebrow">{props.companyName} · {props.countryName}</p><h2 id="account-modal-title">Elegir cuenta</h2></div><button type="button" className="quiet-button" aria-label="Cerrar" onClick={() => setAccountModalOpen(false)}>Cerrar</button></header>
        <label className="account-search">Buscar por banco, alias o número<input autoFocus type="search" value={accountSearch} onChange={(event) => setAccountSearch(event.target.value)} /></label>
        {accountLoading ? <p role="status">Cargando cuentas autorizadas…</p> : accountError ? <div className="notice error" role="alert"><span>{accountError}</span><button type="button" className="quiet-button" onClick={() => setAccountReload((value) => value + 1)}>Reintentar</button></div> : null}
        <ul className="account-options">
          {accounts.filter((account) => `${account.banco} ${account.alias} ${account.numero} ${account.numero_terminacion ?? ""} ${account.moneda_codigo}`.toLocaleLowerCase().includes(accountSearch.trim().toLocaleLowerCase())).map((account) => (
            <li key={account.id}>
              <button type="button" className={selectedAccount?.id === account.id ? "account-option selected" : "account-option"} onClick={() => chooseAccount(account)}>
                <strong>{account.banco} · {account.alias}</strong>
                <span>{accountNumberLabel(account)} · {account.moneda_codigo}</span>
                <small>{account.incorporacion_provisional ? "Configuración activa · número completo pendiente" : "Cuenta activa y configurada"} · Código contable: {account.codigo_contable || "No asociado"}</small>
              </button>
            </li>
          ))}
          {!accountLoading && !accountError && accounts.length === 0 && <li className="empty-copy">No hay cuentas activas autorizadas para este ámbito.</li>}
          {!accountLoading && accounts.length > 0 && !accounts.some((account) => `${account.banco} ${account.alias} ${account.numero} ${account.numero_terminacion ?? ""} ${account.moneda_codigo}`.toLocaleLowerCase().includes(accountSearch.trim().toLocaleLowerCase())) && <li className="empty-copy">No hay coincidencias con la búsqueda.</li>}
        </ul>
      </section>
    </div>}

    {stage === "analyzing" && <div className="direct-card progress-card">
      <h2>Progreso</h2>
      <ol className="stage-list">{stages.map((item) => <li key={item.name}><span className={`stage-dot ${item.status}`} />{item.name}<small>{item.status}</small></li>)}</ol>
    </div>}
    {stage === "complete" && <div className="direct-card progress-complete">
      <strong>Análisis terminado</strong>
      <details><summary>Ver detalle del análisis</summary><ol className="stage-list">{stages.map((item) => <li key={item.name}><span className={`stage-dot ${item.status}`} />{item.name}<small>{item.status}</small></li>)}</ol></details>
    </div>}

    {accountLinkSuggestion && <div className="direct-card account-link-suggestion">
      <div><strong>Coincidencia única por terminación</strong><p>El documento identifica {accountLinkSuggestion.numero_completo} y coincide con {selectedAccount?.banco} {selectedAccount ? accountNumberLabel(selectedAccount) : `terminación ${accountLinkSuggestion.terminacion}`} en {selectedAccount?.moneda_codigo}. El vínculo no altera el análisis ni crea otra cuenta.</p></div>
      {props.role === "administrador" && <button type="button" className="secondary-button" onClick={() => void confirmAccountNumberLink()}>Confirmar vínculo</button>}
    </div>}

    {activeResult && <ReconciliationDashboard
      result={activeResult}
      job={{ id: activeJobId, cuenta_id: activeJobAccountId, cuenta_seleccionada: selectedAccount, estado: stage === "clarifications" ? "requiere_aclaraciones" : stage === "complete" ? "preparado" : undefined, responsable_nombre: undefined, finalizado_en: undefined, actualizado_en: undefined }}
      companyName={props.companyName}
      countryCode={props.countryCode}
      saved={Boolean(dashboardReturnState)}
      onBackToHistory={dashboardReturnState ? returnFromPreviousReconciliation : undefined}
      onBackLabel={dashboardReturnState ? "Volver a la conciliación" : undefined}
      saving={saving}
      onSave={() => void saveToHistory()}
      onDiscard={() => void discardOrDeleteJob()}
      onDownloadExcel={downloadWorkbook}
      onDownloadPdf={downloadPdf}
      onPrint={() => window.print()}
      onRecalculate={() => void recalculateJob()}
      canEdit={props.role === "administrador" || props.role === "auxiliar"}
      onResolveClosingBalance={resolveClosingBalance}
      onConfirmObservation={confirmObservation}
      onReplaceClosingFile={openClosingFileReplacement}
      onOpenPreviousReconciliation={(jobId, accountId) => void openPreviousReconciliation(jobId, accountId)}
      onOpenSource={(filename) => void downloadSource(filename).catch((error: unknown) => setApiError(error instanceof Error ? error.message : "No se pudo abrir el documento de origen."))}
      onReadSourceRow={readSourceRow}
    />}
    {activeResult && false && (stage === "complete" || stage === "clarifications") && (() => {
      const result = activeResult!;
      const resumen = (result.resumen ?? {}) as Record<string, any>;
      const proceso = (result.proceso_conciliacion ?? {}) as Record<string, any>;
      const depositos = (result.depositos ?? {}) as Record<string, any>;
      const cheques = (result.cheques ?? []) as Record<string, any>[];
      const diagnosticos = (result.diagnosticos ?? []) as Record<string, any>[];
      const matches = (proceso.coincidencias_confirmadas ?? []) as Record<string, any>[];
      const bankUnmatched = (proceso.movimientos_sin_contraparte?.banco ?? []) as Record<string, any>[];
      const booksUnmatched = (proceso.movimientos_sin_contraparte?.contabilidad ?? []) as Record<string, any>[];
      return <div className="direct-card reconciliation-dashboard">
        <nav className="dashboard-tabs" aria-label="Apartados de conciliación">
          {(["resumen","proceso","depositos","cheques"] as const).map((tab) => <button key={tab} type="button" className={dashboardTab === tab ? "active" : ""} onClick={() => setDashboardTab(tab)}>
            {tab === "resumen" ? "Resumen" : tab === "proceso" ? "Proceso" : tab === "depositos" ? "Depósitos" : "Cheques en circulación"}
          </button>)}
        </nav>
        {dashboardTab === "resumen" && <div>
          <div className="summary-grid">
            <article><span>Saldo final Banco</span><strong>{resumen.saldo_final_banco ? `Q ${resumen.saldo_final_banco}` : "Por confirmar"}</strong></article>
            <article><span>Saldo final Contabilidad</span><strong>{resumen.saldo_final_contabilidad ? `Q ${resumen.saldo_final_contabilidad}` : "Por confirmar"}</strong></article>
            <article><span>Diferencia original</span><strong>{resumen.diferencia_original ?? "Por confirmar"}</strong></article>
            <article><span>Diferencia residual pendiente</span><strong>{resumen.diferencia_residual_pendiente ?? "Por confirmar"}</strong></article>
          </div>
          <div className="summary-grid">
            <article><span>Cruces confirmados</span><strong>{proceso.conteos?.cruces_confirmados ?? 0}</strong></article>
            <article><span>Movimientos Banco consumidos</span><strong>{proceso.conteos?.movimientos_banco_consumidos ?? 0}</strong></article>
            <article><span>Movimientos Contabilidad consumidos</span><strong>{proceso.conteos?.movimientos_contabilidad_consumidos ?? 0}</strong></article>
            <article><span>Alternativas / pendientes</span><strong>{(proceso.conteos?.alternativas_ambiguas ?? 0) + (proceso.conteos?.pendientes_banco ?? 0) + (proceso.conteos?.pendientes_contabilidad ?? 0)}</strong></article>
          </div>
          <h3>Partidas conciliatorias verificadas</h3>
          {(resumen.partidas_conciliatorias ?? []).length > 0
            ? <ul>{(resumen.partidas_conciliatorias ?? []).map((partida: Record<string, any>, index: number) => <li key={index}>{partida.concepto}: {partida.importe} · {partida.lado} · {partida.signo}</li>)}</ul>
            : <p>No hay partidas explicativas verificadas. Los cruces son movimientos consumidos y no explican por sí solos la diferencia entre saldos.</p>}
          {diagnosticos.length > 0 && <div className="diagnostics"><h3>Aclaraciones requeridas</h3>{diagnosticos.map((item, index) => <p key={index}><strong>{item.codigo}</strong>: {item.mensaje}</p>)}</div>}
        </div>}
        {dashboardTab === "proceso" && <div className="table-scroll">
          <table><thead><tr><th>Fecha Banco</th><th>Referencia Banco</th><th>Descripción Banco</th><th>Monto Banco</th><th>Fecha Contabilidad</th><th>Referencia Contabilidad</th><th>Descripción Contabilidad</th><th>Monto Contabilidad</th><th>Diferencia</th><th>Criterio</th><th>Resultado</th></tr></thead><tbody>
            {matches.map((match, index) => <tr key={index}><td>{match.fecha_banco}</td><td>{match.referencia}</td><td>{match.descripcion_banco ?? "—"}</td><td>{match.monto_banco}</td><td>{match.fecha_contabilidad}</td><td>{match.referencias_alternativas?.join(", ") ?? match.referencia}</td><td>{match.descripcion_contabilidad ?? "—"}</td><td>{match.monto_contabilidad}</td><td>{match.diferencia ?? "0"}</td><td>{match.criterio}</td><td>{match.resultado}</td></tr>)}
            {bankUnmatched.slice(0, 100).map((item, index) => {
              const movement = item.movimiento ?? {};
              return <tr key={`b${index}`}><td>{movement.fecha}</td><td>{movement.referencia ?? movement.numero_transaccion ?? movement.numero_cheque ?? "—"}</td><td>{movement.descripcion ?? "—"}</td><td>{movement.monto ?? "—"}</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>Sin contraparte inequívoca</td><td>Requiere revisión</td></tr>;
            })}
            {!matches.length && !bankUnmatched.length && !booksUnmatched.length && <tr><td colSpan={11}>No hay movimientos evaluables con los documentos reconocidos.</td></tr>}
          </tbody></table>
        </div>}
        {dashboardTab === "depositos" && <div>
          <h3>Ingresos y casos sin contraparte</h3>
          <p>{(depositos.en_transito ?? []).length} tránsitos contables · {(depositos.ingresos_bancarios_sin_naturaleza_confirmada ?? []).length} ingresos bancarios por naturaleza · {(depositos.salidas_pendientes ?? []).length} salidas pendientes excluidas de depósitos.</p>
          <div className="table-scroll"><table><thead><tr><th>Fecha</th><th>Referencia</th><th>Descripción</th><th>Importe</th><th>Situación</th><th>Evidencia</th></tr></thead><tbody>
            {(depositos.sin_contraparte ?? []).slice(0, 100).map((item: Record<string, any>, index: number) => { const movement = item.movimiento ?? {}; return <tr key={index}><td>{movement.fecha ?? "—"}</td><td>{movement.referencia ?? movement.numero_transaccion ?? "—"}</td><td>{movement.descripcion ?? "—"}</td><td>{movement.monto ?? "—"}</td><td>{item.situacion}</td><td>{item.evidencia?.archivo} · fila {item.evidencia?.fila}</td></tr>; })}
          </tbody></table></div>
        </div>}
        {dashboardTab === "cheques" && <div>
          {(cheques as Record<string, any>[]).map((cut, cutIndex) => <section key={cutIndex} className="cheque-cut">
            <h3>Corte {cut.periodo_corte ?? "por confirmar"}</h3>
            <div className="table-scroll"><table><thead><tr><th>Número</th><th>Fecha</th><th>Descripción</th><th>Monto</th><th>Estado al corte</th><th>Origen</th></tr></thead><tbody>
              {(cut.registros ?? []).map((record: Record<string, any>, index: number) => {
                const cobro = (record.cobro_identificado ?? []) as Record<string, any>[];
                const estado = record.estado_al_corte === "cobrado_identificado_en_banco" ? "Cobrado identificado en Banco" : record.estado_al_corte === "pendiente_identificacion" ? "Pendiente de identificación (número 0)" : "Pendiente al corte actual";
                return <tr key={index}><td>{record.numero_cero ? "0 / requiere identificación" : record.numero_documento}</td><td>{record.fecha ?? "—"}</td><td>{record.descripcion ?? "—"}</td><td>{record.monto}</td><td>{estado}{cobro.length ? ` · ${cobro[0].fecha} / ${cobro[0].monto}` : ""}</td><td>{record.archivo} · {record.hoja} · fila {record.fila}</td></tr>;
              })}
            </tbody></table></div>
          </section>)}
          {!cheques.length && <p>No se recibió ni reconoció un corte histórico de cheques.</p>}
        </div>}
      </div>;
    })()}

    {stage === "clarifications" && !activeResult && <div className="direct-card clarifications">
      <h2>Revisión pendiente</h2>
      <p>Revisá los documentos cargados antes de continuar.</p>
    </div>}
  </section>;
}
