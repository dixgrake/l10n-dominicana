# -*- coding: utf-8 -*-
"""Post-migrate 17.0.1.0.9 — Corrección integral de NCFs y tipos de documento.

Corrige cinco categorías de problemas detectados tras la migración 17.0.1.0.8:

1. CINF B11 (Comprobante de Compra) — campos NCF intercambiados:
   En v12 el nombre de secuencia de la factura (B1100000001…) ERA el NCF
   propio de la empresa.  La migración anterior copió `ref` →
   `l10n_do_fiscal_number`, pero para diario CINF el campo `ref` contenía
   también el NCF B11 (no el del proveedor).  Resultado: el NCF del
   proveedor quedó en `l10n_do_fiscal_number` y el B11 propio quedó
   únicamente en `name`.  Se corrige haciendo el swap y fijando doc_type=B11.
   También se corrigen las anomalías:
     - Factura 567  (CFIS): tiene B11 en fiscal_number — debe usar ref (B01)
     - Factura 1948 (VNT):  tiene B11 en fiscal_number — debe usar
       payment_reference (B02)

2. Facturas de proveedores (in_invoice/in_refund) con NCF válido en `ref`
   pero `l10n_do_fiscal_number` vacío — el safety_net de 1.0.6 y 1.0.7
   no los procesó.  Se copian masivamente sin riesgo de constraint porque
   la investigación previa confirmó 0 colisiones cruzadas.

3. Facturas de clientes (out_invoice/out_refund) con NCF en
   `payment_reference` pero `l10n_do_fiscal_number` vacío — mismo
   patrón que el punto anterior.  Se usa paso secuencial para evitar
   conflictos del índice único de ventas.

4. Tipos de documento incorrectos o ausentes:
   Se sobreescribe `l10n_latam_document_type_id` en cualquier factura
   donde el prefijo del NCF (LEFT 3) no coincida con el tipo asignado
   (cubre tanto los 22 registros con tipo incorrecto como los 13 con NULL).

5. Diario "Otros Gastos (sin NCF)" (code=GASTO):
   Tenía `l10n_latam_use_documents = True` a pesar de que su nombre y
   propósito indican que NO requiere comprobante fiscal.  Se desactiva.
"""

import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

_NCF_B   = r'^[Bb][0-9]{10}$'
_NCF_ANY = r'^[BbEe][0-9]{2}[0-9]{6,10}$'


def migrate(cr, version):
    _logger.info("=" * 70)
    _logger.info("POST-MIGRATE 17.0.1.0.9 — Corrección integral de NCFs")
    _logger.info("=" * 70)

    env = api.Environment(cr, SUPERUSER_ID, {})

    # Orden importante: CINF B11 primero libera NCF B1100000002 de factura 567
    # antes de que la corrección masiva de proveedores intente asignarlo.
    _fix_cinf_b11_invoices(cr)
    _fix_vendor_ncf_from_ref(cr)
    _fix_out_invoice_ncf(cr)
    _fix_document_types(cr, env)
    _recompute_split_sequences(cr)
    _fix_journal_gasto(cr)
    _report(cr)

    _logger.info("POST-MIGRATE 17.0.1.0.9 done")


# ---------------------------------------------------------------------------
# 1. Corrección CINF B11 — campos intercambiados
# ---------------------------------------------------------------------------

