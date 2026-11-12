import { useEffect, useRef, useState } from "react";

type Props = {
  result: Record<string, any>;
  job?: Record<string, any>;
  companyName: string;
  countryCode: "GT" | "SV";
  saved?: boolean;
  onBackToHistory?: () => void;
  onBackLabel?: string;
  onSave?: () => void;
  onDiscard?: () => void;
  onDownloadExcel?: () => void;
  onDownloadPdf?: () => void;
  onPrint?: () => void;
  onRecalculate?: () => void;
  canEdit?: boolean;
  onResolveClosingBalance?: (resolution: { tipo: "confirmar" | "manual"; saldo?: string; motivo?: string }) => Promise<void>;
  onConfirmObservation?: (index: number, code: string) => Promise<void>;
  onReplaceClosingFile?: (filename?: string) => void;
  onOpenPreviousReconciliation?: (jobId: string, accountId: string) => void;
  onOpenSource?: (filename: string) => void;
  onReadSourceRow?: (source: SourceLocation) => Promise<SourceRow>;
  saving?: boolean;
};

export type SourceLocation = { archivo: string; hoja: string; fila: number };
export type SourceRow = SourceLocation & {
  celdas: { columna: string; encabezado: string | null; valor: string | number | boolean }[];
};

function SourceRowPreview({ source, label, onRead }: {
  source: SourceLocation; label?: string; onRead?: Props["onReadSourceRow"];
}) {
  const [data, setData] = useState<SourceRow | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    if (!onRead || loading) return;
    setLoading(true);
    setError("");
    try {
      setData(await onRead(source));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "No se pudo leer la fila.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="observation-source-row">
      {label && <strong>{label}</strong>}
      <small>{source.archivo} · Hoja {source.hoja} · Fila {source.fila}</small>
      {onRead && (
        <details onToggle={(event) => {
          if (event.currentTarget.open && !data && !error) void load();
        }}>
          <summary>Ver contenido de la fila</summary>
          {loading && <small role="status">Cargando fila…</small>}
          {error && <><small role="alert">{error}</small><button type="button" className="quiet-button" onClick={() => void load()}>Reintentar</button></>}
          {data && (data.celdas.length
            ? <dl>{data.celdas.map((cell) => (
              <div key={cell.columna}>
                <dt>{cell.columna}{cell.encabezado ? ` · ${cell.encabezado}` : ""}</dt>
                <dd>{String(cell.valor)}</dd>
              </div>
            ))}</dl>
            : <small>La fila está vacía.</small>)}
        </details>
      )}
    </div>
  );
}

