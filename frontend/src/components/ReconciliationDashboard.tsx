import { useState } from "react";

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
  onOpenPreviousReconciliation?: (jobId: string, accountId: string) => void;
  onOpenSource?: (filename: string) => void;
  saving?: boolean;
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

function Evidence({ evidence, movement, onOpenSource, label = "Ver evidencia" }: { evidence?: Record<string, any>; movement?: Record<string, any>; onOpenSource?: (filename: string) => void; label?: string }) {
  const source = evidence ?? movement?.datos_originales ?? {};
  return (
    <details className="evidence-detail">
      <summary>{label}</summary>
      <dl>
        <div><dt>Archivo</dt><dd>{source.archivo ?? "No disponible"}</dd></div>
        <div><dt>Hoja</dt><dd>{source.hoja ?? "No disponible"}</dd></div>
        <div><dt>Fila</dt><dd>{source.fila ?? movement?.fila_origen ?? "No disponible"}</dd></div>
      </dl>
      {source.archivo && onOpenSource && <button type="button" className="quiet-button source-button" onClick={() => onOpenSource(String(source.archivo))}>Abrir documento de origen</button>}
    </details>
  );
}

export default function ReconciliationDashboard({
  result, job, companyName, countryCode, saved = false, onBackToHistory,
  onBackLabel = "Volver al historial",
  onSave, onDiscard, onDownloadExcel, onDownloadPdf, onPrint, onRecalculate, onOpenSource, saving = false,
  onOpenPreviousReconciliation,
}: Props) {
  const [tab, setTab] = useState<"resumen" | "proceso" | "depositos" | "cheques">("resumen");
  const [differenceOpen, setDifferenceOpen] = useState(false);
  const resumen = result.resumen ?? {};
  const proceso = result.proceso_conciliacion ?? {};
  const depositos = result.depositos ?? {};
  const cheques = result.cheques ?? [];
  const contexto = result.contexto_resultado ?? {};
  const bankContext = result.banco?.reconocimiento?.contexto ?? {};
  const openingEvidence = resumen.saldo_inicial_banco_evidencia ?? {};
  const selectedAccount = job?.cuenta_seleccionada ?? {};
  const difference = resumen.explicacion_diferencia ?? {};
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
          <h2>{selectedAccount.banco || contexto.banco || "Banco no disponible"} · {selectedAccount.alias ? `${selectedAccount.alias} · ${selectedAccount.numero_completo ?? selectedAccount.numero ?? "Cuenta no disponible"}` : contexto.cuenta || contexto.numero_cuenta || "Cuenta no disponible"}</h2>
          <p className="dashboard-subtitle">{period} · {currency}</p>
        </div>
        {onBackToHistory && <button type="button" className="quiet-button" onClick={onBackToHistory}>{onBackLabel}</button>}
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
                <article className="residual-card"><div className="residual-title"><span>Diferencia residual</span><button type="button" className="detail-button" onClick={() => setDifferenceOpen(true)}>Ver detalle</button></div><strong>{formatMoney(resumen.diferencia_residual_pendiente, currency)}</strong></article>
              </div>
              {resumen.cobertura_periodo_bancario && <section className="dashboard-section">
                <h3>Saldo inicial y cobertura bancaria</h3>
                <p><strong>Saldo inicial Banco:</strong> {formatMoney(resumen.saldo_inicial_banco, currency)}
                  {openingEvidence.fecha && <> · movimiento del {openingEvidence.fecha}</>}
                  {openingEvidence.archivo && <> · {openingEvidence.archivo}</>}
                  {openingEvidence.hoja && <> · {openingEvidence.hoja}</>}
                  {openingEvidence.fila && <> · fila {openingEvidence.fila}</>}
                </p>
                {resumen.cobertura_periodo_bancario !== "completa" && <p className="difference-note">
                  Cobertura del período incierta. El saldo final es el último Saldo Real observado
                  {bankContext.saldo_final_observado_evidencia?.fecha ? ` (${bankContext.saldo_final_observado_evidencia.fecha})` : ""}.
                </p>}
              </section>}
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

              {(result.diagnosticos ?? []).length > 0 && <div className="diagnostics"><h3>Observaciones</h3>{result.diagnosticos.map((item: Record<string, any>, index: number) => <p key={index}><strong>{item.codigo}</strong>{(item.archivo ?? item.origen) && <> · {item.archivo ?? item.origen}</>}: {item.mensaje}</p>)}</div>}
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
                    return <tr key={`b${index}`} className="exception-row"><td>{movement.fecha ?? "—"}</td><td>{movement.referencia ?? movement.numero_transaccion ?? movement.numero_cheque ?? "—"}</td><td>{movement.descripcion ?? "—"}</td><td className="money">{formatMoney(movement.monto, currency)}</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td><details><summary>Ver detalle</summary><p>{item.situacion ?? "Requiere revisión"}</p><Evidence evidence={item.evidencia} movement={movement} onOpenSource={onOpenSource} /></details></td></tr>;
                  })}
                  {booksUnmatched.map((item: Record<string, any>, index: number) => {
                    const movement = item.movimiento ?? item ?? {};
                    return <tr key={`c${index}`} className="exception-row"><td>—</td><td>—</td><td>—</td><td>—</td><td>{movement.fecha ?? "—"}</td><td>{movement.referencia || (((movement.referencias_alternativas ?? []).join(", ")) || "—")}</td><td>{movement.descripcion ?? "—"}</td><td className="money">{formatMoney(movement.monto, currency)}</td><td>—</td><td><details><summary>Ver detalle</summary><p>{item.situacion ?? "Requiere revisión"}</p><Evidence evidence={item.evidencia} movement={movement} onOpenSource={onOpenSource} /></details></td></tr>;
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
                return <tr key={index}><td>{movement.fecha ?? "—"}</td><td>{movement.referencia ?? movement.numero_transaccion ?? "—"}</td><td>{movement.descripcion ?? "—"}</td><td className="money">{formatMoney(movement.monto, currency)}</td><td>{item.situacion ?? "Por revisar"}</td><td><Evidence evidence={item.evidencia} movement={movement} onOpenSource={onOpenSource} /></td></tr>;
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
                      const historicalEvidence = { archivo: record.archivo ?? cut.nombre, hoja: record.hoja, fila: record.fila };
                      const state = record.estado_al_corte === "cobrado_identificado_en_banco"
                        ? `Cobrado · ${cobro[0]?.tipo_movimiento ?? "CQ"} ${cobro[0]?.referencia ?? record.numero_documento} · ${cobro[0]?.fecha ?? "fecha no disponible"}`
                        : record.estado_al_corte === "pendiente_identificacion" ? "Pendiente de identificación" : "Pendiente al corte actual";
                      return <tr key={index}><td>{record.numero_cero ? "0 / identificar" : record.numero_documento ?? "No disponible"}</td><td>{record.fecha ?? "No disponible"}</td><td>{record.descripcion ?? "No disponible"}</td><td className="money">{formatMoney(record.monto, currency)}</td><td><span className={record.estado_al_corte === "cobrado_identificado_en_banco" ? "status-ready" : "status-muted"}>{state}</span></td><td><Evidence evidence={historicalEvidence} movement={record} onOpenSource={onOpenSource} label="Corte histórico" />{cobro.length > 0 && <Evidence evidence={cobro[0].evidencia} onOpenSource={onOpenSource} label="Evidencia bancaria" />}</td></tr>;
                    })}
                  </tbody></table></div>
                </section>
              ))}
              {!cheques.length && <p className="empty-copy">No se recibió ni reconoció un corte histórico de cheques.</p>}
            </div>
          )}
        </>
      )}

      {differenceOpen && <div className="difference-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setDifferenceOpen(false); }} onKeyDown={(event) => { if (event.key === "Escape") setDifferenceOpen(false); }}>
        <section className="difference-modal" role="dialog" aria-modal="true" aria-labelledby="difference-modal-title">
          <header><div><p className="eyebrow">DESGLOSE DE SALDOS</p><h2 id="difference-modal-title">Explicación de la diferencia</h2></div><button type="button" className="quiet-button" aria-label="Cerrar detalle" onClick={() => setDifferenceOpen(false)}>Cerrar</button></header>

          <section className="difference-section">
            <h3>A. Diferencia original</h3>
            <div className="difference-equation">
              <span>Saldo Banco <strong>{formatMoney(difference.diferencia_original?.saldo_banco, currency)}</strong></span>
              <span>−</span>
              <span>Saldo Contabilidad <strong>{formatMoney(difference.diferencia_original?.saldo_contabilidad, currency)}</strong></span>
              <span>=</span>
              <span>Diferencia original <strong>{formatMoney(difference.diferencia_original?.importe, currency)}</strong></span>
            </div>
            <p>{difference.diferencia_original?.direccion === "banco_mayor"
              ? `El saldo del banco es ${formatMoney(difference.diferencia_original?.importe_absoluto, currency)} mayor que el de Contabilidad.`
              : difference.diferencia_original?.direccion === "banco_menor"
                ? `El saldo del banco es ${formatMoney(difference.diferencia_original?.importe_absoluto, currency)} menor que el de Contabilidad.`
                : difference.diferencia_original?.direccion === "iguales"
                  ? "Los saldos del banco y de Contabilidad son iguales."
                  : "No se pudo determinar la diferencia original."}</p>
          </section>

          <section className="difference-section">
            <h3>B. Diferencia que viene del mes anterior</h3>
            {difference.diferencia_inicial?.importe !== null && difference.diferencia_inicial?.importe !== undefined ? <>
              <div className="difference-equation">
                <span>Saldo inicial Banco <strong>{formatMoney(difference.diferencia_inicial.saldo_banco, currency)}</strong></span>
                <span>−</span>
                <span>Saldo inicial Contabilidad <strong>{formatMoney(difference.diferencia_inicial.saldo_contabilidad, currency)}</strong></span>
                <span>=</span>
                <span>Diferencia inicial <strong>{formatMoney(difference.diferencia_inicial.importe, currency)}</strong></span>
              </div>
              <div className="difference-opening-evidence">
                <div><strong>Banco</strong><Evidence evidence={difference.diferencia_inicial.evidencia_banco} onOpenSource={onOpenSource} label="Evidencia del saldo inicial" />{difference.diferencia_inicial.evidencia_banco?.fecha && <span>Fecha: {difference.diferencia_inicial.evidencia_banco.fecha}</span>}</div>
                <div><strong>Contabilidad</strong><Evidence evidence={difference.diferencia_inicial.evidencia_contabilidad} onOpenSource={onOpenSource} label="Evidencia del saldo inicial" />{difference.diferencia_inicial.evidencia_contabilidad?.fecha && <span>Fecha: {difference.diferencia_inicial.evidencia_contabilidad.fecha}</span>}</div>
              </div>
              {difference.conciliacion_anterior?.id
                ? <button type="button" className="secondary-button" onClick={() => onOpenPreviousReconciliation?.(String(difference.conciliacion_anterior.id), String(job?.cuenta_id ?? selectedAccount.id ?? ""))}>Ver conciliación anterior</button>
                : <p className="difference-note">{difference.diferencia_inicial.origen_pendiente ?? "Origen pendiente de revisar en el cierre anterior"}</p>}
            </> : <p className="empty-copy">No hay evidencia de ambos saldos iniciales para calcular la diferencia del mes anterior.</p>}
          </section>

          {difference.desglose_diferencia?.coincide_con_diferencia_original && <section className="difference-section">
            <h3>C. Desglose de la diferencia</h3>
            <div className="difference-equation">
              <span>Diferencia inicial <strong>{formatMoney(difference.desglose_diferencia.diferencia_inicial, currency)}</strong></span>
              <span>+</span>
              <span>Diferencia neta de movimientos <strong>{formatMoney(difference.desglose_diferencia.diferencia_neta_movimientos, currency)}</strong></span>
              <span>=</span>
              <span>Diferencia final <strong>{formatMoney(difference.desglose_diferencia.total, currency)}</strong></span>
            </div>
          </section>}

          <section className="difference-section">
            <h3>D. Partidas identificadas</h3>
            {(difference.partidas_explicativas ?? []).length ? <div className="difference-table-scroll">
              <table className="difference-table"><thead><tr><th>Concepto</th><th>Importe con signo</th><th>Lado</th><th>Motivo y evidencia</th><th>Aplicada al residual</th></tr></thead><tbody>
                {(difference.partidas_explicativas ?? []).map((item: Record<string, any>, index: number) => (
                  <tr key={`${item.id ?? item.concepto}-${index}`}><td>{item.concepto}</td><td className="money">{formatMoney(item.importe_con_signo, currency)}</td><td>{item.lado}</td><td>{item.motivo}{item.evidencia?.banco && <Evidence evidence={item.evidencia.banco} onOpenSource={onOpenSource} label="Evidencia bancaria" />}{item.evidencia?.historico && <Evidence evidence={item.evidencia.historico} onOpenSource={onOpenSource} label="Corte histórico" />}</td><td>{item.afecta_residual ? "Sí" : "No"}</td></tr>
                ))}
              </tbody></table>
            </div> : <p className="empty-copy">No hay partidas identificadas con evidencia suficiente.</p>}
          </section>

          <section className="difference-section">
            <h3>E. Paso al residual</h3>
            <p>{formatMoney(difference.diferencia_original?.importe, currency)} + {formatMoney(difference.residual?.impacto_total_aplicado, currency)} = {formatMoney(difference.residual?.importe, currency)}</p>
            {difference.residual?.partidas_aplicadas?.length ? <div className="difference-table-scroll"><table className="difference-table"><thead><tr><th>Concepto</th><th>Importe con signo</th><th>Lado</th><th>Motivo y evidencia</th><th>Residual</th></tr></thead><tbody>
              {difference.residual.partidas_aplicadas.map((item: Record<string, any>, index: number) => {
                return <tr key={`${item.id ?? item.concepto}-${index}`}><td>{item.concepto}</td><td className="money">{formatMoney(item.importe_con_signo, currency)}</td><td>{item.lado}</td><td>{item.motivo}{item.evidencia?.banco && <Evidence evidence={item.evidencia.banco} onOpenSource={onOpenSource} label="Evidencia bancaria" />}{item.evidencia?.historico && <Evidence evidence={item.evidencia.historico} onOpenSource={onOpenSource} label="Corte histórico" />}</td><td>{item.afecta_residual ? "Sí" : "No"}</td></tr>;
              })}
            </tbody></table></div> : <p>{difference.residual?.mensaje_sin_partidas ?? "No hay partidas explicativas aplicadas."}</p>}
          </section>

          <section className="difference-section">
            <h3>F. Movimientos pendientes y candidatos</h3>
            {(["banco", "contabilidad"] as const).map((side) => {
              const label = side === "banco" ? "Banco" : "Contabilidad";
              const sideData = difference.pendientes?.[side] ?? {};
              return <div key={side} className="difference-pending-side">
                <h4>Pendientes de {label} · Neto {formatMoney(sideData.neto, currency)}</h4>
                {sideData.grupos_fecha?.length ? <div className="difference-date-groups">
                  {sideData.grupos_fecha.map((group: Record<string, any>) => <details key={`${side}-${group.fecha}`} className="difference-date-group">
                    <summary><span>{group.fecha}</span><strong>Neto {formatMoney(group.neto, currency)}</strong><span>{group.cantidad ? `Ver filas (${group.cantidad})` : `Ver reversión abajo (${group.cantidad_reversiones})`}{group.cantidad && group.cantidad_reversiones ? ` · ${group.cantidad_reversiones} filas en reversión abajo` : ""}</span></summary>
                    {group.movimientos?.length ? <div className="difference-table-scroll"><table className="difference-table"><thead><tr><th>Referencia</th><th>Descripción</th><th>Importe</th><th>Estado al cierre</th><th>Período posterior</th><th>Evidencia</th></tr></thead><tbody>
                      {group.movimientos.map((item: Record<string, any>) => <tr key={item.id}>
                        <td>{item.referencia ?? "—"}</td><td>{item.descripcion ?? "—"}</td><td className="money">{formatMoney(item.importe_con_signo, currency)}</td>
                        <td>{item.estado_al_cierre ?? "Pendiente al cierre"}</td>
                        <td>{item.reflejado_en_periodo_posterior
                          ? <>{item.reflejado_en_periodo_posterior.estado} · {item.reflejado_en_periodo_posterior.fecha}<Evidence evidence={item.reflejado_en_periodo_posterior.evidencia} onOpenSource={onOpenSource} label="Evidencia posterior" /></>
                          : "Sin evidencia posterior"}</td>
                        <td>{item.id}<Evidence evidence={item.evidencia} onOpenSource={onOpenSource} /></td>
                      </tr>)}
                    </tbody></table></div> : <p className="empty-copy">Las filas de esta fecha forman parte de una reversión identificada abajo.</p>}
                  </details>)}
                </div> : <p className="empty-copy">No hay movimientos pendientes de este lado.</p>}
                {(sideData.reversiones_identificadas ?? []).length > 0 && <div className="difference-reversals">
                  <h4>Reversiones identificadas</h4>
                  {sideData.reversiones_identificadas.map((group: Record<string, any>) => <details key={group.id} className="difference-date-group">
                    <summary><span>Referencia {group.referencia}</span><strong>Neto {formatMoney(group.neto, currency)}</strong><span>Ver movimientos ({group.movimientos?.length ?? 0})</span></summary>
                    <p>{group.motivo}</p>
                    <div className="difference-table-scroll"><table className="difference-table"><thead><tr><th>Fecha</th><th>Referencia</th><th>Descripción</th><th>Importe</th><th>Origen</th></tr></thead><tbody>
                      {group.movimientos.map((item: Record<string, any>) => <tr key={item.id}><td>{item.fecha ?? "—"}</td><td>{item.referencia ?? "—"}</td><td>{item.descripcion ?? "—"}</td><td className="money">{formatMoney(item.importe_con_signo, currency)}</td><td>{item.id}<Evidence evidence={item.evidencia} onOpenSource={onOpenSource} /></td></tr>)}
                    </tbody></table></div>
                  </details>)}
                </div>}
              </div>;
            })}
            <p className="difference-note">Diferencia entre netos pendientes: {formatMoney(difference.pendientes?.diferencia_neta_movimientos, currency)}. No se aplica automáticamente ni sustituye la comparación de saldos.</p>
            {(difference.candidatos ?? []).length > 0 && <div className="difference-candidates">
              <h4>Agrupaciones candidatas sin confirmar</h4>
              {(difference.candidatos ?? []).map((candidate: Record<string, any>, index: number) => <article key={`${candidate.tipo}-${index}`}>
                <strong>{candidate.criterio ?? candidate.tipo}</strong><p>{candidate.explicacion ?? "Requiere revisión; no se atribuye como causa."}</p>
                {(["banco", "contabilidad"] as const).map((side) => <ul key={side}>{(candidate[side] ?? []).map((item: Record<string, any>) => <li key={item.id}>{side === "banco" ? "Banco" : "Contabilidad"} · {item.movimiento?.fecha ?? "—"} · {item.movimiento?.referencia ?? "—"} · {item.movimiento?.descripcion ?? "—"} · {formatMoney(item.movimiento?.monto, currency)} · {item.id} <Evidence evidence={item.evidencia} onOpenSource={onOpenSource} /></li>)}</ul>)}
              </article>)}
            </div>}
            <div className="difference-coverage">
              <strong>Cobertura y saldos iniciales reportados</strong>
              {(["banco", "contabilidad"] as const).map((side) => {
                const label = side === "banco" ? "Banco" : "Contabilidad";
                const item = difference.cobertura_y_saldos_iniciales?.[side] ?? {};
                const evidence = item.evidencia ?? {};
                return <div key={side}>
                  <strong>{label}</strong>
                  <span>Cobertura: {item.cobertura?.desde ?? "No disponible"} al {item.cobertura?.hasta ?? "No disponible"}</span>
                  <span>Saldo inicial: {formatMoney(item.saldo_inicial, currency)}</span>
                  {evidence.fecha && <span>Fecha: {evidence.fecha}</span>}
                  {evidence.archivo && <span>Archivo: {evidence.archivo}</span>}
                  {evidence.hoja && <span>Hoja: {evidence.hoja}</span>}
                  {evidence.fila && <span>Fila: {evidence.fila}</span>}
                </div>;
              })}
            </div>
          </section>
          {!difference.causa_determinada && <p className="difference-undetermined">La causa de la diferencia no está determinada con la evidencia disponible. Los pendientes y candidatos no se atribuyen como comisión, cheque o depósito sin verificación.</p>}
        </section>
      </div>}

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
