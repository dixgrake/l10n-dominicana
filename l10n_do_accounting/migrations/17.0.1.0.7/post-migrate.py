# -*- coding: utf-8 -*-
"""Post-migrate 17.0.1.0.7 — NCF full correction for out_invoice records.

Previous post-migrate (17.0.1.0.6) only fixed out_invoice records with an
E-type NCF.  Records with wrong B-type NCFs (wrong prefix or wrong serial
number) and NULL NCF records with duplicate payment_references were left
broken.

This script corrects ALL out_invoice / out_refund records in a single unified
pass so that v12 duplicate NCFs (two invoices with the same reference) are
handled correctly across both the "wrong value" and "NULL value" cases.
"""

import logging
import os

from odoo import api, SUPERUSER_ID
from odoo.tools.config import config as odoo_config

_logger = logging.getLogger(__name__)

_NCF_B   = r'^[Bb][0-9]{10}$'
_NCF_ANY = r'^[BbEe][0-9]{2}[0-9]{6,10}$'


def migrate(cr, version):
    _logger.info("=" * 70)
    _logger.info("POST-MIGRATE 17.0.1.0.7 — NCF full correction")
    _logger.info("=" * 70)

    env = api.Environment(cr, SUPERUSER_ID, {})

    _fix_all_out_invoice_ncf(cr)
    _clear_draft_ncf(cr)
    _safety_net_in_invoices(cr)
    _assign_document_types(cr, env)
    _set_manual_document_number(cr)
    _recompute_split_sequences(cr)
    _fix_invoice_name_sequence(cr)
    _deactivate_orphan_views(cr)
    _report(cr)

    _logger.info("POST-MIGRATE 17.0.1.0.7 done")


# ---------------------------------------------------------------------------
# Unified out_invoice NCF correction
# ---------------------------------------------------------------------------

def _fix_all_out_invoice_ncf(cr):
    """Restore l10n_do_fiscal_number from payment_reference for ALL out_invoice
    records that are wrong or missing.

    Processes both cases in a single ordered pass so that v12 duplicate NCFs
    (two invoices sharing the same payment_reference) are handled correctly:
    the lowest-id record always gets the original NCF; later duplicates are
    renumbered to the next available sequential NCF in the same prefix series.

    This unified approach prevents the split-function conflict where one pass
    restores a value and the next pass (for NULL records) then finds that value
    already taken.
    """
    _logger.info("--- Correcting all out_invoice NCFs from payment_reference ---")

    # All records that need to be set (wrong value OR NULL/empty)
    cr.execute("""
        SELECT id, company_id, payment_reference
          FROM account_move
         WHERE move_type IN ('out_invoice', 'out_refund')
           AND payment_reference ~ %s
           AND (
               l10n_do_fiscal_number IS NULL
               OR l10n_do_fiscal_number = ''
               OR l10n_do_fiscal_number != UPPER(payment_reference)
           )
         ORDER BY id
    """, (_NCF_B,))
    records = cr.fetchall()

    if not records:
        _logger.info("  All out_invoice NCFs already correct")
    else:
        _logger.info("  out_invoice records to correct: %d", len(records))

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
# Clear NCF from empty draft invoices (they block the sequence)
# ---------------------------------------------------------------------------

def _clear_draft_ncf(cr):
    """Remove l10n_do_fiscal_number from unconfirmed empty draft invoices.

    In v12, drafts could have NCFs pre-assigned.  In v17 the NCF is only
    committed when the invoice is posted.  Empty drafts (name='/', amount=0)
    that were never confirmed in v12 must not hold a NCF after migration —
    they would block the sequence counter and cause a duplicate-key error
    when the next invoice tries to use that number.
    """
    cr.execute("""
        UPDATE account_move
           SET l10n_do_fiscal_number  = NULL,
               l10n_do_sequence_prefix = NULL,
               l10n_do_sequence_number = 0
         WHERE state    = 'draft'
           AND move_type IN ('out_invoice','out_refund')
           AND name     = '/'
           AND (amount_total = 0 OR amount_total IS NULL)
           AND l10n_do_fiscal_number IS NOT NULL
           AND l10n_do_fiscal_number != ''
    """)
    if cr.rowcount:
        _logger.info(
            "  Cleared NCF from %d empty draft out_invoice/out_refund records",
            cr.rowcount,
        )


