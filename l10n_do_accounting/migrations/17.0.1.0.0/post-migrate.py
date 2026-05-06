# -*- coding: utf-8 -*-
# Part of Domincana Premium. See LICENSE file for full copyright and licensing details.
"""Post-migration: ncf_manager (v12) → l10n_do_accounting (v17).

Runs AFTER the v17 module is loaded. At this point:
  - All v17 models/columns exist in the database.
  - l10n_latam.document.type records are loaded (from l10n_do data files).
  - Pre-migrate already renamed the custom columns.

Responsibilities:
  1. Assign l10n_latam_document_type_id to invoices using their NCF prefix.
  2. Set l10n_latam_manual_document_number on vendor bills (always manual in v12).
  3. Create l10n_do.account.journal.document_type entries for Dominican journals.
  4. Generate a migration report.
"""

import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# v12 ncf_manager sale_fiscal_type → doc_code_prefix
# Values confirmed from ncf_manager/models/account_invoice.py source:
#   "final"   = Consumo (B02) — NOT "consumo", NOT "consumer"
#   "fiscal"  = Crédito Fiscal (B01)
#   "gov"     = Gubernamentales (B15)
#   "special" = Regímenes Especiales (B14)
#   "unico"   = Único Ingreso (B12)
#   "export"  = Exportaciones (B16)
# Only applies to out_invoice; never to vendor bills.
_SALE_FISCAL_TYPE_TO_PREFIX = {
    'final':   'B02',
    'fiscal':  'B01',
    'gov':     'B15',
    'special': 'B14',
    'unico':   'B12',
    'export':  'B16',
}

# v12 ncf_manager account.journal.purchase_type → doc_code_prefix
# Applies to in_invoice only.
_PURCHASE_TYPE_TO_PREFIX = {
    'normal':   'B01',
    'minor':    'B13',
    'informal': 'B11',
    'exterior': 'B17',
    'import':   'B16',
}


def migrate(cr, version):
    _logger.info("START post-migrate ncf_manager v12 → l10n_do_accounting v17")
    env = api.Environment(cr, SUPERUSER_ID, {})

    prefix_to_doc_type_id = _build_prefix_map(env)
    _fix_duplicate_ncf(cr)
    _assign_document_types(cr, prefix_to_doc_type_id)
    _set_manual_document_number(cr)
    _populate_document_numbers(cr)
    _create_journal_document_types(env)
    _report(cr)

    _logger.info("DONE  post-migrate ncf_manager v12 → l10n_do_accounting v17")


# ---------------------------------------------------------------------------
# 1. Assign l10n_latam_document_type_id
# ---------------------------------------------------------------------------

def _build_prefix_map(env):
    """Build {doc_code_prefix: document_type_id} from loaded v17 data.

    Maps directly via doc_code_prefix (e.g. 'B01', 'B02', 'E31') so the
    first 3 chars of any NCF resolve unambiguously to the correct doc type.
    """
    doc_types = env['l10n_latam.document.type'].search([
        ('country_id.code', '=', 'DO'),
        ('doc_code_prefix', '!=', False),
    ])
    result = {dt.doc_code_prefix: dt.id for dt in doc_types}
    _logger.info("Document type prefix map: %d entries loaded", len(result))
    return result


def _fix_duplicate_ncf(cr):
    """Renumber duplicate l10n_do_fiscal_number values to avoid UniqueViolation.

    The unique index account_move_unique_l10n_do_fiscal_number_sales covers
    only non-vendor move types. For each duplicate group, the record with the
    lowest id is kept; subsequent duplicates get a new NCF by incrementing past
    the current maximum sequence for that prefix + company.
    """
    _logger.info("Checking for duplicate l10n_do_fiscal_number (sales-side)")
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
        _logger.info("  No duplicate NCFs found – skipping")
        return

    _logger.info("  Found %d duplicate NCF group(s) to fix", len(dups))
    fixed = 0

    for ncf, company_id, ids in dups:
        prefix = ncf[:3].upper()
        seq_len = 10 if ncf.upper().startswith('E') else 8

        cr.execute("""
            SELECT MAX(CAST(SUBSTRING(l10n_do_fiscal_number, 4) AS BIGINT))
              FROM account_move
             WHERE company_id = %s
               AND move_type NOT IN ('in_invoice', 'in_refund')
               AND l10n_do_fiscal_number ~ %s
        """, (company_id, r'^' + prefix + r'\d+$'))
        max_seq = cr.fetchone()[0] or 0

        for inv_id in ids[1:]:
            max_seq += 1
            new_ncf = prefix + str(max_seq).zfill(seq_len)
            cr.execute(
                "UPDATE account_move SET l10n_do_fiscal_number = %s WHERE id = %s",
                (new_ncf, inv_id),
            )
            _logger.warning("  NCF duplicate renumbered: %s → %s (move id=%d, company=%d)",
                            ncf, new_ncf, inv_id, company_id)
            fixed += 1

    _logger.info("  Fixed %d duplicate NCF record(s)", fixed)


