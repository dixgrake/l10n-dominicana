# -*- coding: utf-8 -*-
"""Post-migrate: ncf_manager (v12) → l10n_do_accounting (v17)

Runs AFTER the module upgrade is fully applied.  At this point every v17
column exists and l10n_latam.document.type records are in the database.

WHAT THIS SCRIPT DOES
---------------------
1. Cleanup    – remove views, ir.model.data and mark obsolete v12 modules
               (ncf_manager, ncf_invoice_template, l10n_do_currency_update,
               web_responsive) as uninstalled so they no longer cause errors.
2. Decontam.  – fix out_invoice / out_refund that got an E-type NCF from a
               previous bad migration (restore from payment_reference).
3. Safety net – fill any l10n_do_fiscal_number that is still NULL.
4. Assign     – l10n_latam_document_type_id from the 3-char NCF prefix.
5. Mark       – l10n_latam_manual_document_number = True on vendor bills.
6. Recompute  – l10n_do_sequence_prefix / l10n_do_sequence_number (stored
                computed fields bypassed by the raw-SQL writes in pre-migrate).
7. Backup     – update ncf_migration_backup with the final state.
8. Report msg – send an inbox notification to all admin users with the
               migration summary (renumbered NCFs, statistics, next steps).
9. Report     – print a summary to the server log.

WHAT THIS SCRIPT DOES NOT DO
-----------------------------
× Drop database columns or tables used by l10n_do_accounting
× Touch sequences, journals, partners, or taxes
"""

import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

_NCF_B   = r'^[Bb][0-9]{10}$'
_NCF_ANY = r'^[BbEe][0-9]{2}[0-9]{6,10}$'


def migrate(cr, version):
    _logger.info("=" * 70)
    _logger.info("POST-MIGRATE  ncf_manager → l10n_do_accounting")
    _logger.info("=" * 70)

    env = api.Environment(cr, SUPERUSER_ID, {})

    _cleanup_obsolete_modules(cr)
    _fix_contaminated_out_invoices(cr)
    _safety_net(cr)
    _assign_document_types(cr, env)
    _set_manual_document_number(cr)
    _recompute_split_sequences(cr)
    _update_backup_table(cr)
    _send_migration_report_to_admins(cr)
    _report(cr)

    _logger.info("POST-MIGRATE done")


# ---------------------------------------------------------------------------
# 1. Clean up obsolete v12 modules
# ---------------------------------------------------------------------------

# v12 modules replaced by l10n_do_accounting in v17.
# Their views cause "ValueError: can only parse strings" on /web because
# they inherit from parent templates that no longer exist.
_OBSOLETE_MODULES = (
    'ncf_manager',
    'ncf_invoice_template',
    'l10n_do_currency_update',
    'web_responsive',           # v12 theme — not available in v17 addons path
)


def _cleanup_obsolete_modules(cr):
    """Remove views and ir.model.data for replaced v12 modules.

    Modules listed in _OBSOLETE_MODULES were installed in v12 but are not
    present in v17 (replaced by l10n_do_accounting).  Odoo logs them as
    "not installable, skipped" but keeps their database records, including
    QWeb templates with arch that fails to parse in v17 context.
    """
    _logger.info("--- Cleaning up obsolete v12 modules ---")

    # Delete ir.ui.view records owned by these modules
    cr.execute("""
        DELETE FROM ir_ui_view v
        USING ir_model_data d
        WHERE d.res_id  = v.id
          AND d.model   = 'ir.ui.view'
          AND d.module  IN %s
    """, (_OBSOLETE_MODULES,))
    _logger.info("  Views deleted: %d", cr.rowcount)

    # Delete ir_asset records (JS/CSS files in asset bundles)
    cr.execute("""
        DELETE FROM ir_asset
        WHERE path ~ %s
    """, ('^/(' + '|'.join(_OBSOLETE_MODULES) + ')/',))
    _logger.info("  Asset records deleted: %d", cr.rowcount)

    # Clear compiled bundle cache so Odoo regenerates clean bundles
    cr.execute("""
        DELETE FROM ir_attachment
        WHERE url  LIKE '/web/assets/%'
           OR name LIKE '%.assets_%.min.%'
    """)
    _logger.info("  Bundle cache cleared: %d attachments", cr.rowcount)

    # Clean up all ir.model.data pointers for these modules
    cr.execute("""
        DELETE FROM ir_model_data
        WHERE module IN %s
    """, (_OBSOLETE_MODULES,))
    _logger.info("  ir.model.data records deleted: %d", cr.rowcount)

    # Mark modules as uninstalled so Odoo stops loading or upgrading them
    cr.execute("""
        UPDATE ir_module_module
           SET state = 'uninstalled'
         WHERE name  IN %s
           AND state IN ('installed', 'to upgrade', 'to install')
    """, (_OBSOLETE_MODULES,))
    _logger.info("  Modules marked uninstalled: %d", cr.rowcount)