# ---------------------------------------------------------------------------
# Safety net — vendor invoices only (no unique constraint)
# ---------------------------------------------------------------------------

def _safety_net_in_invoices(cr):
    """Fill NULL l10n_do_fiscal_number for vendor invoices from ref.

    In-invoice uniqueness is not enforced at DB level (different suppliers
    can legitimately share the same NCF), so a bulk UPDATE is safe here.
    """
    cr.execute("""
        UPDATE account_move
           SET l10n_do_fiscal_number = UPPER(ref)
         WHERE move_type IN ('in_invoice', 'in_refund')
           AND ref ~ %s
           AND (l10n_do_fiscal_number IS NULL OR l10n_do_fiscal_number = '')
    """, (_NCF_ANY,))
    if cr.rowcount:
        _logger.info("  Vendor invoice safety net: %d records filled", cr.rowcount)


# ---------------------------------------------------------------------------
# Assign document types
# ---------------------------------------------------------------------------

def _assign_document_types(cr, env):
    doc_types = env['l10n_latam.document.type'].search([
        ('country_id.code', '=', 'DO'),
        ('doc_code_prefix', '!=', False),
    ])
    if not doc_types:
        _logger.warning("  No Dominican document types found")
        return

    prefix_map = {dt.doc_code_prefix: dt.id for dt in doc_types}
    _logger.info("--- Assigning document types (prefixes: %s) ---", sorted(prefix_map))

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


# ---------------------------------------------------------------------------
# Mark vendor bills as manual
# ---------------------------------------------------------------------------

def _set_manual_document_number(cr):
    cr.execute("""
        UPDATE account_move
           SET l10n_latam_manual_document_number = TRUE
         WHERE move_type IN ('in_invoice', 'in_refund')
           AND l10n_latam_document_type_id IS NOT NULL
           AND (l10n_latam_manual_document_number IS NULL
                OR l10n_latam_manual_document_number = FALSE)
    """)
    if cr.rowcount:
        _logger.info("  Vendor bills marked manual: %d", cr.rowcount)


# ---------------------------------------------------------------------------
# Recompute stored split-sequence fields
# ---------------------------------------------------------------------------

def _recompute_split_sequences(cr):
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


# ---------------------------------------------------------------------------
# Fix invoice name sequences (v12 B00XXXXXX → v17 CODE/YYYY/NNNNN)
# ---------------------------------------------------------------------------

def _fix_invoice_name_sequence(cr):
    """Rewrite account_move.name for DO sale journals from v12 format to v17.

    In v12, account_invoice.move_name stored a sequential number like
    'B00003163'.  After the core v12→v17 migration this value landed in
    account_move.name.  Odoo v17's sequence mixin expects name to follow
    the journal-code/year/seq pattern (e.g. 'VNT/2026/00272').  Without
    this fix, posting a new invoice raises:
        AttributeError: 'NoneType' object has no attribute 'groupdict'
    because _deduce_sequence_number_reset() cannot match the old format.

    For each DO sale journal (l10n_latam_use_documents=True) we assign
    names partitioned by year and ordered by the original v12 name so
    that the relative order of invoices within each year is preserved.
    """
    _logger.info("--- Fixing invoice name sequences (v12 → v17 format) ---")

    # Find DO sale journals with l10n_latam_use_documents
    cr.execute("""
        SELECT aj.id, aj.code
          FROM account_journal aj
         WHERE aj.type = 'sale'
           AND aj.l10n_latam_use_documents = TRUE
           AND EXISTS (
               SELECT 1 FROM account_move am
                WHERE am.journal_id = aj.id
                  AND am.name IS NOT NULL
                  AND am.name != '/'
                  AND am.name ~ '^[Bb][0-9]{8,}$'
           )
    """)
    journals = cr.fetchall()

    if not journals:
        _logger.info("  No journals with v12-format invoice names found")
        return

    _logger.info("  Journals to fix: %d", len(journals))
    total_fixed = 0

    for journal_id, journal_code in journals:
        code = (journal_code or 'VNT').upper()

        cr.execute("""
            WITH ranked AS (
                SELECT
                    am.id,
                    %s || '/' || EXTRACT(YEAR FROM am.date)::int || '/'
                        || LPAD(
                            ROW_NUMBER() OVER (
                                PARTITION BY EXTRACT(YEAR FROM am.date)
                                ORDER BY am.name
                            )::text,
                            5, '0'
                        ) AS new_name,
                    %s || '/' || EXTRACT(YEAR FROM am.date)::int || '/'
                        AS new_prefix,
                    ROW_NUMBER() OVER (
                        PARTITION BY EXTRACT(YEAR FROM am.date)
                        ORDER BY am.name
                    )::int AS new_number
                FROM account_move am
                WHERE am.journal_id = %s
                  AND am.name IS NOT NULL
                  AND am.name != '/'
                  AND am.name ~ '^[Bb][0-9]{8,}$'
            )
            UPDATE account_move am
               SET name            = r.new_name,
                   sequence_prefix = r.new_prefix,
                   sequence_number = r.new_number
              FROM ranked r
             WHERE am.id = r.id
        """, (code, code, journal_id))

        count = cr.rowcount
        total_fixed += count
        _logger.info("  Journal %s (id=%d): %d invoices renamed", code, journal_id, count)

    _logger.info("  Total invoice names fixed: %d", total_fixed)