def _fix_cinf_b11_invoices(cr):
    """Corrige las facturas CINF (B11) con campos NCF intercambiados y anomalías.

    Estado incorrecto (resultado de la migración anterior):
        name              = B11XXXXXXXX  ← NCF propio de la empresa (correcto aquí)
        ref               = B11XXXXXXXX  ← mismo valor (no debe ser así)
        l10n_do_fiscal_number = B01/E31  ← NCF del proveedor (campo equivocado)
        doc_type          = B01 / E31    ← incorrecto (debe ser B11)

    Estado correcto tras esta función:
        name              = B11XXXXXXXX  ← sin cambio
        ref               = B01/E31      ← NCF del proveedor (lugar correcto)
        l10n_do_fiscal_number = B11XXXX  ← NCF propio B11 (lugar correcto)
        doc_type          = 5 (B11)      ← correcto
    """
    _logger.info("--- Corrigiendo CINF B11 (campos intercambiados) ---")

    # Obtener id del tipo de documento B11
    cr.execute("""
        SELECT id FROM l10n_latam_document_type
         WHERE doc_code_prefix = 'B11'
           AND country_id = (SELECT id FROM res_country WHERE code = 'DO')
         LIMIT 1
    """)
    row = cr.fetchone()
    if not row:
        _logger.warning("  Tipo de documento B11 no encontrado — omitiendo")
        return
    b11_dt_id = row[0]

    # Paso 1: Factura 567 (CFIS con B11 en fiscal_number)
    # Debe corregirse ANTES que los CINF para liberar B1100000002 del índice
    # (aunque son partners distintos, conviene procesar primero).
    cr.execute("""
        UPDATE account_move
           SET l10n_do_fiscal_number       = UPPER(ref),
               l10n_latam_document_type_id = NULL,
               l10n_do_sequence_prefix     = NULL,
               l10n_do_sequence_number     = 0
         WHERE id = 567
           AND l10n_do_fiscal_number ~ '^B11'
           AND ref ~ %s
    """, (_NCF_ANY,))
    _logger.info("  Factura 567 (CFIS/B11→ref): %d fila(s)", cr.rowcount)

    # Paso 2: Factura 1948 (VNT out_invoice con B11 en fiscal_number)
    cr.execute("""
        UPDATE account_move
           SET l10n_do_fiscal_number       = UPPER(payment_reference),
               l10n_latam_document_type_id = NULL,
               l10n_do_sequence_prefix     = NULL,
               l10n_do_sequence_number     = 0
         WHERE id = 1948
           AND l10n_do_fiscal_number ~ '^B11'
           AND payment_reference ~ %s
    """, (_NCF_B,))
    _logger.info("  Factura 1948 (VNT/B11→pmt_ref): %d fila(s)", cr.rowcount)

    # Paso 3: Las 6 facturas CINF — swap atómico en PostgreSQL
    # En un solo UPDATE, PostgreSQL lee todos los valores VIEJOS antes de
    # escribir: el swap ref ↔ l10n_do_fiscal_number es seguro y atómico.
    cr.execute("""
        UPDATE account_move
           SET ref                              = l10n_do_fiscal_number,
               l10n_do_fiscal_number            = name,
               l10n_latam_document_type_id      = %s,
               l10n_latam_manual_document_number = FALSE,
               l10n_do_sequence_prefix          = 'B11',
               l10n_do_sequence_number          = CAST(
                   SUBSTRING(name FROM 4) AS INTEGER)
         WHERE journal_id = (
                   SELECT id FROM account_journal
                    WHERE code = 'CINF' LIMIT 1
               )
           AND move_type = 'in_invoice'
           AND name      ~ '^B11[0-9]{8}$'
           AND l10n_do_fiscal_number IS NOT NULL
           AND l10n_do_fiscal_number != ''
    """, (b11_dt_id,))
    _logger.info("  Facturas CINF B11 corregidas: %d", cr.rowcount)


# ---------------------------------------------------------------------------
# 2. Facturas de proveedores — NCF vacío pero válido en ref
# ---------------------------------------------------------------------------