# ---------------------------------------------------------------------------
# 2. Fix contaminated out_invoice / out_refund records
# ---------------------------------------------------------------------------

def _fix_contaminated_out_invoices(cr):
    """Correct out_invoice / out_refund records whose l10n_do_fiscal_number
    doesn't match their payment_reference.

    The Odoo core migration copied account_invoice.reference → payment_reference
    for out types, making payment_reference the authoritative NCF source.
    Any divergence — wrong B-type serial, wrong prefix, or E-type on a customer
    invoice — means a previous migration assigned the wrong NCF.

    Records are processed in id order so the lowest-id record always gets the
    original NCF; later duplicates of the same payment_reference are renumbered.

    After the main pass, any remaining E-type on out_invoice (unreachable via
    payment_reference) is cleared for manual reassignment.
    """
    _logger.info("--- Fixing out_invoice NCFs from payment_reference ---")

    cr.execute("""
        SELECT COUNT(*) FROM account_move
         WHERE move_type IN ('out_invoice', 'out_refund')
           AND payment_reference ~ %s
           AND l10n_do_fiscal_number IS NOT NULL
           AND l10n_do_fiscal_number != ''
           AND l10n_do_fiscal_number != UPPER(payment_reference)
    """, (_NCF_B,))
    mismatched = cr.fetchone()[0]

    if not mismatched:
        _logger.info("  All out_invoice NCFs match payment_reference")
    else:
        _logger.warning(
            "  Mismatched out_invoice NCFs: %d — restoring from payment_reference",
            mismatched)

        cr.execute("""
            SELECT id, company_id, payment_reference
              FROM account_move
             WHERE move_type IN ('out_invoice', 'out_refund')
               AND payment_reference ~ %s
               AND l10n_do_fiscal_number IS NOT NULL
               AND l10n_do_fiscal_number != ''
               AND l10n_do_fiscal_number != UPPER(payment_reference)
             ORDER BY id
        """, (_NCF_B,))
        records = cr.fetchall()

        restored   = 0
        renumbered = 0
        for inv_id, company_id, payment_ref in records:
            target_ncf = payment_ref.upper()

            # Check current state — may have changed in an earlier loop iteration
            cr.execute("""
                SELECT 1 FROM account_move
                 WHERE l10n_do_fiscal_number = %s
                   AND company_id            = %s
                   AND id                   != %s
                   AND move_type IN ('out_invoice','out_refund')
            """, (target_ncf, company_id, inv_id))
            conflict = cr.fetchone()

            if not conflict:
                new_ncf = target_ncf
                restored += 1
            else:
                prefix    = target_ncf[:3]
                total_len = 11  # B-type: 3-char prefix + 8-digit suffix

                cr.execute("""
                    SELECT COALESCE(
                        MAX(CAST(SUBSTRING(l10n_do_fiscal_number FROM 4) AS BIGINT)), 0)
                      FROM account_move
                     WHERE company_id                     = %s
                       AND LEFT(l10n_do_fiscal_number, 3) = %s
                       AND LENGTH(l10n_do_fiscal_number)  = %s
                       AND move_type IN ('out_invoice','out_refund')
                """, (company_id, prefix, total_len))
                max_seq = cr.fetchone()[0] + 1
                new_ncf = prefix + str(max_seq).zfill(8)
                _logger.warning(
                    "  Renumbered id=%d: %s (conflict) → %s",
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

        _logger.info("  Restored: %d  Renumbered: %d", restored, renumbered)

    # Clear any remaining E-type on out_invoice (no valid B-type payment_reference)
    cr.execute("""
        UPDATE account_move
           SET l10n_do_fiscal_number       = NULL,
               l10n_latam_document_type_id = NULL,
               l10n_do_sequence_prefix     = NULL,
               l10n_do_sequence_number     = 0
         WHERE move_type IN ('out_invoice', 'out_refund')
           AND l10n_do_fiscal_number ~ '^[Ee][0-9]{2}'
    """)
    if cr.rowcount:
        _logger.warning(
            "  Cleared %d E-type NCFs on out_invoice (no valid payment_reference)"
            " — manual review needed",
            cr.rowcount)


# ---------------------------------------------------------------------------
# 3. Safety net
# ---------------------------------------------------------------------------

def _safety_net(cr):
    """Fill any l10n_do_fiscal_number that pre-migrate left empty.

    For out_invoice/out_refund a loop with conflict detection is required
    because duplicate payment_references (genuine v12 duplicates) would
    otherwise violate the unique sales-side NCF index.
    For in_invoice/in_refund a bulk UPDATE is safe (no unique constraint).
    """
    _logger.info("--- Safety net ---")

    # Customer invoices: loop to handle v12 duplicate payment_references
    cr.execute("""
        SELECT id, company_id, payment_reference
          FROM account_move
         WHERE move_type IN ('out_invoice', 'out_refund')
           AND payment_reference ~ %s
           AND (l10n_do_fiscal_number IS NULL OR l10n_do_fiscal_number = '')
         ORDER BY id
    """, (_NCF_B,))
    out_records = cr.fetchall()

    out_filled    = 0
    out_renumbered = 0
    for inv_id, company_id, payment_ref in out_records:
        target_ncf = payment_ref.upper()

        cr.execute("""
            SELECT 1 FROM account_move
             WHERE l10n_do_fiscal_number = %s
               AND company_id            = %s
               AND id                   != %s
               AND move_type IN ('out_invoice','out_refund')
        """, (target_ncf, company_id, inv_id))
        conflict = cr.fetchone()

        if not conflict:
            new_ncf = target_ncf
            out_filled += 1
        else:
            prefix    = target_ncf[:3]
            total_len = 11
            cr.execute("""
                SELECT COALESCE(
                    MAX(CAST(SUBSTRING(l10n_do_fiscal_number FROM 4) AS BIGINT)), 0)
                  FROM account_move
                 WHERE company_id                     = %s
                   AND LEFT(l10n_do_fiscal_number, 3) = %s
                   AND LENGTH(l10n_do_fiscal_number)  = %s
                   AND move_type IN ('out_invoice','out_refund')
            """, (company_id, prefix, total_len))
            max_seq = cr.fetchone()[0] + 1
            new_ncf = prefix + str(max_seq).zfill(8)
            _logger.warning("  Safety net renumbered id=%d: %s (conflict) → %s",
                            inv_id, target_ncf, new_ncf)
            out_renumbered += 1

        cr.execute("""
            UPDATE account_move SET l10n_do_fiscal_number = %s WHERE id = %s
        """, (new_ncf, inv_id))

    if out_filled or out_renumbered:
        _logger.info("  Safety net out_invoice: %d filled, %d renumbered",
                     out_filled, out_renumbered)

    # Vendor invoices: bulk UPDATE is safe (no unique constraint)
    cr.execute("""
        UPDATE account_move
           SET l10n_do_fiscal_number = UPPER(ref)
         WHERE move_type IN ('in_invoice', 'in_refund')
           AND ref ~ %s
           AND (l10n_do_fiscal_number IS NULL OR l10n_do_fiscal_number = '')
    """, (_NCF_ANY,))
    if cr.rowcount:
        _logger.info("  Safety net in_invoice: %d filled", cr.rowcount)

    if not out_records and not cr.rowcount:
        _logger.info("  Safety net: nothing to fill")


# ---------------------------------------------------------------------------
# 2. Assign l10n_latam_document_type_id
# ---------------------------------------------------------------------------

def _assign_document_types(cr, env):
    """Map NCF prefix (first 3 chars) to l10n_latam_document_type_id.

    Dominican document types have doc_code_prefix = 'B01', 'B02', 'E31', etc.,
    which is exactly the first 3 characters of every NCF.
    """
    _logger.info("--- Assigning document types ---")

    doc_types = env['l10n_latam.document.type'].search([
        ('country_id.code', '=', 'DO'),
        ('doc_code_prefix', '!=', False),
    ])
    if not doc_types:
        _logger.warning("  No Dominican document types found – check module data")
        return

    prefix_map = {dt.doc_code_prefix: dt.id for dt in doc_types}
    _logger.info("  Prefixes available: %s", sorted(prefix_map))

    total = 0
    for prefix, dt_id in prefix_map.items():
        cr.execute("""
            UPDATE account_move
               SET l10n_latam_document_type_id = %s
             WHERE move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
               AND LEFT(l10n_do_fiscal_number, 3) = %s
               AND l10n_latam_document_type_id IS NULL
        """, (dt_id, prefix))
        if cr.rowcount:
            _logger.info("  %s → doc_type %d : %d records", prefix, dt_id, cr.rowcount)
            total += cr.rowcount

    _logger.info("  Document types assigned: %d", total)

    # Warn about NCFs with no matching document type
    cr.execute("""
        SELECT move_type, LEFT(l10n_do_fiscal_number, 3) AS prefix, COUNT(*) AS n
          FROM account_move
         WHERE move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
           AND l10n_do_fiscal_number IS NOT NULL AND l10n_do_fiscal_number != ''
           AND l10n_latam_document_type_id IS NULL
         GROUP BY move_type, prefix
         ORDER BY move_type, prefix
    """)
    for move_type, prefix, n in cr.fetchall():
        _logger.warning("  UNRESOLVED prefix '%s' (%s): %d records", prefix, move_type, n)


# ---------------------------------------------------------------------------
# 3. Mark vendor bills as manual
# ---------------------------------------------------------------------------

def _set_manual_document_number(cr):
    """Vendor NCFs are always manually entered (they belong to the supplier)."""
    _logger.info("--- Marking vendor bills as manual ---")
    cr.execute("""
        UPDATE account_move
           SET l10n_latam_manual_document_number = TRUE
         WHERE move_type IN ('in_invoice', 'in_refund')
           AND l10n_latam_document_type_id IS NOT NULL
           AND (l10n_latam_manual_document_number IS NULL
                OR l10n_latam_manual_document_number = FALSE)
    """)
    _logger.info("  Vendor bills marked manual: %d", cr.rowcount)


# ---------------------------------------------------------------------------
# 4. Recompute stored split-sequence fields
# ---------------------------------------------------------------------------

def _recompute_split_sequences(cr):
    """Backfill l10n_do_sequence_prefix and l10n_do_sequence_number.

    These stored computed fields depend on l10n_do_fiscal_number via @api.depends
    but raw-SQL writes bypass the ORM trigger.  Without correct values, every new
    invoice after migration would restart the sequence counter from 1.
    """
    _logger.info("--- Recomputing split-sequence fields ---")
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
    _logger.info("  Split sequences updated: %d records", cr.rowcount)

    # Sanity check: highest sequence per prefix
    cr.execute("""
        SELECT l10n_do_sequence_prefix, move_type,
               MAX(l10n_do_sequence_number) AS last_seq, COUNT(*) AS total
          FROM account_move
         WHERE l10n_do_sequence_prefix IS NOT NULL AND l10n_do_sequence_prefix != ''
           AND move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
         GROUP BY l10n_do_sequence_prefix, move_type
         ORDER BY move_type, l10n_do_sequence_prefix
    """)
    for prefix, mtype, last_seq, total in cr.fetchall():
        _logger.info("  %-4s  %-12s  last=%d  total=%d", prefix, mtype, last_seq, total)


# ---------------------------------------------------------------------------
# 5. Update backup table with final state
# ---------------------------------------------------------------------------

def _update_backup_table(cr):
    """Persist the final NCF and document type in ncf_migration_backup."""
    cr.execute("""
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'ncf_migration_backup' LIMIT 1
    """)
    if not cr.fetchone():
        _logger.warning("  ncf_migration_backup not found – skipping")
        return

    cr.execute("""
        UPDATE ncf_migration_backup b
           SET fiscal_after   = am.l10n_do_fiscal_number,
               doc_type_after = am.l10n_latam_document_type_id
          FROM account_move am
         WHERE am.id = b.move_id
    """)
    _logger.info("  Backup table updated: %d records", cr.rowcount)


# ---------------------------------------------------------------------------
# 6. Chatter notes for renumbered invoices
# ---------------------------------------------------------------------------

def _send_migration_report_to_admins(cr):
    """Send an inbox notification to all admin users with the migration summary.

    The message appears in each administrator's Odoo inbox (not email) and
    summarises every action taken during the NCF migration so administrators
    can review and validate the results without opening the logs.
    """
    _logger.info("--- Sending migration report to admin users ---")

    # ── Collect statistics ────────────────────────────────────────────────

    # Overall NCF migration result
    cr.execute("""
        SELECT move_type,
               COUNT(*)                                                       AS total,
               COUNT(l10n_do_fiscal_number)
                   FILTER (WHERE l10n_do_fiscal_number != '')                 AS with_ncf,
               COUNT(l10n_latam_document_type_id)                             AS with_doc_type
          FROM account_move
         WHERE move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
         GROUP BY move_type ORDER BY move_type
    """)
    invoice_stats = cr.fetchall()

    # Renumbered records
    renumbered_rows = []
    cr.execute("""
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'ncf_migration_backup' LIMIT 1
    """)
    if cr.fetchone():
        cr.execute("""
            SELECT move_name, move_type,
                   COALESCE(payment_ref_before, ref_before) AS original_ncf,
                   fiscal_after, notes
              FROM ncf_migration_backup
             WHERE renumbered = TRUE
             ORDER BY move_type, move_name
        """)
        renumbered_rows = cr.fetchall()

    # ── Build HTML body ───────────────────────────────────────────────────

    rows_html = ''.join(
        '<tr>'
        f'<td style="padding:4px 8px;border:1px solid #dee2e6">{mt}</td>'
        f'<td style="padding:4px 8px;border:1px solid #dee2e6;text-align:center">{total}</td>'
        f'<td style="padding:4px 8px;border:1px solid #dee2e6;text-align:center">{with_ncf}</td>'
        f'<td style="padding:4px 8px;border:1px solid #dee2e6;text-align:center">{with_dt}</td>'
        '</tr>'
        for mt, total, with_ncf, with_dt in invoice_stats
    )

    if renumbered_rows:
        ren_rows_html = ''.join(
            '<tr>'
            f'<td style="padding:4px 8px;border:1px solid #dee2e6">{name}</td>'
            f'<td style="padding:4px 8px;border:1px solid #dee2e6">{mt}</td>'
            f'<td style="padding:4px 8px;border:1px solid #dee2e6">{orig or "—"}</td>'
            f'<td style="padding:4px 8px;border:1px solid #dee2e6">{after or "—"}</td>'
            '</tr>'
            for name, mt, orig, after, _notes in renumbered_rows
        )
        renumbered_section = f"""
        <h3 style="color:#856404;margin-top:20px">
            ⚠ Comprobantes Renumerados ({len(renumbered_rows)})
        </h3>
        <p>Los siguientes comprobantes tenían duplicados en el sistema de origen
           (v12) y fueron renumerados automáticamente para cumplir con la
           unicidad requerida por el módulo de facturación dominicana.</p>
        <table style="border-collapse:collapse;width:100%;font-size:13px">
          <thead style="background:#fff3cd">
            <tr>
              <th style="padding:6px 8px;border:1px solid #dee2e6;text-align:left">Factura</th>
              <th style="padding:6px 8px;border:1px solid #dee2e6;text-align:left">Tipo</th>
              <th style="padding:6px 8px;border:1px solid #dee2e6;text-align:left">NCF original</th>
              <th style="padding:6px 8px;border:1px solid #dee2e6;text-align:left">NCF asignado</th>
            </tr>
          </thead>
          <tbody>{ren_rows_html}</tbody>
        </table>
        <p style="margin-top:8px">
            Puede consultar el historial completo en la tabla
            <code>ncf_migration_backup</code> de la base de datos.
        </p>"""
    else:
        renumbered_section = """
        <p style="color:#155724;background:#d4edda;padding:8px 12px;
                  border-radius:4px;margin-top:16px">
            ✓ No se renumeró ningún comprobante — todos los NCF se migraron
            exactamente como estaban en la versión 12.
        </p>"""

    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:800px">

      <h2 style="color:#1f5c99">
          Migración completada: ncf_manager (v12) → l10n_do_accounting (v17)
      </h2>

      <p>La migración de comprobantes fiscales (NCF) de la localización
         dominicana ha finalizado correctamente.</p>

      <h3 style="color:#155724;margin-top:20px">Resumen de facturas migradas</h3>
      <table style="border-collapse:collapse;width:100%;font-size:13px">
        <thead style="background:#d4edda">
          <tr>
            <th style="padding:6px 8px;border:1px solid #dee2e6;text-align:left">Tipo</th>
            <th style="padding:6px 8px;border:1px solid #dee2e6;text-align:center">Total</th>
            <th style="padding:6px 8px;border:1px solid #dee2e6;text-align:center">Con NCF</th>
            <th style="padding:6px 8px;border:1px solid #dee2e6;text-align:center">Con tipo doc.</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>

      {renumbered_section}

      <h3 style="margin-top:20px">Módulos v12 desactivados</h3>
      <p>Los siguientes módulos de la versión 12 han sido marcados como
         desinstalados para evitar errores de renderizado:</p>
      <ul>
        {''.join(f'<li><code>{m}</code></li>' for m in _OBSOLETE_MODULES)}
      </ul>

      <h3 style="margin-top:20px">Acciones recomendadas</h3>
      <ol>
        <li>Verificar las facturas sin comprobante (columna <em>Con NCF</em> vs <em>Total</em>).</li>
        <li>Revisar los comprobantes renumerados (si los hay) y validar con contabilidad.</li>
        <li>Generar una prueba de los reportes 606, 607 y 608 para el período más reciente.</li>
        <li>Confirmar que las secuencias de facturas nuevas continúan desde el último NCF utilizado.</li>
      </ol>

    </div>"""

    # ── Find admin users ──────────────────────────────────────────────────

    cr.execute("""
        SELECT DISTINCT rp.id AS partner_id, ru.id AS user_id
          FROM res_users ru
          JOIN res_partner rp ON rp.id = ru.partner_id
          JOIN res_groups_users_rel gur ON gur.uid = ru.id
          JOIN res_groups rg ON rg.id = gur.gid
          JOIN ir_model_data imd ON imd.res_id = rg.id
                                AND imd.model  = 'res.groups'
                                AND imd.module = 'base'
                                AND imd.name   = 'group_system'
         WHERE ru.active = TRUE AND ru.share = FALSE
         ORDER BY ru.id
    """)
    admins = cr.fetchall()

    if not admins:
        _logger.warning("  No admin users found — skipping report message")
        return

    # ── System bot as author (OdooBot) ────────────────────────────────────
    cr.execute("""
        SELECT rp.id FROM res_users ru
          JOIN res_partner rp ON rp.id = ru.partner_id
         WHERE ru.id = 1
    """)
    row = cr.fetchone()
    author_id = row[0] if row else admins[0][0]

    cr.execute("""
        SELECT id FROM mail_message_subtype
         WHERE res_model IS NULL ORDER BY id LIMIT 1
    """)
    row = cr.fetchone()
    subtype_id = row[0] if row else None

    # ── Insert message ────────────────────────────────────────────────────
    cr.execute("""
        INSERT INTO mail_message
            (model, res_id, message_type, subtype_id, body,
             author_id, date, is_internal, email_from)
        VALUES ('', 0, 'notification', %s, %s, %s, NOW(), FALSE, '')
        RETURNING id
    """, (subtype_id, body, author_id))
    msg_id = cr.fetchone()[0]

    # ── Send inbox notification to each admin ─────────────────────────────
    for partner_id, _uid in admins:
        cr.execute("""
            INSERT INTO mail_notification
                (mail_message_id, res_partner_id, notification_type,
                 notification_status, is_read)
            VALUES (%s, %s, 'inbox', 'sent', FALSE)
            ON CONFLICT DO NOTHING
        """, (msg_id, partner_id))

    _logger.info(
        "  Migration report sent to %d admin user(s) (msg_id=%d)",
        len(admins), msg_id
    )


# ---------------------------------------------------------------------------
# 7. Report
# ---------------------------------------------------------------------------

def _report(cr):
    _logger.info("=" * 70)
    _logger.info("MIGRATION REPORT — ncf_manager → l10n_do_accounting")
    _logger.info("=" * 70)

    cr.execute("""
        SELECT move_type,
               COUNT(*)                                                   AS total,
               COUNT(l10n_do_fiscal_number)
                   FILTER (WHERE l10n_do_fiscal_number != '')              AS with_ncf,
               COUNT(l10n_latam_document_type_id)                          AS with_doc_type,
               COUNT(*) FILTER (WHERE l10n_latam_manual_document_number)   AS manual
          FROM account_move
         WHERE move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
         GROUP BY move_type
         ORDER BY move_type
    """)
    _logger.info("  %-15s %7s %8s %9s %6s",
                 "move_type", "total", "with_ncf", "doc_type", "manual")
    for row in cr.fetchall():
        _logger.info("  %-15s %7d %8d %9d %6d", *row)

    cr.execute("""
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'ncf_migration_backup' LIMIT 1
    """)
    if cr.fetchone():
        cr.execute("""
            SELECT
                move_type,
                COUNT(*)                                                     AS total,
                COUNT(*) FILTER (WHERE payment_ref_before = fiscal_after
                                    OR ref_before = fiscal_after)            AS exact_match,
                COUNT(*) FILTER (WHERE renumbered = TRUE)                    AS renumbered,
                COUNT(*) FILTER (WHERE fiscal_after IS NULL
                                    OR fiscal_after = '')                    AS no_ncf
              FROM ncf_migration_backup
             GROUP BY move_type
             ORDER BY move_type
        """)
        _logger.info("")
        _logger.info("  %-15s %7s %11s %10s %6s",
                     "move_type", "total", "exact_match", "renumbered", "no_ncf")
        for row in cr.fetchall():
            _logger.info("  %-15s %7d %11d %10d %6d", *row)

    _logger.info("=" * 70)