# ---------------------------------------------------------------------------
# Deactivate views from uninstalled modules with missing arch_fs files
# ---------------------------------------------------------------------------

def _deactivate_orphan_views(cr):
    """Deactivate views whose arch_fs points to a module absent from both the
    database and the filesystem.

    Two conditions must both be true to deactivate a view:
      1. The module is NOT in ir_module_module as installed/to_upgrade.
      2. The module directory is not found in any registered addons path
         (uses odoo.addons.__path__ which includes Odoo core paths absent
         from the addons_path config entry, preventing false positives for
         core modules like 'base' or 'web').
    """
    import odoo.addons as _odoo_addons
    all_addons_paths = list(_odoo_addons.__path__)

    cr.execute("SELECT name FROM ir_module_module WHERE state IN ('installed','to upgrade','to install')")
    installed_modules = {row[0] for row in cr.fetchall()}

    cr.execute("SELECT id, arch_fs FROM ir_ui_view WHERE active = TRUE AND arch_fs IS NOT NULL")
    orphan_ids = []
    for view_id, arch_fs in cr.fetchall():
        module = arch_fs.split('/')[0]
        if module in installed_modules:
            continue
        if any(os.path.isdir(os.path.join(p, module)) for p in all_addons_paths):
            continue
        orphan_ids.append(view_id)

    if orphan_ids:
        cr.execute(
            "UPDATE ir_ui_view SET active = FALSE WHERE id = ANY(%s)",
            (orphan_ids,),
        )
        _logger.warning(
            "  Deactivated %d orphan view(s) from modules missing in DB and filesystem",
            len(orphan_ids),
        )
    else:
        _logger.info("  No orphan views found")


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def _report(cr):
    _logger.info("=" * 70)
    _logger.info("REPORT 17.0.1.0.7 — NCF correction summary")
    _logger.info("=" * 70)

    cr.execute("""
        SELECT move_type,
               COUNT(*)                                                   AS total,
               COUNT(l10n_do_fiscal_number)
                   FILTER (WHERE l10n_do_fiscal_number != '')              AS with_ncf,
               COUNT(l10n_latam_document_type_id)                          AS with_doc_type
          FROM account_move
         WHERE move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
         GROUP BY move_type
         ORDER BY move_type
    """)
    _logger.info("  %-15s %7s %8s %9s", "move_type", "total", "with_ncf", "doc_type")
    for row in cr.fetchall():
        _logger.info("  %-15s %7d %8d %9d", *row)

    # Verify no remaining mismatches for out_invoices
    cr.execute("""
        SELECT COUNT(*) FROM account_move
         WHERE move_type IN ('out_invoice','out_refund')
           AND payment_reference ~ %s
           AND l10n_do_fiscal_number IS NOT NULL AND l10n_do_fiscal_number != ''
           AND l10n_do_fiscal_number != UPPER(payment_reference)
    """, (_NCF_B,))
    remaining = cr.fetchone()[0]
    if remaining:
        _logger.warning("  ⚠ Still %d mismatched out_invoice NCFs after fix!", remaining)
    else:
        _logger.info("  ✓ All out_invoice NCFs match payment_reference")

    _logger.info("=" * 70)
