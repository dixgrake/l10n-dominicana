# -*- coding: utf-8 -*-
# Part of Domincana Premium. See LICENSE file for full copyright and licensing details.
"""post_init_hook for l10n_do_accounting.

When l10n_do_accounting is installed fresh on a database that was previously
running ncf_manager (v12), Odoo installs the module as 'to install' instead
of 'to upgrade', so the migration scripts in the migrations/ directory are
never called.  This hook bridges that gap: it runs the same data-migration
logic on first install so that existing v12 data is correctly mapped to the
v17 field names.

The hook is idempotent — it checks whether each source column still exists
before acting, so it is safe to run on a clean database (columns absent →
nothing happens) or after the migration scripts already ran.
"""

import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# v12 ncf_manager sale_fiscal_type → doc_code_prefix (fallback when NCF is absent)
# Values confirmed from ncf_manager/models/account_invoice.py source.
# Only applies to out_invoice; never use for vendor bills.
_SALE_FISCAL_TYPE_TO_PREFIX = {
    'final':   'B02',
    'fiscal':  'B01',
    'gov':     'B15',
    'special': 'B14',
    'unico':   'B12',
    'export':  'B16',
}

# v12 account.journal.purchase_type → doc_code_prefix
# Applies to in_invoice only.
_PURCHASE_TYPE_TO_PREFIX = {
    'normal':   'B01',
    'minor':    'B13',
    'informal': 'B11',
    'exterior': 'B17',
    'import':   'B16',
}


def post_init_hook(env):
    cr = env.cr
    _logger.info("START post_init_hook l10n_do_accounting (ncf_manager v12 data migration)")

    _hook_migrate_res_partner(cr)
    _hook_migrate_account_move(cr)
    _hook_migrate_account_journal(cr)
    _hook_migrate_account_tax(cr)

    # Invalidate ORM cache so the journal search below sees the raw SQL updates
    # we just applied to l10n_latam_use_documents.
    env.invalidate_all()

    _hook_fix_duplicate_ncf(cr)
    _hook_assign_document_types(cr, env)
    _hook_set_manual_document_number(cr)
    _hook_populate_document_numbers(cr)
    _hook_create_journal_document_types(env)

    _logger.info("DONE  post_init_hook l10n_do_accounting")


# ---------------------------------------------------------------------------
# res.partner
# ---------------------------------------------------------------------------

