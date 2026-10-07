# -*- coding: utf-8 -*-
"""Post-migrate 17.0.1.0.8 — Fix invoice name sequences and draft NCFs.

Two issues found after 17.0.1.0.7 on databases migrated from v12:

1. account_move.name still in v12 format (e.g. 'B00003163') for DO sale
   journals — causes AttributeError in sequence_mixin when posting new invoices.

2. Draft invoices (name='/', amount=0) with NCFs assigned — they were never
   confirmed in v12 but the migration gave them a NCF, blocking the sequence
   counter and causing duplicate-key errors.

This script fixes both issues.
"""

import logging
import os

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("=" * 70)
    _logger.info("POST-MIGRATE 17.0.1.0.8 — Fix invoice names and draft NCFs")
    _logger.info("=" * 70)

    _clear_draft_ncf(cr)
    _fix_invoice_name_sequence(cr)
    _deactivate_orphan_views(cr)

    _logger.info("POST-MIGRATE 17.0.1.0.8 done")


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
        _logger.info("  No journals with v12-format invoice names found — nothing to do")
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