def _fix_vendor_ncf_from_ref(cr):
    """Copia ref → l10n_do_fiscal_number para facturas de proveedores.

    Investigación previa confirmó 0 colisiones cruzadas de
    (l10n_do_fiscal_number, commercial_partner_id, company_id) entre las
    facturas afectadas, por lo que el UPDATE masivo es seguro.

    Las facturas CINF ya fueron procesadas en _fix_cinf_b11_invoices y
    no tienen l10n_do_fiscal_number vacío, por lo que no se ven afectadas.
    """
    _logger.info("--- Rellenando NCF de proveedores desde ref ---")
    cr.execute("""
        UPDATE account_move
           SET l10n_do_fiscal_number = UPPER(ref)
         WHERE move_type IN ('in_invoice', 'in_refund')
           AND ref ~ %s
           AND (l10n_do_fiscal_number IS NULL OR l10n_do_fiscal_number = '')
    """, (_NCF_ANY,))
    _logger.info("  Facturas de proveedor rellenadas: %d", cr.rowcount)

    # Registrar refs que NO coinciden con el patrón (posibles errores tipográficos)
    cr.execute("""
        SELECT id, name, ref, move_type
          FROM account_move
         WHERE move_type IN ('in_invoice', 'in_refund')
           AND ref IS NOT NULL AND ref != ''
           AND ref !~ %s
           AND (l10n_do_fiscal_number IS NULL OR l10n_do_fiscal_number = '')
           AND state = 'posted'
         LIMIT 20
    """, (_NCF_ANY,))
    unresolved = cr.fetchall()
    for inv_id, name, ref, mtype in unresolved:
        _logger.warning(
            "  Sin resolver (ref no coincide con patrón NCF): "
            "id=%d name=%s ref=%s type=%s", inv_id, name, ref, mtype)
    if unresolved:
        _logger.warning(
            "  → Estas %d factura(s) requieren revisión manual", len(unresolved))


# ---------------------------------------------------------------------------
# 3. Facturas de clientes — NCF vacío pero válido en payment_reference
# ---------------------------------------------------------------------------

def _fix_out_invoice_ncf(cr):
    """Copia payment_reference → l10n_do_fiscal_number para facturas de cliente.

    Usa paso secuencial para manejar payment_references duplicadas de v12:
    el registro de menor id conserva el NCF original; los posteriores
    duplicados reciben el siguiente número disponible en la misma serie.
    """
    _logger.info("--- Rellenando NCF de clientes desde payment_reference ---")

    cr.execute("""
        SELECT id, company_id, payment_reference
          FROM account_move
         WHERE move_type IN ('out_invoice', 'out_refund')
           AND payment_reference ~ %s
           AND (l10n_do_fiscal_number IS NULL OR l10n_do_fiscal_number = '')
         ORDER BY id
    """, (_NCF_B,))
    records = cr.fetchall()

    if not records:
        _logger.info("  Todos los NCF de clientes ya están asignados")
        return

    _logger.info("  Facturas de cliente a procesar: %d", len(records))
    restored   = 0
    renumbered = 0

    for inv_id, company_id, payment_ref in records:
        target_ncf = payment_ref.upper()

        cr.execute("""
            SELECT 1 FROM account_move
             WHERE l10n_do_fiscal_number = %s
               AND company_id            = %s
               AND id                   != %s
               AND move_type IN ('out_invoice', 'out_refund')
        """, (target_ncf, company_id, inv_id))
        conflict = cr.fetchone()

        if not conflict:
            new_ncf = target_ncf
            restored += 1
        else:
            prefix    = target_ncf[:3]
            total_len = 11  # B-type: prefijo 3 + secuencia 8
            cr.execute("""
                SELECT COALESCE(
                    MAX(CAST(SUBSTRING(l10n_do_fiscal_number FROM 4) AS BIGINT)), 0)
                  FROM account_move
                 WHERE company_id                     = %s
                   AND LEFT(l10n_do_fiscal_number, 3) = %s
                   AND LENGTH(l10n_do_fiscal_number)  = %s
                   AND move_type IN ('out_invoice', 'out_refund')
            """, (company_id, prefix, total_len))
            max_seq = cr.fetchone()[0] + 1
            new_ncf = prefix + str(max_seq).zfill(8)
            _logger.warning(
                "  Renumerada out_invoice id=%d: %s → %s (duplicado)",
                inv_id, target_ncf, new_ncf)
            renumbered += 1

        cr.execute("""
            UPDATE account_move
               SET l10n_do_fiscal_number       = %s,
                   l10n_latam_document_type_id = NULL,
                   l10n_do_sequence_prefix     = NULL,
                   l10n_do_sequence_number     = 0
             WHERE id = %s
        """, (new_ncf, inv_id))

    _logger.info(
        "  Clientes: %d restauradas, %d renumeradas", restored, renumbered)


