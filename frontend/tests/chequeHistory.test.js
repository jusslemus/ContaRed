import assert from "node:assert/strict";
import test from "node:test";

import {
  chequeCountKey,
  chequeStateLabel,
  parseChequeHistoryPayload,
} from "../src/components/chequeHistory.js";

const classificationAndDelivery = {
  cheque_id: "cheque-000121",
  movimiento_emision_id: "movement-000121",
  movimiento_cobro_id: null,
  conciliacion_origen_id: "reconciliation-september",
  conciliacion_resolucion_id: null,
  events: [
    {
      id: "event-classified",
      estado_anterior: null,
      estado_nuevo: "retenido",
      fecha_efectiva: "2026-09-09",
      registrado_en: "2026-09-09T10:00:00Z",
      usuario: "Auxiliar",
      motivo: "Clasificación inicial desde movimiento explícito de cheque",
      observacion: null,
      movimiento_banco_id: null,
      conciliacion_id: null,
      cruce_id: null,
      soporte_url: null,
    },
    {
      id: "event-delivered",
      estado_anterior: "retenido",
      estado_nuevo: "en_circulacion",
      fecha_efectiva: "2026-09-12",
      registrado_en: "2026-09-12T10:00:00Z",
      usuario: "Auxiliar",
      motivo: "Entrega registrada",
      observacion: "Entregado al beneficiario",
      movimiento_banco_id: null,
      conciliacion_id: null,
      cruce_id: null,
      soporte_url: null,
    },
  ],
};

test("reads API events for cheque 000121 and preserves trace references", () => {
  const parsed = parseChequeHistoryPayload(classificationAndDelivery);
  assert.ok(parsed);
  assert.deepEqual(parsed.events.map(({ estado_nuevo }) => estado_nuevo), [
    "retenido",
    "en_circulacion",
  ]);
  assert.equal(parsed.events[0].id, "event-classified");
  assert.equal(parsed.events[1].id, "event-delivered");
  assert.equal(parsed.links.movimiento_emision_id, "movement-000121");
  assert.equal(parsed.links.conciliacion_origen_id, "reconciliation-september");
});

test("distinguishes an empty event list from an invalid history response", () => {
  assert.deepEqual(
    parseChequeHistoryPayload({ ...classificationAndDelivery, events: [] })?.events,
    [],
  );
  assert.equal(parseChequeHistoryPayload({ results: [] }), null);
  assert.equal(
    parseChequeHistoryPayload({ ...classificationAndDelivery, events: [{ estado_nuevo: "retenido" }] }),
    null,
  );
  assert.equal(
    parseChequeHistoryPayload({
      ...classificationAndDelivery,
      events: [{ ...classificationAndDelivery.events[0], fecha_efectiva: "2026-99-99" }],
    }),
    null,
  );
});

test("uses complete cheque state labels and the backend counter keys", () => {
  assert.deepEqual(
    ["retenido", "en_circulacion", "cobrado", "anulado"].map(chequeStateLabel),
    ["Retenido", "En circulación", "Cobrado", "Anulado"],
  );
  assert.deepEqual(
    ["retenido", "en_circulacion", "cobrado", "anulado"].map(chequeCountKey),
    ["retenido", "en_circulacion", "cobrado", "anulado"],
  );
});
