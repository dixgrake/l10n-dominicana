# -*- coding: utf-8 -*-
# Part of Domincana Premium. See LICENSE file for full copyright and licensing details.
"""Pre-migration: ncf_manager (v12) → l10n_do_accounting (v17).

Odoo's core migrator already handled:
  - account.invoice → account.move  (v14 structural change)
  - All standard Odoo field renames

This script handles ONLY the custom fields added by ncf_manager that
survived the core migration as orphaned columns and must be renamed/remapped
BEFORE the v17 ORM loads, so that no data is lost when the ORM reconciles
the new field definitions.

Execution order during a v12 → v17 upgrade:
  [17.0.1.0.0] pre-migrate  ← this file
  [17.0.1.0.5] pre-migrate  (existing v15→v17 script, untouched)
  Odoo loads v17 module
  [17.0.1.0.0] post-migrate
  [17.0.1.0.5] post-migrate (existing v15→v17 script, untouched)
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    _logger.info("START pre-migrate ncf_manager v12 → l10n_do_accounting v17")
    _migrate_res_partner(cr)
    _migrate_account_move(cr)
    _migrate_account_journal(cr)
    _migrate_account_tax(cr)
    _delete_ncf_manager_views(cr)
    _logger.info("DONE  pre-migrate ncf_manager v12 → l10n_do_accounting v17")


# ---------------------------------------------------------------------------
# 1. res.partner
#    sale_fiscal_type  → l10n_do_dgii_tax_payer_type   (rename + value remap)
#    expense_type      → l10n_do_expense_type           (rename only)
# ---------------------------------------------------------------------------

def _migrate_res_partner(cr):
    _logger.info("Migrating res.partner custom fields")

    # 1a) sale_fiscal_type → l10n_do_dgii_tax_payer_type
    if _col(cr, 'res_partner', 'sale_fiscal_type'):
        if not _col(cr, 'res_partner', 'l10n_do_dgii_tax_payer_type'):
            cr.execute("ALTER TABLE res_partner ADD COLUMN l10n_do_dgii_tax_payer_type VARCHAR")

        # v12 value   → v17 value
        # final       → non_payer     (consumidor final, cédula 11 dígitos)
        # fiscal      → taxpayer      (crédito fiscal, RNC empresa)
        # gov         → governmental  (entidad gubernamental)
        # special     → special       (régimen especial / zona franca / iglesia)
        # unico       → non_payer     (único ingreso, persona física informal)
        # export      → foreigner     (cliente de exportación)
        value_map = [
            ('final',   'non_payer'),
            ('fiscal',  'taxpayer'),
            ('gov',     'governmental'),
            ('special', 'special'),
            ('unico',   'non_payer'),
            ('export',  'foreigner'),
        ]
        total = 0
        for old, new in value_map:
            cr.execute("""
                UPDATE res_partner
                   SET l10n_do_dgii_tax_payer_type = %s
                 WHERE sale_fiscal_type = %s
                   AND l10n_do_dgii_tax_payer_type IS NULL
            """, (new, old))
            if cr.rowcount:
                total += cr.rowcount
                _logger.info("  partner sale_fiscal_type '%s' → '%s': %d rows", old, new, cr.rowcount)

        # Unknown values fall back to non_payer (safest DGII classification)
        cr.execute("""
            UPDATE res_partner
               SET l10n_do_dgii_tax_payer_type = 'non_payer'
             WHERE sale_fiscal_type IS NOT NULL
               AND l10n_do_dgii_tax_payer_type IS NULL
        """)
        if cr.rowcount:
            _logger.warning("  %d partners had unrecognised sale_fiscal_type → 'non_payer'",
                            cr.rowcount)
        _logger.info("  Total partners migrated: %d", total)

    # 1b) expense_type → l10n_do_expense_type
    #     Values are already compatible (01-11 string codes).
    if _col(cr, 'res_partner', 'expense_type') and not _col(cr, 'res_partner', 'l10n_do_expense_type'):
        cr.execute("ALTER TABLE res_partner RENAME COLUMN expense_type TO l10n_do_expense_type")
        _logger.info("  Renamed res_partner.expense_type → l10n_do_expense_type")


# ---------------------------------------------------------------------------
# 2. account.move  (custom ncf_manager columns now orphaned after core migration)
#    anulation_type → l10n_do_cancellation_type   (values 01-10, direct)
#    income_type    → l10n_do_income_type          (values 01-06, direct)
#    expense_type   → l10n_do_expense_type         (values 01-11, direct)
#    reference/ref  → l10n_do_fiscal_number        (copy NCF string)
# ---------------------------------------------------------------------------

def _migrate_account_move(cr):
    _logger.info("Migrating account.move custom ncf_manager fields")

    # Simple renames – values are directly compatible between v12 and v17
    renames = [
        ('anulation_type', 'l10n_do_cancellation_type'),
        ('income_type',    'l10n_do_income_type'),
        ('expense_type',   'l10n_do_expense_type'),
    ]
    for old_col, new_col in renames:
        if not _col(cr, 'account_move', old_col):
            continue
        if _col(cr, 'account_move', new_col):
            # Target already exists (e.g. partial migration): fill only gaps
            cr.execute(f"""
                UPDATE account_move SET {new_col} = {old_col}
                 WHERE {old_col} IS NOT NULL AND {new_col} IS NULL
            """)
            _logger.info("  account_move: filled %s from %s: %d rows", new_col, old_col, cr.rowcount)
        else:
            cr.execute(f"ALTER TABLE account_move RENAME COLUMN {old_col} TO {new_col}")
            _logger.info("  account_move: renamed %s → %s", old_col, new_col)

    # l10n_do_fiscal_number: copy NCF stored in ref/reference
    # In v12, ncf_manager stored the NCF in account.invoice.reference which
    # Odoo's core migrator copies into account_move.ref.
    # Pattern: paper NCF = B + 2 digits + 8 digits; e-CF = E + 2 digits + 10 digits.
    if not _col(cr, 'account_move', 'l10n_do_fiscal_number'):
        cr.execute("ALTER TABLE account_move ADD COLUMN l10n_do_fiscal_number VARCHAR")
        _logger.info("  account_move: added l10n_do_fiscal_number column")

    # NCF origen: account_invoice.reference (existe durante la migración).
    # Dos pasadas porque no todos los IDs coinciden entre account_invoice y account_move:
    #   Pasada 1: JOIN por id (migración preservó el id)
    #   Pasada 2: JOIN por number=name (migración asignó nuevo id en account_move)
    # Solo se copian valores únicos (duplicados = datos inválidos en v12, se omiten).
    ncf_regex = r'^[BbEe]\d{2}\d{6,10}$'
    if _col(cr, 'account_invoice', 'reference'):
        cr.execute("""
            UPDATE account_move am
               SET l10n_do_fiscal_number = UPPER(ai.reference)
              FROM account_invoice ai
             WHERE ai.id = am.id
               AND ai.reference ~ %s
               AND am.l10n_do_fiscal_number IS NULL
               AND am.move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
               AND 1 = (SELECT COUNT(*) FROM account_invoice
                         WHERE reference = ai.reference)
        """, (ncf_regex,))
        pass1 = cr.rowcount
        cr.execute("""
            UPDATE account_move am
               SET l10n_do_fiscal_number = UPPER(ai.reference)
              FROM account_invoice ai
             WHERE ai.number = am.name
               AND ai.number IS NOT NULL AND ai.number != ''
               AND ai.reference ~ %s
               AND am.l10n_do_fiscal_number IS NULL
               AND am.move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
               AND NOT EXISTS (
                 SELECT 1 FROM account_invoice ai2 WHERE ai2.id = am.id)
               AND 1 = (SELECT COUNT(*) FROM account_invoice
                         WHERE reference = ai.reference)
        """, (ncf_regex,))
        _logger.info("  account_move: NCF → l10n_do_fiscal_number: %d (by id) + %d (by name) rows",
                     pass1, cr.rowcount)
    elif _col(cr, 'account_move', 'ref'):
        cr.execute("""
            UPDATE account_move am
               SET l10n_do_fiscal_number = UPPER(am.ref)
              FROM (
                SELECT ref, company_id FROM account_move
                 WHERE move_type IN ('in_invoice','in_refund')
                   AND ref ~ %s AND l10n_do_fiscal_number IS NULL
                 GROUP BY ref, company_id HAVING COUNT(*) = 1
              ) uniq
             WHERE am.ref = uniq.ref AND am.company_id = uniq.company_id
               AND am.move_type IN ('in_invoice','in_refund')
               AND am.l10n_do_fiscal_number IS NULL
        """, (ncf_regex,))
        if cr.rowcount:
            _logger.info("  account_move: NCF ref → l10n_do_fiscal_number (fallback vendor): %d rows",
                         cr.rowcount)


# ---------------------------------------------------------------------------
# 3. account.journal
#    ncf_control (v12 bool) → l10n_latam_use_documents (v17 bool)
#    payment_form (codes '01'-'07') → l10n_do_payment_form (string keys)
#
#    IMPORTANT: l10n_latam_use_documents MUST be set here via raw SQL, before
#    the ORM loads.  In v17 the ORM blocks changing this field once validated
#    invoices exist on the journal.  Pre-migrate bypasses that constraint.
# ---------------------------------------------------------------------------

def _migrate_account_journal(cr):
    _logger.info("Migrating account.journal fields")

    # 3a) Activate l10n_latam_use_documents on fiscal Dominican journals.
    #
    # In v12 ncf_manager, ncf_control was a RELATED (non-stored) field on account.journal
    # pointing to ir_sequence.ncf_control — it never existed as a column on account_journal.
    # We activate use_documents via the ir_sequence link for sale journals, and via
    # purchase_type for purchase journals.
    #
    # IMPORTANT: l10n_latam_use_documents must be set via raw SQL here (pre-migrate).
    # The v17 ORM blocks changing it once validated invoices exist on the journal.

    # Sale journals: activate if the linked sequence has ncf_control=True
    if _col(cr, 'ir_sequence', 'ncf_control'):
        cr.execute("""
            UPDATE account_journal aj
               SET l10n_latam_use_documents = TRUE
              FROM ir_sequence irs
             WHERE irs.id = aj.sequence
               AND irs.ncf_control = TRUE
               AND aj.type = 'sale'
               AND (aj.l10n_latam_use_documents IS NULL OR aj.l10n_latam_use_documents = FALSE)
        """)
        if cr.rowcount:
            _logger.info("  ir_sequence.ncf_control=True → l10n_latam_use_documents=True: %d sale journals",
                         cr.rowcount)

    # Purchase journals: activate if purchase_type indicates a fiscal NCF type
    if _col(cr, 'account_journal', 'purchase_type'):
        cr.execute("""
            UPDATE account_journal
               SET l10n_latam_use_documents = TRUE
             WHERE type = 'purchase'
               AND purchase_type IN ('normal', 'minor', 'informal', 'exterior', 'import')
               AND (l10n_latam_use_documents IS NULL OR l10n_latam_use_documents = FALSE)
        """)
        if cr.rowcount:
            _logger.info("  purchase_type fiscal → l10n_latam_use_documents=True: %d purchase journals",
                         cr.rowcount)

    # Fallback: activate all Dominican sale/purchase journals that are still unset.
    # Covers cases where ncf_control or purchase_type columns are absent.
    cr.execute("""
        UPDATE account_journal aj
           SET l10n_latam_use_documents = TRUE
          FROM res_company rc
          JOIN res_partner rp ON rp.id = rc.partner_id
         WHERE aj.company_id = rc.id
           AND rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
           AND aj.type IN ('sale', 'purchase')
           AND (aj.l10n_latam_use_documents IS NULL OR aj.l10n_latam_use_documents = FALSE)
    """)
    if cr.rowcount:
        _logger.info("  fallback: activated l10n_latam_use_documents on %d additional DO journals",
                     cr.rowcount)

    # 3b) payment_form → l10n_do_payment_form
    #
    # In v12, account.journal.payment_form ALREADY uses string keys ('cash', 'bank', etc.).
    # This is a simple column rename — NO value conversion needed.
    if not _col(cr, 'account_journal', 'payment_form'):
        return
    if _col(cr, 'account_journal', 'l10n_do_payment_form'):
        # Target already exists: merge non-null values
        cr.execute("""
            UPDATE account_journal
               SET l10n_do_payment_form = payment_form
             WHERE payment_form IS NOT NULL AND l10n_do_payment_form IS NULL
        """)
        if cr.rowcount:
            _logger.info("  journal payment_form merged into l10n_do_payment_form: %d rows", cr.rowcount)
    else:
        cr.execute("ALTER TABLE account_journal RENAME COLUMN payment_form TO l10n_do_payment_form")
        _logger.info("  Renamed account_journal.payment_form → l10n_do_payment_form")


# ---------------------------------------------------------------------------
# 4. account.tax
#    purchase_tax_type (v12 ncf_manager field) → l10n_do_tax_type (v17)
#    This rename is needed here so dgii_reports migration finds the column
#    under the expected v17 name.
# ---------------------------------------------------------------------------

def _migrate_account_tax(cr):
    _logger.info("Migrating account.tax.purchase_tax_type → l10n_do_tax_type")

    if not _col(cr, 'account_tax', 'purchase_tax_type'):
        _logger.info("  purchase_tax_type absent – already migrated or not present")
        return

    if not _col(cr, 'account_tax', 'l10n_do_tax_type'):
        cr.execute("ALTER TABLE account_tax ADD COLUMN l10n_do_tax_type VARCHAR DEFAULT 'none'")

    # v12 purchase_tax_type actual values (from ncf_manager source):
    #   itbis, ritbis, isr, rext, none
    # Values are directly compatible with v17 l10n_do_tax_type — direct copy suffices.
    tax_map = [
        ('itbis',  'itbis'),
        ('ritbis', 'ritbis'),
        ('isr',    'isr'),
        ('rext',   'rext'),
        ('none',   'none'),
    ]
    total = 0
    for old, new in tax_map:
        cr.execute("""
            UPDATE account_tax
               SET l10n_do_tax_type = %s
             WHERE purchase_tax_type = %s
               AND (l10n_do_tax_type IS NULL OR l10n_do_tax_type = 'none')
        """, (new, old))
        if cr.rowcount:
            total += cr.rowcount
    _logger.info("  account.tax: %d rows mapped from purchase_tax_type → l10n_do_tax_type", total)

    # Ensure no NULLs remain (catches any unrecognised values)
    cr.execute("""
        UPDATE account_tax SET l10n_do_tax_type = 'none'
         WHERE l10n_do_tax_type IS NULL
    """)


# ---------------------------------------------------------------------------
# 5. Delete obsolete ncf_manager views
#    The v17 XML files will recreate them. If we leave the old ones in place
#    the view inheritance will conflict.
# ---------------------------------------------------------------------------

def _delete_ncf_manager_views(cr):
    _logger.info("Deleting obsolete ncf_manager views")

    for module in ('ncf_manager', 'l10n_do_accounting'):
        cr.execute("""
            SELECT v.id FROM ir_ui_view v
              JOIN ir_model_data d ON d.model = 'ir.ui.view' AND d.res_id = v.id
             WHERE d.module = %s
        """, (module,))
        view_ids = [r[0] for r in cr.fetchall()]
        if not view_ids:
            continue

        # Remove child views from any module inheriting these
        cr.execute("""
            SELECT id FROM ir_ui_view WHERE inherit_id = ANY(%s)
        """, (view_ids,))
        child_ids = [r[0] for r in cr.fetchall()]
        if child_ids:
            cr.execute("DELETE FROM ir_model_data WHERE model='ir.ui.view' AND res_id = ANY(%s)",
                       (child_ids,))
            cr.execute("DELETE FROM ir_ui_view WHERE id = ANY(%s)", (child_ids,))

        cr.execute("DELETE FROM ir_model_data WHERE module=%s AND model='ir.ui.view'", (module,))
        cr.execute("DELETE FROM ir_ui_view WHERE id = ANY(%s)", (view_ids,))
        _logger.info("  Deleted %d views for module '%s'", len(view_ids), module)

    # Clean up stale action/menu entries registered under ncf_manager
    cr.execute("""
        DELETE FROM ir_model_data
         WHERE module = 'ncf_manager'
           AND model IN ('ir.actions.act_window','ir.ui.menu',
                         'ir.actions.server','ir.rule','res.groups')
    """)
    if cr.rowcount:
        _logger.info("  Removed %d stale ncf_manager XML-ID entries", cr.rowcount)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _col(cr, table, column):
    """Return True if the column exists in the table."""
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