def _hook_migrate_res_partner(cr):
    if not _col(cr, 'res_partner', 'sale_fiscal_type'):
        return
    _logger.info("hook: migrating res.partner fields from ncf_manager v12")

    if not _col(cr, 'res_partner', 'l10n_do_dgii_tax_payer_type'):
        cr.execute("ALTER TABLE res_partner ADD COLUMN l10n_do_dgii_tax_payer_type VARCHAR")

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
            UPDATE res_partner SET l10n_do_dgii_tax_payer_type = %s
             WHERE sale_fiscal_type = %s AND l10n_do_dgii_tax_payer_type IS NULL
        """, (new, old))
        total += cr.rowcount

    cr.execute("""
        UPDATE res_partner SET l10n_do_dgii_tax_payer_type = 'non_payer'
         WHERE sale_fiscal_type IS NOT NULL AND l10n_do_dgii_tax_payer_type IS NULL
    """)
    if cr.rowcount:
        _logger.warning("  %d partners had unrecognised sale_fiscal_type → 'non_payer'", cr.rowcount)
    _logger.info("  Migrated %d partner payer-type rows", total)

    if _col(cr, 'res_partner', 'expense_type') and not _col(cr, 'res_partner', 'l10n_do_expense_type'):
        cr.execute("ALTER TABLE res_partner RENAME COLUMN expense_type TO l10n_do_expense_type")
        _logger.info("  Renamed res_partner.expense_type → l10n_do_expense_type")


# ---------------------------------------------------------------------------
# account.move
# ---------------------------------------------------------------------------

def _hook_migrate_account_move(cr):
    renames = [
        ('anulation_type', 'l10n_do_cancellation_type'),
        ('income_type',    'l10n_do_income_type'),
        ('expense_type',   'l10n_do_expense_type'),
    ]
    renamed = False
    for old_col, new_col in renames:
        if not _col(cr, 'account_move', old_col):
            continue
        renamed = True
        if _col(cr, 'account_move', new_col):
            cr.execute(f"""
                UPDATE account_move SET {new_col} = {old_col}
                 WHERE {old_col} IS NOT NULL AND {new_col} IS NULL
            """)
        else:
            cr.execute(f"ALTER TABLE account_move RENAME COLUMN {old_col} TO {new_col}")
    if renamed:
        _logger.info("hook: account.move ncf_manager columns renamed/merged")

    if not _col(cr, 'account_move', 'l10n_do_fiscal_number'):
        cr.execute("ALTER TABLE account_move ADD COLUMN l10n_do_fiscal_number VARCHAR")

    # NCF origen: account_invoice.reference. Dos pasadas por inconsistencia de IDs.
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
        _logger.info("hook: NCF → l10n_do_fiscal_number: %d (by id) + %d (by name) rows",
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
            _logger.info("hook: NCF ref → l10n_do_fiscal_number (fallback vendor): %d rows",
                         cr.rowcount)


# ---------------------------------------------------------------------------
# account.journal
# ---------------------------------------------------------------------------

def _hook_migrate_account_journal(cr):
    # ncf_control in v12 is a RELATED (non-stored) field on account.journal — it was
    # never a column on the account_journal table. Activate via ir_sequence link.
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
            _logger.info("hook: ir_sequence.ncf_control=True → l10n_latam_use_documents=True: %d journals",
                         cr.rowcount)

    if _col(cr, 'account_journal', 'purchase_type'):
        cr.execute("""
            UPDATE account_journal
               SET l10n_latam_use_documents = TRUE
             WHERE type = 'purchase'
               AND purchase_type IN ('normal', 'minor', 'informal', 'exterior', 'import')
               AND (l10n_latam_use_documents IS NULL OR l10n_latam_use_documents = FALSE)
        """)
        if cr.rowcount:
            _logger.info("hook: purchase_type fiscal → l10n_latam_use_documents=True: %d journals",
                         cr.rowcount)

    # Fallback: all Dominican sale/purchase journals
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
        _logger.info("hook: fallback activated l10n_latam_use_documents on %d DO journals", cr.rowcount)

    # payment_form → l10n_do_payment_form
    # In v12, payment_form already uses string keys — simple rename, no value conversion.
    if not _col(cr, 'account_journal', 'payment_form'):
        return
    if _col(cr, 'account_journal', 'l10n_do_payment_form'):
        cr.execute("""
            UPDATE account_journal
               SET l10n_do_payment_form = payment_form
             WHERE payment_form IS NOT NULL AND l10n_do_payment_form IS NULL
        """)
        if cr.rowcount:
            _logger.info("hook: journal payment_form merged: %d rows", cr.rowcount)
    else:
        cr.execute("ALTER TABLE account_journal RENAME COLUMN payment_form TO l10n_do_payment_form")
        _logger.info("hook: renamed account_journal.payment_form → l10n_do_payment_form")


# ---------------------------------------------------------------------------
# account.tax
# ---------------------------------------------------------------------------

def _hook_migrate_account_tax(cr):
    if not _col(cr, 'account_tax', 'purchase_tax_type'):
        return
    if not _col(cr, 'account_tax', 'l10n_do_tax_type'):
        cr.execute("ALTER TABLE account_tax ADD COLUMN l10n_do_tax_type VARCHAR DEFAULT 'none'")

    # Actual v12 purchase_tax_type values (from ncf_manager source):
    #   itbis, ritbis, isr, rext, none — values are directly compatible with v17
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
            UPDATE account_tax SET l10n_do_tax_type = %s
             WHERE purchase_tax_type = %s
               AND (l10n_do_tax_type IS NULL OR l10n_do_tax_type = 'none')
        """, (new, old))
        total += cr.rowcount
    cr.execute("UPDATE account_tax SET l10n_do_tax_type = 'none' WHERE l10n_do_tax_type IS NULL")
    if total:
        _logger.info("hook: mapped %d account_tax rows purchase_tax_type → l10n_do_tax_type", total)


# ---------------------------------------------------------------------------
# Duplicate NCF fix
# ---------------------------------------------------------------------------