# ---------------------------------------------------------------------------
# 4. Corrección de tipos de documento (NULL e incorrectos)
# ---------------------------------------------------------------------------

def _fix_document_types(cr, env):
    """Asigna o corrige l10n_latam_document_type_id según el prefijo del NCF.

    Cubre dos casos:
    a) doc_type IS NULL  → asignar el tipo correcto según LEFT(ncf, 3)
    b) doc_type incorrecto → sobreescribir (p.ej. E31 con tipo B01)

    Los registros CINF ya tienen su doc_type=B11 correcto asignado en el
    paso anterior; como su prefijo NCF coincide con el tipo, este UPDATE
    los deja intactos.
    """
    _logger.info("--- Corrigiendo tipos de documento ---")

    doc_types = env['l10n_latam.document.type'].search([
        ('country_id.code', '=', 'DO'),
        ('doc_code_prefix', '!=', False),
    ])
    if not doc_types:
        _logger.warning("  No se encontraron tipos de documento dominicanos")
        return

    prefix_map = {dt.doc_code_prefix: dt.id for dt in doc_types}
    _logger.info("  Prefijos disponibles: %s", sorted(prefix_map))

    total = 0
    for prefix, dt_id in prefix_map.items():
        cr.execute("""
            UPDATE account_move
               SET l10n_latam_document_type_id = %s
             WHERE move_type IN (
                       'out_invoice', 'out_refund', 'in_invoice', 'in_refund'
                   )
               AND LEFT(l10n_do_fiscal_number, 3) = %s
               AND (l10n_latam_document_type_id IS NULL
                    OR l10n_latam_document_type_id != %s)
        """, (dt_id, prefix, dt_id))
        if cr.rowcount:
            _logger.info("  %s → tipo %d: %d registro(s)", prefix, dt_id, cr.rowcount)
            total += cr.rowcount

    _logger.info("  Tipos de documento corregidos/asignados: %d total", total)


# ---------------------------------------------------------------------------
# 5. Recalcular prefijo y número de secuencia
# ---------------------------------------------------------------------------

def _recompute_split_sequences(cr):
    """Recalcula l10n_do_sequence_prefix y l10n_do_sequence_number.

    Solo actúa sobre registros donde los valores están vacíos o en cero —
    los registros CINF B11 ya se calcularon en _fix_cinf_b11_invoices y
    no se tocan aquí.
    """
    _logger.info("--- Recalculando campos de secuencia ---")
    cr.execute(r"""
        UPDATE account_move
           SET l10n_do_sequence_prefix = LEFT(l10n_do_fiscal_number, 3),
               l10n_do_sequence_number = CAST(
                   SUBSTRING(l10n_do_fiscal_number FROM 4) AS INTEGER)
         WHERE l10n_do_fiscal_number ~ %s
           AND (l10n_do_sequence_prefix IS NULL
                OR l10n_do_sequence_prefix = ''
                OR l10n_do_sequence_number  = 0)
    """, (_NCF_ANY,))
    _logger.info("  Secuencias actualizadas: %d registros", cr.rowcount)


# ---------------------------------------------------------------------------
# 6. Diario GASTO — desactivar l10n_latam_use_documents
# ---------------------------------------------------------------------------

