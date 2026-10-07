# -*- coding: utf-8 -*-
"""Pre-migrate: ncf_manager (v12) → l10n_do_accounting (v17)

CONTEXT
-------
The Odoo core migration (v12 → v17) has already run.  Every invoice exists in
account_move with the correct move_type.  The core migration already copied the
NCF from account_invoice.reference into:

  out_invoice / out_refund  →  account_move.payment_reference
  in_invoice  / in_refund   →  account_move.ref

This script copies those values into l10n_do_fiscal_number, which is the v17
field used by l10n_do_accounting.

WHAT THIS SCRIPT DOES
---------------------
1. Creates ncf_migration_backup table for before/after auditing.
2. Snapshots current state (payment_reference / ref as "before" NCF).
3. Creates l10n_do_fiscal_number column if it does not exist yet.
4. Copies NCF to l10n_do_fiscal_number:
     - out_invoice / out_refund: from payment_reference (B-type only)
     - in_invoice  / in_refund:  from ref (B or E type)
5. Renumbers any duplicate NCFs on the sales side and logs changes.

WHAT THIS SCRIPT DOES NOT DO
-----------------------------
× Delete views, models, or fields
× Modify sequences or journals
× Touch res.partner or account.tax
× Rename columns
"""

import logging

_logger = logging.getLogger(__name__)

# B-type NCF: B + 2-digit type code + 8-digit sequence = 11 chars total
# Used only for customer invoices (out types).
_NCF_B = r'^[Bb][0-9]{10}$'

# Any valid NCF: B (11 chars) or E (13 chars).
# Used for vendor invoices (in types) which accept both paper and electronic.
_NCF_ANY = r'^[BbEe][0-9]{2}[0-9]{6,10}$'


def migrate(cr, version):
    if not version:
        return

    _logger.info("=" * 70)
    _logger.info("PRE-MIGRATE  ncf_manager → l10n_do_accounting")
    _logger.info("=" * 70)

    _create_backup_table(cr)
    _snapshot_before(cr)
    _ensure_fiscal_number_column(cr)
    _migrate_out_ncf(cr)
    _migrate_in_ncf(cr)
    _fix_sales_duplicates(cr)

    _logger.info("PRE-MIGRATE done")


# ---------------------------------------------------------------------------
# 1. Backup / audit table
# ---------------------------------------------------------------------------

def _create_backup_table(cr):
    """Permanent audit table: before/after NCF values for every invoice."""
    cr.execute("DROP TABLE IF EXISTS ncf_migration_backup")
    cr.execute("""
        CREATE TABLE ncf_migration_backup (
            move_id         INTEGER      NOT NULL,
            move_type       VARCHAR(32),
            move_name       VARCHAR,
            move_state      VARCHAR(32),
            company_id      INTEGER,
            partner_id      INTEGER,
            -- "before" values (source fields after core v12→v17 migration)
            payment_ref_before  VARCHAR,   -- payment_reference for out types
            ref_before          VARCHAR,   -- ref for in types
            -- "after" values (filled in post-migrate)
            fiscal_after        VARCHAR,   -- l10n_do_fiscal_number after migration
            doc_type_after      INTEGER,   -- l10n_latam_document_type_id after
            -- flags
            renumbered      BOOLEAN  DEFAULT FALSE,
            notes           TEXT,
            snapshot_at     TIMESTAMP DEFAULT NOW()
        )
    """)
    _logger.info("  ncf_migration_backup created")


def _snapshot_before(cr):
    """Capture current state of all invoices before NCF migration."""
    cr.execute("""
        INSERT INTO ncf_migration_backup
            (move_id, move_type, move_name, move_state, company_id, partner_id,
             payment_ref_before, ref_before)
        SELECT
            id, move_type, name, state, company_id, partner_id,
            payment_reference, ref
        FROM account_move
        WHERE move_type IN ('out_invoice','out_refund','in_invoice','in_refund')
    """)
    _logger.info("  Snapshot: %d invoice records", cr.rowcount)


# ---------------------------------------------------------------------------
# 2. Column guard
# ---------------------------------------------------------------------------