function formatMoney(value: unknown, currency = "") {
  if (value === null || value === undefined || value === "") return "No disponible";
  const number = Number(String(value).replace(/,/g, ""));
  if (!Number.isFinite(number)) return String(value);
  const formatted = new Intl.NumberFormat("es-GT", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(number);
  return currency === "GTQ" ? `Q${formatted}` : currency === "USD" ? `$${formatted}` : formatted;
}

function formatPeriod(value: unknown) {
  if (!value) return "No disponible";
  const text = String(value);
  const match = text.match(/^(\d{4})-(\d{2})(?:-|$)/);
  if (!match) return text;
  const names = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"];
  const month = Number(match[2]);
  return month >= 1 && month <= 12 ? `${names[month - 1]} de ${match[1]}` : text;
}

function formatLocalDate(value: unknown) {
  if (!value) return "No disponible";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return "No disponible";
  return new Intl.DateTimeFormat("es-SV", {
    timeZone: "America/El_Salvador",
    day: "2-digit",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function Evidence({ evidence, movement, onOpenSource, label = "Ver evidencia" }: { evidence?: Record<string, any>; movement?: Record<string, any>; onOpenSource?: (filename: string) => void; label?: string }) {
  const source = evidence ?? movement?.datos_originales ?? {};
  const sourceText = [source.archivo, source.hoja, (source.fila ?? movement?.fila_origen) && `Fila ${source.fila ?? movement?.fila_origen}`].filter(Boolean).join(" · ");
  return (
    <><span className="printed-evidence">{sourceText || "Origen no disponible"}</span><details className="evidence-detail">
      <summary>{label}</summary>
      <dl>
        <div><dt>Archivo</dt><dd>{source.archivo ?? "No disponible"}</dd></div>
        <div><dt>Hoja</dt><dd>{source.hoja ?? "No disponible"}</dd></div>
        <div><dt>Fila</dt><dd>{source.fila ?? movement?.fila_origen ?? "No disponible"}</dd></div>
      </dl>
      {source.archivo && onOpenSource && <button type="button" className="quiet-button source-button" onClick={() => onOpenSource(String(source.archivo))}>Abrir documento de origen</button>}
    </details></>
  );
}

type RowData = Record<string, any>;
type Side = "Banco" | "Contabilidad" | "Corte histórico";
type DetailRow = { side: Side; movement: RowData; evidence?: RowData; followUp?: RowData };
type DetailCase = { kind: string; status: string; rows: DetailRow[]; note?: string; amount?: number };

const detailTabs = { resumen: "Resumen", movimientos: "Movimientos", depositos: "Depósitos", cheques: "Cheques" };
type DetailTab = keyof typeof detailTabs;
type ProcessSide = "banco" | "contabilidad";
type ProcessStatusLabel = "Conciliado" | "Por confirmar" | "Sin coincidencia" | "Cheque cobrado";
const movementGlossary = {
  "Conciliado": "El sistema encontró y confirmó el cruce entre Banco y Contabilidad.",
  "Diferencia en monto": "La referencia coincide, pero los importes son distintos. Hay que revisar ambos registros.",
  "Diferencia por centavos": "Los importes difieren por menos de una unidad de la moneda, por ejemplo Q0.25 o $0.25.",
  "Cheque cobrado": "Hay una salida en Banco asociada a un cheque. El caso indica si el cobro se identificó o sigue por confirmar.",
  "Cheque en circulación": "El cheque viene de este mes o de un corte anterior y todavía no se ha identificado su cobro.",
  "Depósito en circulación": "Es un depósito registrado en Contabilidad que aún no se ha encontrado en Banco.",
  "Anulación en el sistema": "Hay movimientos en Contabilidad que se compensan o podrían corresponder a una anulación. El caso indica si se identificó la reversión.",
  "Cheque rechazado": "La descripción indica un posible rechazo o devolución. Revisá la evidencia del caso.",
  "Agrupación de movimientos": "Varias partidas podrían corresponder a un solo registro. Por ejemplo, dos depósitos del mismo día que suman el importe registrado en Contabilidad.",
  "Sin contraparte": "El movimiento aparece de un lado y el sistema todavía no encontró con qué cruzarlo del otro lado.",
};

function processPendingRows(process: RowData, difference: RowData, side: ProcessSide) {
  const rows = new Map<string, { movement: RowData; status: ProcessStatusLabel }>();
  for (const [index, item] of (process.movimientos_sin_contraparte?.[side] ?? []).entries()) {
    const id = item.movimiento_id ?? `${side}-sin-cruce-${index}`;
    rows.set(id, { movement: item.movimiento ?? item, status: "Sin coincidencia" });
  }

  const markForReview = (id: string, movement: RowData) => {
    rows.set(id, { movement: rows.get(id)?.movement ?? movement, status: "Por confirmar" });
  };
  const prefix = side === "banco" ? "bank" : "books";
  for (const group of [...(process.alternativas_ambiguas ?? []), ...(process.diferencias_importe ?? [])]) {
    (group[side] ?? []).forEach((movement: RowData, index: number) => {
      const sourceIndex = group[`${side}_indices`]?.[index];
      const id = group[`${side}_ids`]?.[index] ?? (sourceIndex == null ? null : `${prefix}:${sourceIndex}`);
      if (id) markForReview(id, movement);
    });
  }
  for (const candidate of difference.candidatos ?? []) {
    for (const item of candidate[side] ?? []) {
      if (item.id && item.movimiento) markForReview(item.id, item.movimiento);
    }
  }
  for (const item of difference.partidas_explicativas ?? []) {
    if (side === "banco" && item.tipo === "cheque_historico_cobrado" && item.verificada) {
      const row = rows.get(item.id);
      if (row) row.status = "Cheque cobrado";
    }
  }
  return Array.from(rows, ([id, row]) => ({ id, ...row }));
}

function ProcessStatus({ status }: { status: ProcessStatusLabel }) {
  const color = status === "Por confirmar" ? "process-status-review"
    : status === "Sin coincidencia" ? "status-muted" : "status-ready";
  return <span className={`process-status ${color}`}>{status}</span>;
}

function detailRow(side: Side, item: RowData): DetailRow {
  return {
    side, movement: item.movimiento ?? item, evidence: item.evidencia,
    followUp: item.reflejado_en_periodo_posterior,
  };
}

function isDeposit(movement: RowData) {
  return Number(movement.monto) > 0 && /\bdep[oó]sitos?\b/i.test(movement.descripcion ?? "");
}

function isCheck(movement: RowData, countryCode: Props["countryCode"]) {
  const number = String(movement.numero_cheque ?? "").trim();
  return (number !== "" && !/^0+$/.test(number))
    || /\bcheques?\b/i.test(movement.descripcion ?? "")
    || (countryCode === "GT" && String(movement.codigo_tt ?? "").trim().toUpperCase() === "CQ");
}

function isRejectedCheck(row: DetailRow, countryCode: Props["countryCode"]) {
  return isCheck(row.movement, countryCode) && /\b(rechaz\w*|devuelt\w*|rechazo|devoluci[oó]n)\b/i.test(row.movement.descripcion ?? "");
}

function classifyDetail(result: RowData, countryCode: Props["countryCode"]) {
  const process = result.proceso_conciliacion ?? {};
  const difference = result.resumen?.explicacion_diferencia ?? {};
  const cases: DetailCase[] = [];
  const deposits: DetailCase[] = [];
  const checks: DetailCase[] = [];
  const classified = new Set<string>();
  const checkKeys = new Set<string>();
  const checkKey = (number: unknown, amount: unknown) => {
    const key = String(number ?? "").trim().replace(/^0+/, "");
    return key && amount != null && Number.isFinite(Number(amount)) ? `${key}:${Math.abs(Number(amount))}` : "";
  };

  for (const cut of result.cheques ?? []) {
    for (const record of cut.registros ?? []) {
      const collected = record.estado_al_corte === "cobrado_identificado_en_banco";
      const kind = collected ? "Cheque cobrado" : "Cheque en circulación";
      const key = checkKey(record.numero_documento, record.monto);
      if (key) checkKeys.add(key);
      const historical = detailRow("Corte histórico", {
        movimiento: { ...record, referencia: record.numero_cero ? "Sin número" : record.numero_documento },
        evidencia: { archivo: record.archivo ?? cut.nombre, hoja: record.hoja, fila: record.fila },
      });
      const entry = {
        kind,
        status: collected ? "Identificado en Banco" : record.estado_al_corte === "pendiente_identificacion" ? "Pendiente de identificación" : "Pendiente al corte",
        note: cut.periodo_corte ? `Corte de origen · ${formatPeriod(cut.periodo_corte)}` : undefined,
        rows: [historical, ...(record.cobro_identificado ?? []).map((item: RowData) => detailRow("Banco", item))],
      };
      checks.push(entry);
      cases.push(entry);
    }
  }

  for (const match of process.coincidencias_confirmadas ?? []) {
    const rows = [
      detailRow("Banco", { movimiento: match.banco ?? { fecha: match.fecha_banco, referencia: match.referencia, descripcion: match.descripcion_banco, monto: match.monto_banco }, evidencia: match.evidencia_banco }),
      detailRow("Contabilidad", { movimiento: match.contabilidad ?? { fecha: match.fecha_contabilidad, referencia: match.referencia, descripcion: match.descripcion_contabilidad, monto: match.monto_contabilidad }, evidencia: match.evidencia_contabilidad }),
    ];
    const rejected = rows.some((row) => isRejectedCheck(row, countryCode));
    const collected = !rejected && Number(rows[0].movement.monto) < 0 && rows.some((row) => isCheck(row.movement, countryCode));
    const kind = rejected ? "Cheque rechazado" : collected ? "Cheque cobrado" : "Conciliado";
    const entry = { kind, status: rejected ? "Cruce confirmado · revisar devolución" : "Cruce confirmado", rows, note: match.criterio };
    cases.push(entry);
    if (collected) checks.push(entry);
  }

  for (const group of difference.pendientes?.contabilidad?.reversiones_identificadas ?? []) {
    const rows = (group.movimientos ?? []).map((item: RowData) => {
      if (item.id) classified.add(item.id);
      return detailRow("Contabilidad", item);
    });
    cases.push({ kind: "Anulación en el sistema", status: "Reversión identificada", rows, note: group.motivo });
  }

  for (const candidate of difference.candidatos ?? []) {
    const bank = (candidate.banco ?? []).map((item: RowData) => detailRow("Banco", item)) as DetailRow[];
    const books = (candidate.contabilidad ?? []).map((item: RowData) => detailRow("Contabilidad", item)) as DetailRow[];
    for (const item of [...(candidate.banco ?? []), ...(candidate.contabilidad ?? [])]) {
      if (item.id) classified.add(item.id);
    }
    const rows = [...bank, ...books];
    const note = candidate.explicacion ?? candidate.criterio;
    if (candidate.tipo === "diferencia_de_importe") {
      const bankAmount = bank[0]?.movement.monto;
      const booksAmount = books[0]?.movement.monto;
      const cents = bank.length === 1 && books.length === 1 && bankAmount != null && booksAmount != null
        ? Math.round(Number(bankAmount) * 100) - Math.round(Number(booksAmount) * 100) : NaN;
      const byCents = Math.abs(cents) > 0 && Math.abs(cents) < 100;
      const entry = { kind: byCents ? "Diferencia por centavos" : "Diferencia en monto", status: "Por verificar", rows, note, amount: Number.isFinite(cents) ? cents / 100 : undefined };
      cases.push(entry);
      if (byCents || (bank.length === 1 && books.length === 1 && rows.every((row) => Number(row.movement.monto) > 0) && rows.some((row) => isDeposit(row.movement)))) {
        deposits.push({ ...entry, kind: byCents ? entry.kind : "Depósito mal operado" });
      }
    } else {
      const kind = candidate.tipo === "reversion_ambigua" ? "Anulación en el sistema" : "Agrupación de movimientos";
      cases.push({ kind, status: "Por verificar", rows, note });
    }
  }

  for (const [side, label] of [["banco", "Banco"], ["contabilidad", "Contabilidad"]] as const) {
    const pending = difference.pendientes?.[side]?.movimientos ?? process.movimientos_sin_contraparte?.[side] ?? [];
    for (const item of pending) {
      if (classified.has(item.id ?? item.movimiento_id)) continue;
      const row = detailRow(label, item);
      const movement = row.movement;
      const amount = Number(movement.monto);
      const rejected = isRejectedCheck(row, countryCode);
      const check = amount < 0 && isCheck(movement, countryCode);
      const deposit = side === "contabilidad" && isDeposit(movement);
      const kind = rejected ? "Cheque rechazado" : check ? side === "banco" ? "Cheque cobrado" : "Cheque en circulación" : deposit ? "Depósito en circulación" : "Sin contraparte";
      const entry = { kind, status: "Por verificar", rows: [row], note: item.estado_al_cierre ?? item.situacion };
      cases.push(entry);
      if (deposit) deposits.push(entry);
      const key = checkKey(movement.numero_cheque || movement.referencia, movement.monto);
      if (check && !rejected && (!key || !checkKeys.has(key))) checks.push(entry);
    }
  }

  return { cases, deposits, checks };
}

function DetailCaseList({ entries, currency, onOpenSource }: { entries: DetailCase[]; currency: string; onOpenSource?: Props["onOpenSource"] }) {
  return <div className="detail-cases">
    {entries.map((entry, index) => <article className="detail-case" key={index}>
      <div className="detail-case-title">
        <strong>{entry.kind}{entry.amount !== undefined && ` · ${formatMoney(entry.amount, currency)} (Banco menos Contabilidad)`}</strong>
        <span>{entry.status}</span>
      </div>
      {entry.note && <p className="empty-copy">{entry.note}</p>}
      <div className="difference-table-scroll"><table className="difference-table">
        <thead><tr><th>Origen</th><th>Fecha</th><th>Referencia / cheque</th><th>Descripción</th><th className="money">Importe</th><th>Evidencia</th></tr></thead>
        <tbody>{entry.rows.map(({ side, movement, evidence, followUp }, rowIndex) => <tr key={rowIndex}>
          <td>{side}</td><td>{movement.fecha ?? "—"}</td>
          <td>{movement.numero_cheque || movement.referencia || movement.numero_transaccion || "—"}</td>
          <td>{movement.beneficiario || movement.descripcion || "—"}</td>
          <td className="money">{formatMoney(movement.monto, currency)}</td>
          <td><Evidence evidence={evidence} movement={movement} onOpenSource={onOpenSource} />
            {followUp && <><small>{followUp.estado} · {followUp.fecha}</small><Evidence evidence={followUp.evidencia} onOpenSource={onOpenSource} label="Evidencia posterior" /></>}
          </td>
        </tr>)}</tbody>
      </table></div>
    </article>)}
  </div>;
}

function ReconciliationDetail({ result, currency, countryCode, context, initialTab, onClose, onPrint, onOpenSource, onOpenPrevious }: {
  result: RowData; currency: string; countryCode: Props["countryCode"]; context: string;
  initialTab: DetailTab;
  onClose: () => void; onPrint?: Props["onPrint"]; onOpenSource?: Props["onOpenSource"];
  onOpenPrevious?: (jobId: string) => void;
}) {
  const [tab, setTab] = useState(initialTab);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  const summary = result.resumen ?? {};
  const difference = summary.explicacion_diferencia ?? {};
  const { cases, deposits, checks } = classifyDetail(result, countryCode);
  const applied = difference.residual?.partidas_aplicadas ?? [];
  const adjustments = [
    { label: "Depósitos en circulación aplicados", types: ["deposito_transito", "deposito_en_circulacion"], banco: 0, contabilidad: 0 },
    { label: "Cheques en circulación aplicados", types: ["cheque_en_circulacion", "cheque_circulacion"], banco: 0, contabilidad: 0 },
    { label: "Depósitos mal operados y centavos aplicados", types: ["deposito_mal_operado", "diferencia_por_centavos"], banco: 0, contabilidad: 0 },
    { label: "Otros movimientos verificados para el cuadre", types: [], banco: 0, contabilidad: 0 },
  ];
  for (const item of applied) {
    const effect = Number(item.impacto_residual);
    if (!Number.isFinite(effect) || item.impacto_residual == null) continue;
    const adjustment = adjustments.find(({ types }) => types.includes(item.tipo)) ?? adjustments[3];
    if (String(item.lado).toLowerCase() === "banco") adjustment.banco += Math.round(effect * 100);
    if (String(item.lado).toLowerCase() === "contabilidad") adjustment.contabilidad -= Math.round(effect * 100);
  }
  const amounts = adjustments.reduce((total, item) => ({ banco: total.banco + item.banco, contabilidad: total.contabilidad + item.contabilidad }), { banco: 0, contabilidad: 0 });
  const explained = applied.filter((item: RowData) => item.tipo !== "cheque_historico_cobrado");
  const adjusted = (value: unknown, cents: number) => value == null || value === "" || !Number.isFinite(Number(value))
    ? null : (Math.round(Number(value) * 100) + cents) / 100;
  const selectedCases = tab === "depositos" ? deposits : checks;
  const reportGroups = tab === "depositos"
    ? ["Depósito mal operado", "Diferencia por centavos", "Depósito en circulación"]
    : ["Cheque en circulación", "Cheque cobrado"];
  const reportTotals = (entries: DetailCase[]) => {
    const cents = entries.reduce((total, entry) => total + Math.round(Math.abs(entry.amount ?? Number(entry.rows[0]?.movement.monto)) * 100), 0);
    return Number.isFinite(cents) ? formatMoney(cents / 100, currency) : "No disponible";
  };

  return <dialog ref={dialog} className="difference-modal" aria-labelledby="difference-modal-title" onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <header>
      <div><p className="eyebrow">{context}</p><h2 id="difference-modal-title">Detalle de conciliación</h2></div>
      <div className="detail-header-actions">
        <details className="movement-glossary">
          <summary><span aria-hidden="true">?</span> ¿Qué significa cada categoría?</summary>
          <div className="movement-glossary-panel">
            <p>El número entre paréntesis indica cuántos casos hay. Cada caso puede incluir varias filas.</p>
            <dl>
              {Object.entries(movementGlossary).map(([kind, description]) => (
                <div key={kind}><dt>{kind}</dt><dd>{description}</dd></div>
              ))}
            </dl>
          </div>
        </details>
        <button type="button" className="quiet-button" onClick={onClose} autoFocus>Cerrar</button>
      </div>
    </header>
    <nav className="dashboard-tabs" aria-label="Apartados del detalle">
      {(Object.keys(detailTabs) as DetailTab[]).map((item) => (
        <button type="button" key={item} className={tab === item ? "active" : ""} aria-pressed={tab === item} onClick={() => setTab(item)}>{detailTabs[item]}</button>
      ))}
    </nav>
    <section className="difference-section">
      <div className="detail-section-title"><h3>{detailTabs[tab]}</h3>
        {onPrint && tab !== "movimientos" && <button type="button" className="secondary-button" onClick={onPrint}>Imprimir {detailTabs[tab].toLowerCase()}</button>}
      </div>
      {tab === "resumen" && <>
        <p className="difference-note">El saldo del Banco es la referencia del cuadre. Los saldos ajustados solo incluyen partidas verificadas. Los candidatos se consultan en Movimientos, Depósitos y Cheques.</p>
        <div className="difference-table-scroll"><table className="difference-table">
          <thead><tr><th>Concepto</th><th className="money">Contabilidad</th><th className="money">Banco</th></tr></thead>
          <tbody>
            <tr><th scope="row">Saldos finales</th><td className="money">{formatMoney(summary.saldo_final_contabilidad, currency)}</td><td className="money">{formatMoney(summary.saldo_final_banco, currency)}</td></tr>
            <tr><th scope="row">{adjustments[0].label}</th><td className="money">{formatMoney(adjustments[0].contabilidad / 100, currency)}</td><td className="money">{formatMoney(adjustments[0].banco / 100, currency)}</td></tr>
            <tr><th scope="row">Subtotal</th><td className="money">{formatMoney(adjusted(summary.saldo_final_contabilidad, adjustments[0].contabilidad), currency)}</td><td className="money">{formatMoney(adjusted(summary.saldo_final_banco, adjustments[0].banco), currency)}</td></tr>
            {adjustments.slice(1).map((item) => <tr key={item.label}><th scope="row">{item.label}</th><td className="money">{formatMoney(item.contabilidad / 100, currency)}</td><td className="money">{formatMoney(item.banco / 100, currency)}</td></tr>)}
            <tr><th scope="row">Saldos ajustados</th><td className="money">{formatMoney(adjusted(summary.saldo_final_contabilidad, amounts.contabilidad), currency)}</td><td className="money">{formatMoney(adjusted(summary.saldo_final_banco, amounts.banco), currency)}</td></tr>
            <tr><th scope="row" colSpan={2}>Diferencia pendiente</th><td className="money"><strong>{formatMoney(summary.diferencia_residual_pendiente, currency)}</strong></td></tr>
          </tbody>
        </table></div>
        <p>{deposits.length} casos en Depósitos · {checks.filter((entry) => entry.kind === "Cheque en circulación").length} casos de cheques pendientes o por verificar</p>
        {applied.some((item: RowData) => item.tipo === "cheque_historico_cobrado") && <p>Consulta los cobros usados en el cuadre en <button type="button" className="detail-button" onClick={() => setTab("cheques")}>Cheques</button>.</p>}
        {explained.length > 0 && <div className="difference-table-scroll"><table className="difference-table">
          <thead><tr><th>Concepto</th><th>Lado</th><th className="money">Importe</th><th>Origen</th></tr></thead>
          <tbody>{explained.map((item: RowData, index: number) => <tr key={index}>
            <td>{item.concepto}</td><td>{item.lado}</td><td className="money">{formatMoney(item.importe_con_signo, currency)}</td>
            <td>{item.evidencia?.banco && <Evidence evidence={item.evidencia.banco} onOpenSource={onOpenSource} label="Evidencia bancaria" />}{item.evidencia?.historico && <Evidence evidence={item.evidencia.historico} onOpenSource={onOpenSource} label="Corte histórico" />}</td>
          </tr>)}</tbody>
        </table></div>}
        {applied.length === 0 && <p className="empty-copy">No hay partidas verificadas aplicadas.</p>}
        {difference.diferencia_inicial?.evidencia_banco && <Evidence evidence={difference.diferencia_inicial.evidencia_banco} onOpenSource={onOpenSource} label="Origen del saldo inicial de Banco" />}
        {difference.diferencia_inicial?.evidencia_contabilidad && <Evidence evidence={difference.diferencia_inicial.evidencia_contabilidad} onOpenSource={onOpenSource} label="Origen del saldo inicial de Contabilidad" />}
        {difference.conciliacion_anterior?.id && onOpenPrevious && <button type="button" className="quiet-button" onClick={() => { onClose(); onOpenPrevious(String(difference.conciliacion_anterior.id)); }}>Ver conciliación anterior</button>}
      </>}
      {tab === "movimientos" && (cases.length ? Object.keys(movementGlossary).map((kind) => {
        const entries = cases.filter((entry) => entry.kind === kind);
        return entries.length > 0 && <details key={kind} className="detail-case-group">
          <summary>{kind} ({entries.length})</summary>
          <DetailCaseList entries={entries} currency={currency} onOpenSource={onOpenSource} />
        </details>;
      }) : <p className="empty-copy">No hay movimientos evaluables en los documentos reconocidos.</p>)}
      {(tab === "depositos" || tab === "cheques") && <>
        {tab === "cheques" && <p className="difference-note">Los cobros se muestran para el cuadre. Los cheques pendientes incluyen el corte histórico reconocido y los candidatos de este período.</p>}
        {reportGroups.map((kind) => {
          const entries = selectedCases.filter((entry) => entry.kind === kind);
          return <section key={kind} className="detail-report-group"><h4>{kind} ({entries.length}){entries.length > 0 && ` · Total listado ${reportTotals(entries)}`}</h4>
            {entries.length ? <DetailCaseList entries={entries} currency={currency} onOpenSource={onOpenSource} /> : <p className="empty-copy">Sin casos identificados.</p>}
          </section>;
        })}
      </>}
    </section>
  </dialog>;
}

function ReconciliationObservations({ diagnostics, lastBankDate, reviewRequired, currency, canEdit = false, onResolveClosingBalance, onReplaceClosingFile, onConfirmObservation, result, selectedNumber, onReadSourceRow }: {
  diagnostics: Record<string, any>[];
  lastBankDate?: string;
  reviewRequired: boolean;
  currency?: string;
  canEdit?: boolean;
  onResolveClosingBalance?: Props["onResolveClosingBalance"];
  onReplaceClosingFile?: Props["onReplaceClosingFile"];
  onConfirmObservation?: Props["onConfirmObservation"];
  result: Props["result"];
  selectedNumber?: string;
  onReadSourceRow?: Props["onReadSourceRow"];
}) {
  const [manualOpen, setManualOpen] = useState(false);
  const [manualBalance, setManualBalance] = useState("");
  const [manualReason, setManualReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  if (!diagnostics.length && !reviewRequired) return null;
  const pendingCount = diagnostics.filter((item) => !item.resuelto && !item.revision_confirmada).length;
  const documents: { nombre: string; reconocimiento?: Record<string, any> }[] = result.archivos ?? [];
  const summary = result.resumen ?? {};
  const ledgerMovements = result.libros?.movimientos ?? [];
  const lastLedgerMovement = ledgerMovements[ledgerMovements.length - 1];
  const closingSources = [
    {
      label: summary.saldo_final_banco_fuente?.columna ?? "Saldo final detectado en Banco",
      evidence: summary.saldo_final_banco_fuente ?? result.banco?.reconocimiento?.contexto?.saldo_final_observado_evidencia,
    },
    {
      label: "Saldo final de Contabilidad",
      evidence: lastLedgerMovement && { ...lastLedgerMovement.datos_originales, fila: lastLedgerMovement.fila_origen },
    },
  ];

  const descriptions: Record<string, { title: string; explanation?: string; confirmLabel?: string }> = {
    exclusiones_requeridas: {
      title: "Filas fuera del período o del corte",
      explanation: "Estas filas quedaron fuera del análisis. Revisá sus fechas para comprobar si corresponden a este mes.",
      confirmLabel: "Confirmar exclusión",
    },
    cuenta_numero_incompatible: {
      title: "Verificá el número de cuenta",
    },
    errores_estado_bancario: {
      title: "Filas del banco que requieren revisión",
      explanation: "Algunas filas del estado de cuenta tienen datos que el sistema no pudo leer o validar.",
    },
    errores_mayor_contable: {
      title: "Filas de Contabilidad que requieren revisión",
      explanation: "Algunas filas del mayor tienen datos que el sistema no pudo leer o validar.",
    },
    cobertura_bancaria_incierta: {
      title: "Cierre bancario sin confirmar",
      explanation: lastBankDate
        ? `El saldo bancario mostrado corresponde al último Saldo Real del archivo (${lastBankDate}). Verificá que la descarga cubra todo el mes.`
        : "Verificá que el estado de cuenta cubra todo el mes.",
    },
  };

  return (
    <details className="reconciliation-observations">
      <summary>
        <span aria-hidden="true" className="observations-icon">i</span>
        {pendingCount ? `Observaciones (${pendingCount})` : "Observaciones"}
        {reviewRequired && <span className="observations-status">Revisión pendiente</span>}
        {!reviewRequired && <span className="observations-status observations-status-confirmed">Revisión registrada</span>}
      </summary>
      {diagnostics.length ? (
        <ul className="observations-list">
          {diagnostics.map((item, index) => {
            const description = descriptions[item.codigo];
            const namedSource = item.archivo ?? item.origen;
            const section = item.codigo === "errores_estado_bancario" ? result.banco
              : item.codigo === "errores_mayor_contable" ? result.libros : undefined;
            const sourceName = namedSource ?? section?.nombre;
            const document = documents.find((entry) => entry.nombre === sourceName)
              ?? documents.find((entry) => entry.nombre.replace(/\.xls[x]?$/i, "") === sourceName?.replace(/\.xls[x]?$/i, ""));
            const source = document?.nombre ?? sourceName;
            const sheet = document?.reconocimiento?.hoja ?? section?.reconocimiento?.hoja;
            const rowOffset = document?.reconocimiento?.encabezado_sintetico ? 1 : 0;
            const isClosing = item.codigo === "cobertura_bancaria_incierta";
            const resolved = isClosing && item.resuelto;
            const review = item.revision_confirmada;
            const rows: { fila: number; fecha?: string; motivo?: string; errores?: { mensaje?: string }[] }[] =
              Array.isArray(item.detalle) ? item.detalle.filter((row) => row?.fila != null) : [];
            return (
              <li key={index}>
                {description && <strong>{description.title}</strong>}
                {resolved ? <p className="observation-resolved">Saldo verificado</p> : <p>{description?.explanation || item.mensaje || "Revisá los documentos cargados."}</p>}
                {typeof source === "string" && source && <small>{source}</small>}
                {item.codigo === "cuenta_numero_incompatible" && (
                  <dl className="observation-account-numbers">
                    <div><dt>Cuenta seleccionada</dt><dd>{selectedNumber || "No disponible"}</dd></div>
                    {documents.map((entry) => {
                      const context = entry.reconocimiento?.contexto ?? {};
                      const detected = context.cuenta_bancaria ?? context.numero_cuenta ?? context.cuenta ?? context.terminacion;
                      if (!detected) return null;
                      const label = entry.reconocimiento?.origen_sugerido === "libros" ? "Número leído en Contabilidad" : "Número leído en Banco";
                      return <div key={entry.nombre}><dt>{label}</dt><dd>{String(detected)} <small>{entry.nombre}</small></dd></div>;
                    })}
                  </dl>
                )}
                {isClosing && (
                  <div className="observation-closing-sources">
                    {closingSources.map(({ label, evidence }) => (
                      evidence?.archivo && evidence.hoja && Number.isInteger(evidence.fila) && (
                        <SourceRowPreview key={label} source={evidence} label={label} onRead={onReadSourceRow} />
                      )
                    ))}
                  </div>
                )}
                {resolved && item.resolucion && <small className="observation-resolution">
                  Saldo confirmado: {formatMoney(item.resolucion.saldo_confirmado, currency)} · {formatLocalDate(item.resolucion.resuelto_en)}
                  {item.resolucion.usuario_nombre ? ` · ${item.resolucion.usuario_nombre}` : ""}
                </small>}
                {review && <small className="observation-resolution">
                  {description?.confirmLabel === "Confirmar exclusión" ? "Exclusión confirmada" : "Revisión confirmada"}
                  {review.usuario_nombre ? ` · ${review.usuario_nombre}` : ""} · {formatLocalDate(review.confirmado_en)}
                </small>}
                {isClosing && !resolved && canEdit && onResolveClosingBalance && (
                  <div className="closing-observation-actions">
                    <button type="button" className="secondary-button" disabled={submitting} onClick={() => {
                      setSubmitting(true);
                      void onResolveClosingBalance({ tipo: "confirmar" }).finally(() => setSubmitting(false));
                    }}>Confirmar saldo</button>
                    <button type="button" className="quiet-button" disabled={submitting} onClick={() => setManualOpen((value) => !value)}>Ingresar saldo manual</button>
                    <button type="button" className="quiet-button" disabled={submitting} onClick={() => onReplaceClosingFile?.(typeof source === "string" ? source : undefined)}>Reemplazar archivo</button>
                    {manualOpen && <form className="manual-closing-form" onSubmit={(event) => {
                      event.preventDefault();
                      setSubmitting(true);
                      void onResolveClosingBalance({ tipo: "manual", saldo: manualBalance, motivo: manualReason })
                        .then(() => { setManualOpen(false); setManualBalance(""); setManualReason(""); })
                        .finally(() => setSubmitting(false));
                    }}>
                      <label>Saldo correcto ({currency || "moneda de la cuenta"})
                        <input required inputMode="decimal" type="number" step="0.01" value={manualBalance} onChange={(event) => setManualBalance(event.target.value)} />
                      </label>
                      <label>Motivo obligatorio
                        <textarea required maxLength={1000} value={manualReason} onChange={(event) => setManualReason(event.target.value)} />
                      </label>
                      <button type="submit" className="primary-button" disabled={submitting}>Guardar saldo</button>
                    </form>}
                  </div>
                )}
                {rows.length > 0 && (
                  <details className="observations-rows">
                    <summary>Ver filas ({rows.length})</summary>
                    <ul>
                      {rows.map((row, rowIndex) => (
                        <li key={rowIndex}>
                          {(!source || !sheet) && <>Fila {row.fila}</>}
                          {row.fecha && <div>Fecha interpretada: {row.fecha}</div>}
                          {row.motivo === "fuera_periodo" && <div>Fuera del mes seleccionado</div>}
                          {row.motivo === "posterior_fecha_corte" && <div>Posterior a la fecha de corte</div>}
                          {Array.isArray(row.errores) && row.errores.map((error) => error.mensaje).filter(Boolean).map((message, messageIndex) => (
                            <div key={messageIndex}>{message?.includes("use date_format") ? "El sistema no pudo interpretar la fecha de esta fila." : message}</div>
                          ))}
                          {source && sheet && <SourceRowPreview key={`${source}:${sheet}:${row.fila}`}
                            source={{ archivo: source, hoja: sheet, fila: row.fila - rowOffset }} onRead={onReadSourceRow} />}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
                {description && !isClosing && !review && canEdit && onConfirmObservation && (
                  <div className="closing-observation-actions">
                    <button type="button" className="secondary-button" disabled={submitting} onClick={() => {
                      const confirmation = item.codigo === "exclusiones_requeridas"
                        ? "¿Confirmás que las filas indicadas deben quedar fuera de este análisis?"
                        : "¿Confirmás que revisaste esta observación? Si hay un dato incorrecto, aún debe corregirse en el documento o en la cuenta.";
                      if (!window.confirm(confirmation)) return;
                      setSubmitting(true);
                      void onConfirmObservation(index, item.codigo).finally(() => setSubmitting(false));
                    }}>{description.confirmLabel ?? "Confirmar revisión"}</button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      ) : <p>El análisis requiere revisar los documentos cargados.</p>}
    </details>
  );
}

export default function ReconciliationDashboard({
  result, job, companyName, countryCode, saved = false, onBackToHistory,
  onBackLabel = "Volver al historial",
  onSave, onDiscard, onDownloadExcel, onDownloadPdf, onPrint, onRecalculate, onOpenSource, saving = false,
  onOpenPreviousReconciliation, canEdit = false, onResolveClosingBalance, onReplaceClosingFile, onConfirmObservation, onReadSourceRow,
}: Props) {
  const [tab, setTab] = useState<"resumen" | "proceso">("resumen");
  const [detailTab, setDetailTab] = useState<DetailTab | null>(null);
  const resumen = result.resumen ?? {};
  const proceso = result.proceso_conciliacion ?? {};
  const contexto = result.contexto_resultado ?? {};
  const bankContext = result.banco?.reconocimiento?.contexto ?? {};
  const currency = contexto.moneda || contexto.moneda_codigo || (countryCode === "GT" ? "GTQ" : "USD");
  const diagnostics: Record<string, any>[] = Array.isArray(result.diagnosticos) ? result.diagnosticos : [];
  const selectedAccount = job?.cuenta_seleccionada ?? {};
  const coverageWarning = resumen.cobertura_periodo_bancario && resumen.cobertura_periodo_bancario !== "completa";
  const observations = coverageWarning && !diagnostics.some((item) => item.codigo === "cobertura_bancaria_incierta")
    ? [...diagnostics, { codigo: "cobertura_bancaria_incierta", archivo: result.banco?.nombre }]
    : diagnostics;
  const observationPanel = (
    <ReconciliationObservations
      key={job?.id}
      diagnostics={observations}
      result={result}
      selectedNumber={selectedAccount.numero_completo ?? selectedAccount.numero ?? contexto.cuenta_operativa}
      onReadSourceRow={onReadSourceRow}
      lastBankDate={bankContext.saldo_final_observado_evidencia?.fecha}
      reviewRequired={observations.length
        ? observations.some((item) => !item.resuelto && !item.revision_confirmada)
        : job?.estado === "requiere_aclaraciones"}
      currency={currency}
      canEdit={canEdit}
      onResolveClosingBalance={onResolveClosingBalance}
      onConfirmObservation={onConfirmObservation}
      onReplaceClosingFile={onReplaceClosingFile}
    />
  );
  const openingEvidence = resumen.saldo_inicial_banco_evidencia ?? {};
  const matches = proceso.coincidencias_confirmadas ?? [];
  const pendingBySide = tab === "proceso"
    ? (["banco", "contabilidad"] as const).map((side) => ({
        side,
        rows: processPendingRows(proceso, resumen.explicacion_diferencia ?? {}, side),
      }))
    : [];
  const period = formatPeriod(contexto.periodo || contexto.periodo_declarado || job?.periodo_sugerido);
  const bankName = selectedAccount.banco || contexto.banco || "Banco no disponible";
  const accountName = selectedAccount.alias
    ? [selectedAccount.alias, selectedAccount.numero_completo ?? selectedAccount.numero].filter(Boolean).join(" · ")
    : contexto.cuenta || contexto.numero_cuenta || "Cuenta no disponible";
  const heading = `${bankName} · ${accountName}`;
  const missingResult = !resumen || typeof resumen !== "object" || (!resumen.saldo_final_banco && !resumen.saldo_final_contabilidad && !proceso.coincidencias_confirmadas);

  return (
    <div className="reconciliation-dashboard">
      <div className="dashboard-heading">
        <div>
          <p className="eyebrow">{companyName}</p>
          <h2>{heading}</h2>
          <p className="dashboard-subtitle">{period} · {currency}</p>
        </div>
        {onBackToHistory && <button type="button" className="quiet-button" onClick={onBackToHistory}>{onBackLabel}</button>}
      </div>

      <nav className="dashboard-tabs" aria-label="Apartados de conciliación">
        {(["resumen", "proceso"] as const).map((item) => (
          <button key={item} type="button" className={tab === item ? "active" : ""} onClick={() => setTab(item)}>
            {item === "resumen" ? "Resumen" : "Proceso"}
          </button>
        ))}
      </nav>

      {missingResult ? (
        <div className="dashboard-missing">
          <h3>Resultado completo no disponible</h3>
          <p>Faltan los datos persistidos necesarios para mostrar el resultado completo.</p>
          {observationPanel}
          {onRecalculate && <button type="button" className="secondary-button" onClick={onRecalculate}>Recalcular conciliación</button>}
        </div>
      ) : (
        <>
          {tab === "resumen" && (
            <div className="dashboard-panel">
              <div className="summary-grid summary-primary">
                <article><span>Saldo final Banco</span><strong>{formatMoney(resumen.saldo_final_banco, currency)}</strong></article>
                <article><span>Saldo final Contabilidad</span><strong>{formatMoney(resumen.saldo_final_contabilidad, currency)}</strong></article>
                <article className="residual-card"><div className="residual-title"><span>Diferencia original</span><button type="button" className="detail-button" aria-haspopup="dialog" onClick={() => setDetailTab("resumen")}>Ver detalle</button></div><strong>{formatMoney(resumen.diferencia_original, currency)}</strong></article>
              </div>
              {resumen.cobertura_periodo_bancario && <section className="dashboard-section">
                <h3>Saldo inicial y cobertura bancaria</h3>
                <p><strong>Saldo inicial Banco:</strong> {formatMoney(resumen.saldo_inicial_banco, currency)}
                  {openingEvidence.fecha && <> · movimiento del {openingEvidence.fecha}</>}
                  {openingEvidence.archivo && <> · {openingEvidence.archivo}</>}
                  {openingEvidence.hoja && <> · {openingEvidence.hoja}</>}
                  {openingEvidence.fila && <> · fila {openingEvidence.fila}</>}
                </p>
              </section>}
              <div className="summary-grid summary-secondary">
                <article><span>Cruces confirmados</span><strong>{proceso.conteos?.cruces_confirmados ?? 0}</strong></article>
                <article><span>Banco consumido</span><strong>{proceso.conteos?.movimientos_banco_consumidos ?? 0}</strong></article>
                <article><span>Contabilidad consumida</span><strong>{proceso.conteos?.movimientos_contabilidad_consumidos ?? 0}</strong></article>
              </div>

              {observationPanel}
              <div className="responsible-line">
                <span><strong>Estado:</strong> {job?.estado === "finalizado_con_pendientes" ? "Guardada con pendientes" : job?.estado === "finalizado" ? "Guardada" : job?.estado === "aprobado" ? "Aprobada" : "Sin guardar"}</span>
                <span><strong>Responsable:</strong> {job?.responsable_nombre || (saved ? "No disponible" : "No disponible")}</span>
                <span><strong>Fecha:</strong> {job?.finalizado_en || job?.actualizado_en ? formatLocalDate(job.finalizado_en || job.actualizado_en) : "No disponible"}</span>
              </div>
            </div>
          )}

          {tab === "proceso" && (
            <div className="dashboard-panel process-table-wrap">
              <button type="button" className="detail-button process-evidence-button" aria-haspopup="dialog" onClick={() => setDetailTab("movimientos")}>Ver motivos y evidencia</button>
              <div className="process-table-scroll">
                <table className="dashboard-table"><thead><tr>
                  <th>Fecha Banco</th><th>Referencia</th><th>Descripción Banco</th><th className="money">Monto Banco</th>
                  <th>Fecha Contabilidad</th><th>Referencia Cont.</th><th>Descripción Contabilidad</th><th className="money">Monto Cont.</th><th className="money">Diferencia</th><th>Estado</th>
                </tr></thead><tbody>
                  {matches.map((match: Record<string, any>, index: number) => (
                    <tr key={`m${index}`}>
                      <td>{match.fecha_banco ?? "—"}</td><td>{match.referencia || ((match.referencias_alternativas ?? []).join(", ")) || "—"}</td><td>{match.descripcion_banco ?? "—"}</td><td className="money">{formatMoney(match.monto_banco, currency)}</td>
                      <td>{match.fecha_contabilidad ?? "—"}</td><td>{((match.referencias_alternativas ?? []).join(", ")) || match.referencia || "—"}</td><td>{match.descripcion_contabilidad ?? "—"}</td><td className="money">{formatMoney(match.monto_contabilidad, currency)}</td><td className="money">{formatMoney(match.diferencia ?? "0", currency)}</td>
                      <td><ProcessStatus status="Conciliado" /></td>
                    </tr>
                  ))}
                  {pendingBySide.flatMap(({ side, rows }) => rows.map(({ id, movement, status }) => {
                    const cells = <>
                      <td>{movement.fecha ?? "—"}</td>
                      <td>{movement.referencia || (movement.referencias_alternativas ?? []).join(", ") || movement.numero_transaccion || movement.numero_cheque || "—"}</td>
                      <td>{movement.descripcion ?? "—"}</td>
                      <td className="money">{formatMoney(movement.monto, currency)}</td>
                    </>;
                    const emptyCells = <><td>—</td><td>—</td><td>—</td><td>—</td></>;
                    return <tr key={`${side}-${id}`} className={status === "Cheque cobrado" ? undefined : "exception-row"}>
                      {side === "banco" ? cells : emptyCells}
                      {side === "contabilidad" ? cells : emptyCells}
                      <td>—</td><td><ProcessStatus status={status} /></td>
                    </tr>;
                  }))}
                  {!matches.length && pendingBySide.every(({ rows }) => rows.length === 0) && <tr><td colSpan={10} className="empty-copy">No hay movimientos evaluables con los documentos reconocidos.</td></tr>}
                </tbody></table>
              </div>
            </div>
          )}

        </>
      )}

      {detailTab && (
        <ReconciliationDetail
          result={result} currency={currency} countryCode={countryCode}
          context={`${companyName} · ${heading} · ${period}`}
          initialTab={detailTab} onClose={() => setDetailTab(null)} onPrint={onPrint} onOpenSource={onOpenSource}
          onOpenPrevious={onOpenPreviousReconciliation && ((id) =>
            onOpenPreviousReconciliation(id, String(job?.cuenta_id ?? selectedAccount.id ?? "")))}
        />
      )}

      <div className="dashboard-actions" aria-label="Acciones de conciliación">
        {!saved && onSave && <button type="button" className="primary-button" disabled={saving} onClick={onSave}>{saving ? "Guardando…" : "Guardar conciliación"}</button>}
        {onDownloadExcel && <button type="button" className="secondary-button" onClick={onDownloadExcel}>Descargar Excel</button>}
        {onDownloadPdf && <button type="button" className="secondary-button" onClick={onDownloadPdf}>Descargar resumen PDF</button>}
        {onPrint && <button type="button" className="quiet-button" onClick={onPrint}>Imprimir</button>}
        {onRecalculate && <button type="button" className="quiet-button" onClick={onRecalculate}>Recalcular</button>}
        {!saved && onDiscard && <button type="button" className="danger-button" onClick={onDiscard}>Descartar análisis</button>}
      </div>
    </div>
  );
}
