import { useState } from "react";

type Props = {
  result: Record<string, any>;
  job?: Record<string, any>;
  companyName: string;
  countryCode: "GT" | "SV";
  saved?: boolean;
  onBackToHistory?: () => void;
  onSave?: () => void;
  onDiscard?: () => void;
  onDownloadExcel?: () => void;
  onDownloadPdf?: () => void;
  onPrint?: () => void;
  onRecalculate?: () => void;
};

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

function Evidence({ evidence, movement }: { evidence?: Record<string, any>; movement?: Record<string, any> }) {
  const source = evidence ?? movement?.datos_originales ?? {};
  return (
    <details className="evidence-detail">
      <summary>Ver evidencia</summary>
      <dl>
        <div><dt>Archivo</dt><dd>{source.archivo ?? "No disponible"}</dd></div>
        <div><dt>Hoja</dt><dd>{source.hoja ?? "No disponible"}</dd></div>
        <div><dt>Fila</dt><dd>{source.fila ?? movement?.fila_origen ?? "No disponible"}</dd></div>
      </dl>
    </details>
  );
}

export default function ReconciliationDashboard({
  result, job, companyName, countryCode, saved = false, onBackToHistory,
  onSave, onDiscard, onDownloadExcel, onDownloadPdf, onPrint, onRecalculate,
}: Props) {
  const [tab, setTab] = useState<"resumen" | "proceso" | "depositos" | "cheques">("resumen");
  const resumen = result.resumen ?? {};
  const proceso = result.proceso_conciliacion ?? {};
  const depositos = result.depositos ?? {};
  const cheques = result.cheques ?? [];
  const contexto = result.contexto_resultado ?? {};
  const matches = proceso.coincidencias_confirmadas ?? [];
  const bankUnmatched = proceso.movimientos_sin_contraparte?.banco ?? [];
  const booksUnmatched = proceso.movimientos_sin_contraparte?.contabilidad ?? [];
  const currency = contexto.moneda || contexto.moneda_codigo || (countryCode === "GT" ? "GTQ" : "USD");
  const period = formatPeriod(contexto.periodo || contexto.periodo_declarado || job?.periodo_sugerido);
  const missingResult = !resumen || typeof resumen !== "object" || (!resumen.saldo_final_banco && !resumen.saldo_final_contabilidad && !proceso.coincidencias_confirmadas);

  return (
    <div className="reconciliation-dashboard">
      <div className="dashboard-heading">
        <div>
          <p className="eyebrow">{companyName}</p>
          <h2>{contexto.banco || "Banco no disponible"} · {contexto.cuenta || contexto.numero_cuenta || "Cuenta no disponible"}</h2>
          <p className="dashboard-subtitle">{period} · {currency}</p>
        </div>
        {onBackToHistory && <button type="button" className="quiet-button" onClick={onBackToHistory}>Volver al historial</button>}
      </div>

      <nav className="dashboard-tabs" aria-label="Apartados de conciliación">
        {(["resumen", "proceso", "depositos", "cheques"] as const).map((item) => (
          <button key={item} type="button" className={tab === item ? "active" : ""} onClick={() => setTab(item)}>
            {item === "resumen" ? "Resumen" : item === "proceso" ? "Proceso" : item === "depositos" ? "Depósitos" : "Cheques"}
          </button>
        ))}
      </nav>

      {missingResult ? (
        <div className="dashboard-missing">
          <h3>Resultado completo no disponible</h3>
          <p>Faltan los datos persistidos necesarios para reconstruir las cuatro pestañas. No se utilizará una lista vacía como sustituto.</p>
          {onRecalculate && <button type="button" className="secondary-button" onClick={onRecalculate}>Recalcular conciliación</button>}
        </div>
      ) : (
        <>
          {tab === "resumen" && (
            <div className="dashboard-panel">
              <div className="summary-grid summary-primary">
                <article><span>Saldo final Banco</span><strong>{formatMoney(resumen.saldo_final_banco, currency)}</strong></article>
                <article><span>Saldo final Contabilidad</span><strong>{formatMoney(resumen.saldo_final_contabilidad, currency)}</strong></article>
                <article className="residual-card"><span>Diferencia residual</span><strong>{formatMoney(resumen.diferencia_residual_pendiente, currency)}</strong></article>
              </div>
              <div className="summary-grid summary-secondary">
                <article><span>Diferencia original</span><strong>{formatMoney(resumen.diferencia_original, currency)}</strong></article>
                <article><span>Cruces confirmados</span><strong>{proceso.conteos?.cruces_confirmados ?? 0}</strong></article>
                <article><span>Banco consumido</span><strong>{proceso.conteos?.movimientos_banco_consumidos ?? 0}</strong></article>
                <article><span>Contabilidad consumida</span><strong>{proceso.conteos?.movimientos_contabilidad_consumidos ?? 0}</strong></article>
              </div>

              <section className="dashboard-section">
                <div className="section-heading"><h3>Partidas conciliatorias explicativas</h3><span>{(resumen.partidas_conciliatorias ?? []).length} partidas</span></div>
                {(resumen.partidas_conciliatorias ?? []).length > 0 ? (
                  <div className="compact-table-scroll"><table className="compact-table"><thead><tr><th>Concepto</th><th className="money">Importe</th><th>Lado</th><th>Signo</th></tr></thead><tbody>
                    {(resumen.partidas_conciliatorias ?? []).map((partida: Record<string, any>, index: number) => (
                      <tr key={index}><td>{partida.concepto ?? partida.tipo ?? "Partida"}</td><td className="money">{formatMoney(partida.importe, currency)}</td><td>{partida.lado ?? "No disponible"}</td><td>{partida.signo ?? "No disponible"}</td></tr>
                    ))}
                  </tbody></table></div>
                ) : <p className="empty-copy">No hay partidas explicativas verificadas.</p>}
              </section>

              {(result.diagnosticos ?? []).length > 0 && <div className="diagnostics"><h3>Observaciones</h3>{result.diagnosticos.map((item: Record<string, any>, index: number) => <p key={index}><strong>{item.codigo}</strong>: {item.mensaje}</p>)}</div>}
              <div className="responsible-line">
                <span><strong>Estado:</strong> {job?.estado === "finalizado_con_pendientes" ? "Guardada con pendientes" : job?.estado === "finalizado" ? "Guardada" : job?.estado === "aprobado" ? "Aprobada" : "Sin guardar"}</span>
                <span><strong>Responsable:</strong> {job?.responsable_nombre || (saved ? "No disponible" : "No disponible")}</span>
                <span><strong>Fecha:</strong> {job?.finalizado_en || job?.actualizado_en ? formatLocalDate(job.finalizado_en || job.actualizado_en) : "No disponible"}</span>
              </div>
            </div>
          )}

          {tab === "proceso" && (
            <div className="dashboard-panel process-table-wrap">
              <div className="process-table-scroll">
                <table className="dashboard-table"><thead><tr>
                  <th>Fecha Banco</th><th>Referencia</th><th>Descripción Banco</th><th className="money">Monto Banco</th>
                  <th>Fecha Contabilidad</th><th>Referencia Cont.</th><th>Descripción Contabilidad</th><th className="money">Monto Cont.</th><th className="money">Diferencia</th><th>Detalle</th>
                </tr></thead><tbody>
                  {matches.map((match: Record<string, any>, index: number) => (
                    <tr key={`m${index}`}>
                      <td>{match.fecha_banco ?? "—"}</td><td>{match.referencia || ((match.referencias_alternativas ?? []).join(", ")) || "—"}</td><td>{match.descripcion_banco ?? "—"}</td><td className="money">{formatMoney(match.monto_banco, currency)}</td>
                      <td>{match.fecha_contabilidad ?? "—"}</td><td>{((match.referencias_alternativas ?? []).join(", ")) || match.referencia || "—"}</td><td>{match.descripcion_contabilidad ?? "—"}</td><td className="money">{formatMoney(match.monto_contabilidad, currency)}</td><td className="money">{formatMoney(match.diferencia ?? "0", currency)}</td>
                      <td><details><summary>Ver detalle</summary><dl><dt>Criterio</dt><dd>{match.criterio ?? "No disponible"}</dd><dt>Resultado</dt><dd>{match.resultado ?? "No disponible"}</dd></dl></details></td>
                    </tr>
                  ))}
                  {bankUnmatched.map((item: Record<string, any>, index: number) => {
                    const movement = item.movimiento ?? {};
                    return <tr key={`b${index}`} className="exception-row"><td>{movement.fecha ?? "—"}</td><td>{movement.referencia ?? movement.numero_transaccion ?? movement.numero_cheque ?? "—"}</td><td>{movement.descripcion ?? "—"}</td><td className="money">{formatMoney(movement.monto, currency)}</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td><details><summary>Ver detalle</summary><p>{item.situacion ?? "Requiere revisión"}</p><Evidence evidence={item.evidencia} movement={movement} /></details></td></tr>;
                  })}
                  {booksUnmatched.map((item: Record<string, any>, index: number) => {
                    const movement = item.movimiento ?? item ?? {};
                    return <tr key={`c${index}`} className="exception-row"><td>—</td><td>—</td><td>—</td><td>—</td><td>{movement.fecha ?? "—"}</td><td>{movement.referencia || (((movement.referencias_alternativas ?? []).join(", ")) || "—")}</td><td>{movement.descripcion ?? "—"}</td><td className="money">{formatMoney(movement.monto, currency)}</td><td>—</td><td><details><summary>Ver detalle</summary><p>{item.situacion ?? "Requiere revisión"}</p><Evidence evidence={item.evidencia} movement={movement} /></details></td></tr>;
                  })}
                  {!matches.length && !bankUnmatched.length && !booksUnmatched.length && <tr><td colSpan={10} className="empty-copy">No hay movimientos evaluables con los documentos reconocidos.</td></tr>}
                </tbody></table>
              </div>
            </div>
          )}

          {tab === "depositos" && (
            <div className="dashboard-panel process-table-wrap"><div className="process-table-scroll"><table className="dashboard-table"><thead><tr><th>Fecha</th><th>Referencia</th><th>Descripción</th><th className="money">Importe</th><th>Situación</th><th>Evidencia</th></tr></thead><tbody>
              {(depositos.sin_contraparte ?? []).map((item: Record<string, any>, index: number) => {
                const movement = item.movimiento ?? {};
                return <tr key={index}><td>{movement.fecha ?? "—"}</td><td>{movement.referencia ?? movement.numero_transaccion ?? "—"}</td><td>{movement.descripcion ?? "—"}</td><td className="money">{formatMoney(movement.monto, currency)}</td><td>{item.situacion ?? "Por revisar"}</td><td><Evidence evidence={item.evidencia} movement={movement} /></td></tr>;
              })}
              {(depositos.sin_contraparte ?? []).length === 0 && <tr><td colSpan={6} className="empty-copy">No hay casos clasificados en Depósitos.</td></tr>}
            </tbody></table></div></div>
          )}

          {tab === "cheques" && (
            <div className="dashboard-panel">
              {(cheques as Record<string, any>[]).map((cut, cutIndex) => (
                <section key={cutIndex} className="cheque-cut"><h3>Corte {formatPeriod(cut.periodo_corte)}</h3>
                  <div className="process-table-scroll"><table className="dashboard-table"><thead><tr><th>Número</th><th>Fecha emisión</th><th>Beneficiario / descripción</th><th className="money">Importe</th><th>Estado al corte</th><th>Evidencia</th></tr></thead><tbody>
                    {(cut.registros ?? []).map((record: Record<string, any>, index: number) => {
                      const cobro = record.cobro_identificado ?? [];
                      const state = record.estado_al_corte === "cobrado_identificado_en_banco" ? `Cobrado identificado${cobro.length ? ` · ${cobro[0].fecha}` : ""}` : record.estado_al_corte === "pendiente_identificacion" ? "Pendiente de identificación" : "Pendiente al corte actual";
                      return <tr key={index}><td>{record.numero_cero ? "0 / identificar" : record.numero_documento ?? "No disponible"}</td><td>{record.fecha ?? "No disponible"}</td><td>{record.descripcion ?? "No disponible"}</td><td className="money">{formatMoney(record.monto, currency)}</td><td><span className={record.estado_al_corte === "cobrado_identificado_en_banco" ? "status-ready" : "status-muted"}>{state}</span></td><td><Evidence evidence={{ archivo: record.archivo ?? cut.nombre, hoja: record.hoja, fila: record.fila }} movement={record} /></td></tr>;
                    })}
                  </tbody></table></div>
                </section>
              ))}
              {!cheques.length && <p className="empty-copy">No se recibió ni reconoció un corte histórico de cheques.</p>}
            </div>
          )}
        </>
      )}

      <div className="dashboard-actions" aria-label="Acciones de conciliación">
        {!saved && onSave && <button type="button" className="primary-button" onClick={onSave}>Guardar conciliación</button>}
        {onDownloadExcel && <button type="button" className="secondary-button" onClick={onDownloadExcel}>Descargar Excel</button>}
        {onDownloadPdf && <button type="button" className="secondary-button" onClick={onDownloadPdf}>Descargar resumen PDF</button>}
        {onPrint && <button type="button" className="quiet-button" onClick={onPrint}>Imprimir</button>}
        {onRecalculate && <button type="button" className="quiet-button" onClick={onRecalculate}>Recalcular</button>}
        {!saved && onDiscard && <button type="button" className="danger-button" onClick={onDiscard}>Descartar análisis</button>}
      </div>
    </div>
  );
}