def _hook_fix_duplicate_ncf(cr):
    """Renumber duplicate l10n_do_fiscal_number values to avoid UniqueViolation.

    The unique index account_move_unique_l10n_do_fiscal_number_sales covers
    only non-vendor move types. For each duplicate group, the record with the
    lowest id is kept; subsequent duplicates get a new NCF by incrementing past
    the current maximum sequence for that prefix + company.
    """
    _logger.info("hook: checking for duplicate l10n_do_fiscal_number (sales-side)")
    cr.execute("""
        SELECT l10n_do_fiscal_number, company_id,
               array_agg(id ORDER BY id) AS ids
          FROM account_move
         WHERE l10n_do_fiscal_number IS NOT NULL
           AND l10n_do_fiscal_number != ''
           AND move_type NOT IN ('in_invoice', 'in_refund')
         GROUP BY l10n_do_fiscal_number, company_id
        HAVING COUNT(*) > 1
    """)
    dups = cr.fetchall()
    if not dups:
        _logger.info("hook: no duplicate NCFs found – skipping")
        return

    _logger.info("hook: found %d duplicate NCF group(s) to fix", len(dups))
    fixed = 0

    for ncf, company_id, ids in dups:
        prefix = ncf[:3].upper()  # 'B02', 'E31', etc.
        seq_len = 10 if ncf.upper().startswith('E') else 8

        # Current max sequence for this prefix + company (sales-side)
        cr.execute("""
            SELECT MAX(CAST(SUBSTRING(l10n_do_fiscal_number, 4) AS BIGINT))
              FROM account_move
             WHERE company_id = %s
               AND move_type NOT IN ('in_invoice', 'in_refund')
               AND l10n_do_fiscal_number ~ %s
        """, (company_id, r'^' + prefix + r'\d+$'))
        max_seq = cr.fetchone()[0] or 0

        for inv_id in ids[1:]:  # keep ids[0] unchanged, renumber the rest
            max_seq += 1
            new_ncf = prefix + str(max_seq).zfill(seq_len)
            cr.execute(
                "UPDATE account_move SET l10n_do_fiscal_number = %s WHERE id = %s",
                (new_ncf, inv_id),
            )
            _logger.warning("  NCF duplicate renumbered: %s → %s (move id=%d, company=%d)",
                            ncf, new_ncf, inv_id, company_id)
            fixed += 1

    _logger.info("hook: fixed %d duplicate NCF record(s)", fixed)


# ---------------------------------------------------------------------------
# Document type assignment
# ---------------------------------------------------------------------------

def _hook_assign_document_types(cr, env):
    """Build prefix_map from doc_code_prefix and assign document types.

    Pass 1  – NCF prefix from l10n_do_fiscal_number (all move_types).
    Pass 2a – out_invoice fallback: sale_fiscal_type column.
    Pass 2b – out_refund fallback: default B04.
    Pass 2c – in_invoice fallback: journal.purchase_type column.
    Pass 2d – in_refund fallback: default B04.
    """
    doc_types = env['l10n_latam.document.type'].search([
        ('country_id.code', '=', 'DO'),
        ('doc_code_prefix', '!=', False),
    ])
    prefix_map = {dt.doc_code_prefix: dt.id for dt in doc_types}
    if not prefix_map:
        _logger.warning("hook: no Dominican document types found – skipping assignment")
        return

    # Pass 1: NCF prefix from l10n_do_fiscal_number
    assigned_ncf = 0
    cr.execute("""
        SELECT am.id, LEFT(am.l10n_do_fiscal_number, 3) AS prefix
          FROM account_move am
          JOIN res_company rc ON rc.id = am.company_id
          JOIN res_partner rp ON rp.id = rc.partner_id
         WHERE rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
           AND am.move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
           AND am.l10n_latam_document_type_id IS NULL
           AND am.l10n_do_fiscal_number IS NOT NULL
           AND am.state != 'cancel'
    """)
    for inv_id, prefix in cr.fetchall():
        doc_id = prefix_map.get((prefix or '').upper())
        if doc_id:
            cr.execute(
                "UPDATE account_move SET l10n_latam_document_type_id = %s WHERE id = %s",
                (doc_id, inv_id),
            )
            assigned_ncf += 1
    _logger.info("hook: Pass 1 (NCF prefix): assigned %d invoices", assigned_ncf)

    # Pass 2a: out_invoice fallback via sale_fiscal_type (customer invoices only)
    if _col(cr, 'account_move', 'sale_fiscal_type'):
        assigned_sft = 0
        cr.execute("""
            SELECT am.id, am.sale_fiscal_type
              FROM account_move am
              JOIN res_company rc ON rc.id = am.company_id
              JOIN res_partner rp ON rp.id = rc.partner_id
             WHERE rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
               AND am.move_type = 'out_invoice'
               AND am.l10n_latam_document_type_id IS NULL
               AND am.sale_fiscal_type IS NOT NULL
               AND am.state != 'cancel'
        """)
        for inv_id, sale_fiscal_type in cr.fetchall():
            prefix = _SALE_FISCAL_TYPE_TO_PREFIX.get(sale_fiscal_type)
            doc_id = prefix_map.get(prefix) if prefix else None
            if doc_id:
                cr.execute(
                    "UPDATE account_move SET l10n_latam_document_type_id = %s WHERE id = %s",
                    (doc_id, inv_id),
                )
                assigned_sft += 1
        _logger.info("hook: Pass 2a (out_invoice/sale_fiscal_type): assigned %d invoices", assigned_sft)

    # Pass 2b: out_refund fallback → B04 (Nota de Crédito)
    b04_id = prefix_map.get('B04')
    if b04_id:
        cr.execute("""
            UPDATE account_move am
               SET l10n_latam_document_type_id = %s
              FROM res_company rc
              JOIN res_partner rp ON rp.id = rc.partner_id
             WHERE am.company_id = rc.id
               AND rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
               AND am.move_type = 'out_refund'
               AND am.l10n_latam_document_type_id IS NULL
               AND am.state != 'cancel'
        """, (b04_id,))
        if cr.rowcount:
            _logger.info("hook: Pass 2b (out_refund → B04): assigned %d invoices", cr.rowcount)

    # Pass 2c: in_invoice fallback via journal.purchase_type
    if _col(cr, 'account_journal', 'purchase_type'):
        assigned_pt = 0
        cr.execute("""
            SELECT am.id, aj.purchase_type
              FROM account_move am
              JOIN account_journal aj ON aj.id = am.journal_id
              JOIN res_company rc ON rc.id = am.company_id
              JOIN res_partner rp ON rp.id = rc.partner_id
             WHERE rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
               AND am.move_type = 'in_invoice'
               AND am.l10n_latam_document_type_id IS NULL
               AND aj.purchase_type IS NOT NULL
               AND am.state != 'cancel'
        """)
        for inv_id, purchase_type in cr.fetchall():
            prefix = _PURCHASE_TYPE_TO_PREFIX.get(purchase_type)
            doc_id = prefix_map.get(prefix) if prefix else None
            if doc_id:
                cr.execute(
                    "UPDATE account_move SET l10n_latam_document_type_id = %s WHERE id = %s",
                    (doc_id, inv_id),
                )
                assigned_pt += 1
        _logger.info("hook: Pass 2c (in_invoice/purchase_type): assigned %d invoices", assigned_pt)

    # Pass 2d: in_refund fallback → B04 (Nota de Crédito)
    if b04_id:
        cr.execute("""
            UPDATE account_move am
               SET l10n_latam_document_type_id = %s
              FROM res_company rc
              JOIN res_partner rp ON rp.id = rc.partner_id
             WHERE am.company_id = rc.id
               AND rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
               AND am.move_type = 'in_refund'
               AND am.l10n_latam_document_type_id IS NULL
               AND am.state != 'cancel'
        """, (b04_id,))
        if cr.rowcount:
            _logger.info("hook: Pass 2d (in_refund → B04): assigned %d invoices", cr.rowcount)