def _fix_journal_gasto(cr):
    """Desactiva l10n_latam_use_documents en el diario 'Otros Gastos (sin NCF)'.

    El diario con code=GASTO se llama explícitamente 'Otros Gastos (sin NCF)'
    y contiene 150 facturas posted sin comprobante.  Tener
    l10n_latam_use_documents=True obligaba a los usuarios a ingresar un
    tipo de documento en gastos que no lo requieren.
    """
    _logger.info("--- Corrigiendo diario GASTO (sin NCF) ---")
    cr.execute("""
        UPDATE account_journal
           SET l10n_latam_use_documents = FALSE
         WHERE code = 'GASTO'
           AND l10n_latam_use_documents = TRUE
    """)
    _logger.info(
        "  l10n_latam_use_documents desactivado en GASTO: %d fila(s)",
        cr.rowcount)


# ---------------------------------------------------------------------------
# 7. Reporte final
# ---------------------------------------------------------------------------

def _report(cr):
    _logger.info("=" * 70)
    _logger.info("REPORTE 17.0.1.0.9 — Resumen de correcciones NCF")
    _logger.info("=" * 70)

    # Estadísticas por tipo de movimiento (excluye canceladas)
    cr.execute("""
        SELECT move_type,
               COUNT(*)                                                         AS total,
               COUNT(*) FILTER (WHERE l10n_do_fiscal_number IS NOT NULL
                                  AND l10n_do_fiscal_number != '')              AS con_ncf,
               COUNT(*) FILTER (WHERE l10n_latam_document_type_id IS NOT NULL)  AS con_tipo_doc
          FROM account_move
         WHERE move_type IN ('out_invoice', 'out_refund', 'in_invoice', 'in_refund')
           AND state != 'cancel'
         GROUP BY move_type
         ORDER BY move_type
    """)
    _logger.info("  %-15s %7s %8s %9s", "move_type", "total", "con_ncf", "con_tipo")
    for row in cr.fetchall():
        _logger.info("  %-15s %7d %8d %9d", *row)

    # Verificar si quedan discrepancias prefijo-NCF vs tipo de documento
    cr.execute("""
        SELECT COUNT(*) FROM account_move am
         WHERE move_type IN ('out_invoice', 'out_refund', 'in_invoice', 'in_refund')
           AND state != 'cancel'
           AND l10n_do_fiscal_number IS NOT NULL AND l10n_do_fiscal_number != ''
           AND l10n_latam_document_type_id IS DISTINCT FROM (
               SELECT id FROM l10n_latam_document_type
                WHERE doc_code_prefix = LEFT(am.l10n_do_fiscal_number, 3)
                  AND country_id = (SELECT id FROM res_country WHERE code = 'DO')
                LIMIT 1
           )
    """)
    mismatches = cr.fetchone()[0]
    if mismatches:
        _logger.warning(
            "  ⚠  %d factura(s) con prefijo NCF ≠ tipo de documento (revisar manualmente)",
            mismatches)
    else:
        _logger.info("  ✓  Todos los prefijos NCF coinciden con su tipo de documento")

    # Facturas posted con NCF en ref/payment_reference pero fiscal_number vacío
    cr.execute("""
        SELECT COUNT(*) FROM account_move
         WHERE move_type IN ('in_invoice', 'in_refund')
           AND state = 'posted'
           AND ref IS NOT NULL AND ref != ''
           AND ref !~ %s
           AND (l10n_do_fiscal_number IS NULL OR l10n_do_fiscal_number = '')
    """, (_NCF_ANY,))
    remaining_vendors = cr.fetchone()[0]
    if remaining_vendors:
        _logger.warning(
            "  ⚠  %d factura(s) de proveedor posted con ref no válido y sin NCF"
            " — revisar manualmente", remaining_vendors)

    # Estado del diario GASTO
    cr.execute("""
        SELECT l10n_latam_use_documents FROM account_journal WHERE code = 'GASTO'
    """)
    row = cr.fetchone()
    if row:
        estado = "ACTIVO (⚠ revisar)" if row[0] else "desactivado ✓"
        _logger.info("  Diario GASTO l10n_latam_use_documents: %s", estado)

    _logger.info("=" * 70)