def _assign_document_types(cr, prefix_map):
    """Set l10n_latam_document_type_id on Dominican invoices that lack it.

    Pass 1 – NCF prefix from l10n_do_fiscal_number (all move_types).
              The first 3 chars of the NCF ARE the doc_code_prefix (e.g. 'B02').
    Pass 2a – out_invoice fallback: v12 sale_fiscal_type column on account_move.
    Pass 2b – out_refund fallback: default to B04 (Nota de Crédito).
    Pass 2c – in_invoice fallback: journal.purchase_type column.
    Pass 2d – in_refund fallback: default to B04 (Nota de Crédito).
    """
    _logger.info("Assigning l10n_latam_document_type_id")

    # Pass 1: NCF prefix → doc_code_prefix (exact match, handles B and E types)
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
    _logger.info("  Pass 1 (NCF prefix): assigned %d invoices", assigned_ncf)

    # Pass 2a: out_invoice fallback via sale_fiscal_type
    # sale_fiscal_type is a customer-invoice-only field — never use it for vendor bills.
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
        _logger.info("  Pass 2a (out_invoice/sale_fiscal_type): assigned %d invoices", assigned_sft)
    else:
        _logger.info("  Pass 2a (sale_fiscal_type): column absent – skipping")

    # Pass 2b: out_refund fallback → B04 (Nota de Crédito a Consumidores)
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
            _logger.info("  Pass 2b (out_refund → B04): assigned %d invoices", cr.rowcount)

    # Pass 2c: in_invoice fallback via journal.purchase_type
    # purchase_type is a stored column on account.journal in v12.
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
        _logger.info("  Pass 2c (in_invoice/purchase_type): assigned %d invoices", assigned_pt)
    else:
        _logger.info("  Pass 2c (purchase_type): column absent – skipping")

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
            _logger.info("  Pass 2d (in_refund → B04): assigned %d invoices", cr.rowcount)

    cr.execute("""
        SELECT COUNT(*) FROM account_move am
          JOIN res_company rc ON rc.id = am.company_id
          JOIN res_partner rp ON rp.id = rc.partner_id
         WHERE rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
           AND am.move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
           AND am.l10n_latam_document_type_id IS NULL
           AND am.state != 'cancel'
    """)
    total_unassigned = cr.fetchone()[0]
    if total_unassigned:
        _logger.warning("  %d invoices still without document_type_id – manual review needed",
                        total_unassigned)


# ---------------------------------------------------------------------------
# 2. l10n_latam_manual_document_number on vendor bills
# ---------------------------------------------------------------------------

def _set_manual_document_number(cr):
    """Vendor bills in v12 always had manually entered NCFs."""
    _logger.info("Setting l10n_latam_manual_document_number on vendor bills")

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
    _logger.info("  Updated %d vendor bill(s)", cr.rowcount)


# ---------------------------------------------------------------------------
# 3. Safety net: ensure l10n_do_fiscal_number is populated
# ---------------------------------------------------------------------------

def _populate_document_numbers(cr):
    """Safety net: populate l10n_do_fiscal_number for invoices missed by pre-migrate.

    Odoo core migration (v12→v14) maps account.invoice.reference by move_type:
      out_invoice / out_refund  → account_move.payment_reference
      in_invoice  / in_refund   → account_move.ref
    """
    _logger.info("Safety net: ensuring l10n_do_fiscal_number populated")

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
        _logger.info("  NCF → l10n_do_fiscal_number (safety net): %d (by id) + %d (by name) rows",
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
            _logger.info("  ref → l10n_do_fiscal_number (safety net, fallback vendor): %d rows",
                         cr.rowcount)


def _col(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


# ---------------------------------------------------------------------------
# 4. Journal document types
# ---------------------------------------------------------------------------

def _create_journal_document_types(env):
    """Populate l10n_do.account.journal.document_type for Dominican journals
    that don't have document type sequences yet.
    """
    _logger.info("Creating journal document types for Dominican journals")

    do_journals = env['account.journal'].search([
        ('company_id.country_id.code', '=', 'DO'),
        ('l10n_latam_use_documents', '=', True),
        ('type', 'in', ('sale', 'purchase')),
        ('l10n_do_document_type_ids', '=', False),
    ])
    _logger.info("  Found %d journals without document types", len(do_journals))

    created = 0
    for journal in do_journals:
        try:
            journal._l10n_do_create_document_types()
            created += len(journal.l10n_do_document_type_ids)
        except Exception as e:
            _logger.warning("  Journal '%s' (id=%d): %s", journal.name, journal.id, e)

    _logger.info("  Created %d document type entries", created)


# ---------------------------------------------------------------------------
# 5. Report
# ---------------------------------------------------------------------------

def _report(cr):
    _logger.info("--- Migration report: l10n_do_accounting v12 → v17 ---")

    cr.execute("""
        SELECT am.move_type,
               COUNT(*)                              AS total,
               COUNT(am.l10n_latam_document_type_id) AS with_doc_type,
               COUNT(am.l10n_do_fiscal_number)        AS with_ncf
          FROM account_move am
          JOIN res_company rc ON rc.id = am.company_id
          JOIN res_partner rp ON rp.id = rc.partner_id
         WHERE rp.country_id = (SELECT id FROM res_country WHERE code = 'DO')
           AND am.move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
         GROUP BY am.move_type ORDER BY am.move_type
    """)
    _logger.info("  %-15s %7s %14s %8s", "move_type", "total", "doc_type_id", "ncf")
    for row in cr.fetchall():
        _logger.info("  %-15s %7d %14d %8d", *row)

    cr.execute("""
        SELECT l10n_do_dgii_tax_payer_type, COUNT(*) AS qty
          FROM res_partner
         WHERE l10n_do_dgii_tax_payer_type IS NOT NULL
         GROUP BY l10n_do_dgii_tax_payer_type ORDER BY qty DESC
    """)
    _logger.info("  Partner payer types:")
    for ptype, qty in cr.fetchall():
        _logger.info("    %-20s : %d", ptype, qty)