def _ensure_fiscal_number_column(cr):
    """Create l10n_do_fiscal_number if the module upgrade hasn't done it yet."""
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name  = 'account_move'
           AND column_name = 'l10n_do_fiscal_number'
    """)
    if not cr.fetchone():
        cr.execute("ALTER TABLE account_move ADD COLUMN l10n_do_fiscal_number VARCHAR")
        _logger.info("  l10n_do_fiscal_number column created")
    else:
        _logger.info("  l10n_do_fiscal_number already exists")


# ---------------------------------------------------------------------------
# 3. NCF migration  (out types)
# ---------------------------------------------------------------------------

def _migrate_out_ncf(cr):
    """Copy payment_reference → l10n_do_fiscal_number for customer invoices.

    Customer NCFs are always B-type (paper comprobante).  E-type prefixes
    belong to vendors; if one appears in payment_reference it is ignored here.
    """
    cr.execute("""
        UPDATE account_move
           SET l10n_do_fiscal_number = UPPER(payment_reference)
         WHERE move_type IN ('out_invoice', 'out_refund')
           AND payment_reference ~ %s
           AND (l10n_do_fiscal_number IS NULL OR l10n_do_fiscal_number = '')
    """, (_NCF_B,))
    _logger.info("  out_invoice/out_refund  NCF set: %d", cr.rowcount)


# ---------------------------------------------------------------------------
# 4. NCF migration  (in types)
# ---------------------------------------------------------------------------

def _migrate_in_ncf(cr):
    """Copy ref → l10n_do_fiscal_number for vendor invoices.

    Vendor invoices accept both B-type (paper) and E-type (electronic).
    Duplicate NCFs across different suppliers are normal; no uniqueness filter.
    """
    cr.execute("""
        UPDATE account_move
           SET l10n_do_fiscal_number = UPPER(ref)
         WHERE move_type IN ('in_invoice', 'in_refund')
           AND ref ~ %s
           AND (l10n_do_fiscal_number IS NULL OR l10n_do_fiscal_number = '')
    """, (_NCF_ANY,))
    _logger.info("  in_invoice/in_refund    NCF set: %d", cr.rowcount)


# ---------------------------------------------------------------------------
# 5. Fix sales-side duplicate NCFs
# ---------------------------------------------------------------------------

def _fix_sales_duplicates(cr):
    """Renumber duplicate NCFs on out_invoice / out_refund.

    The unique index (account_move_unique_l10n_do_fiscal_number_sales) will be
    created by the module upgrade and will reject duplicates on the sales side.
    We resolve them now by keeping the lowest-id record at the original NCF and
    assigning consecutive new NCFs to all remaining duplicates.

    NCF format:
      B-type → prefix(3) + 8 digits  (11 chars total)
      E-type → prefix(3) + 10 digits (13 chars total)

    Changes are logged in ncf_migration_backup with renumbered = TRUE.
    """
    cr.execute("""
        SELECT l10n_do_fiscal_number,
               company_id,
               array_agg(id ORDER BY id) AS ids
          FROM account_move
         WHERE move_type IN ('out_invoice', 'out_refund')
           AND l10n_do_fiscal_number IS NOT NULL
           AND l10n_do_fiscal_number != ''
         GROUP BY l10n_do_fiscal_number, company_id
        HAVING COUNT(*) > 1
    """)
    dups = cr.fetchall()

    if not dups:
        _logger.info("  No sales-side duplicate NCFs")
        return

    _logger.info("  Duplicate NCF groups found: %d", len(dups))
    fixed = 0

    for ncf, company_id, ids in dups:
        prefix   = ncf[:3].upper()
        seq_len  = 10 if prefix.startswith('E') else 8
        total_len = 3 + seq_len

        # Highest existing sequence number for this prefix + company
        cr.execute("""
            SELECT COALESCE(
                MAX(CAST(SUBSTRING(l10n_do_fiscal_number FROM 4) AS BIGINT)), 0)
              FROM account_move
             WHERE company_id = %s
               AND LEFT(l10n_do_fiscal_number, 3) = %s
               AND LENGTH(l10n_do_fiscal_number)  = %s
               AND move_type IN ('out_invoice', 'out_refund')
        """, (company_id, prefix, total_len))
        max_seq = cr.fetchone()[0]

        for dup_id in ids[1:]:   # keep ids[0]; renumber the rest
            max_seq += 1
            new_ncf = prefix + str(max_seq).zfill(seq_len)

            cr.execute(
                "UPDATE account_move SET l10n_do_fiscal_number = %s WHERE id = %s",
                (new_ncf, dup_id),
            )
            cr.execute("""
                UPDATE ncf_migration_backup
                   SET renumbered = TRUE,
                       notes      = COALESCE(notes, '') ||
                                    '[renumbered: ' || %s || ' → ' || %s || '] '
                 WHERE move_id = %s
            """, (ncf, new_ncf, dup_id))

            _logger.warning("  Renumbered id=%d: %s → %s  (company=%d)",
                            dup_id, ncf, new_ncf, company_id)
            fixed += 1

    _logger.info("  Total renumbered: %d", fixed)