# ---------------------------------------------------------------------------
# Manual document number
# ---------------------------------------------------------------------------

def _hook_set_manual_document_number(cr):
    cr.execute("""
        UPDATE account_move am
           SET l10n_latam_manual_document_number = TRUE
          FROM res_company  rc
          JOIN res_partner  rp ON rp.id = rc.partner_id
         WHERE am.company_id = rc.id
           AND rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
           AND am.move_type IN ('in_invoice','in_refund')
           AND am.l10n_latam_document_type_id IS NOT NULL
           AND (am.l10n_latam_manual_document_number IS NULL
                OR am.l10n_latam_manual_document_number = FALSE)
    """)
    if cr.rowcount:
        _logger.info("hook: set l10n_latam_manual_document_number on %d vendor bills", cr.rowcount)


# ---------------------------------------------------------------------------
# Populate l10n_do_fiscal_number (safety net)
# ---------------------------------------------------------------------------

def _hook_populate_document_numbers(cr):
    """Safety net: populate l10n_do_fiscal_number for invoices missed by
    _hook_migrate_account_move.

    Odoo core migration (v12→v14) maps account.invoice.reference by move_type:
      out_invoice / out_refund  → account_move.payment_reference
      in_invoice  / in_refund   → account_move.ref
    """
    if not _col(cr, 'account_move', 'l10n_do_fiscal_number'):
        return
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
        _logger.info("hook: NCF → l10n_do_fiscal_number (safety net): %d (by id) + %d (by name) rows",
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
            _logger.info("hook: NCF ref → l10n_do_fiscal_number (safety net, fallback vendor): %d rows",
                         cr.rowcount)


# ---------------------------------------------------------------------------
# Journal document types
# ---------------------------------------------------------------------------

def _hook_create_journal_document_types(env):
    do_journals = env['account.journal'].search([
        ('company_id.country_id.code', '=', 'DO'),
        ('l10n_latam_use_documents', '=', True),
        ('type', 'in', ('sale', 'purchase')),
        ('l10n_do_document_type_ids', '=', False),
    ])
    if not do_journals:
        return
    created = 0
    for journal in do_journals:
        try:
            journal._l10n_do_create_document_types()
            created += len(journal.l10n_do_document_type_ids)
        except Exception as e:
            _logger.warning("hook: journal '%s' (id=%d): %s", journal.name, journal.id, e)
    _logger.info("hook: created %d journal document type entries", created)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _col(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())
