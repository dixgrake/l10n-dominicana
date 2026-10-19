"""Migrate the ncf_manager (Odoo 11/12) data to l10n_do_accounting.

Runs only when the module comes from ncf_manager, renamed by
scripts/migrate_from_v12/00_rename_v12_modules.sql.

The Odoo upgrade service already moved account.invoice to account.move and
kept the ncf_manager columns (sale_fiscal_type, expense_type, payment_form,
...). It also kept the account_invoice table, whose `reference` column holds
the NCF and whose `move_id` points to the matching account.move.

This script runs before the l10n_do_accounting models are loaded. It creates
the new columns itself and fills them with SQL, so that the ORM does not
compute them on every existing record. This matters for
account.move.line.l10n_do_itbis_amount, computed by _compute_totals, which
would recompute the amounts of all the historical lines.
"""
import logging

from odoo.tools.sql import column_exists, create_column, table_exists

_logger = logging.getLogger(__name__)

# B + type (2) + sequence (8) | E + type (2) + sequence (10)
NCF_REGEX = r"^(B[0-9]{10}|E[0-9]{12})$"

# v12 sale_fiscal_type → v17 l10n_do_dgii_tax_payer_type
PARTNER_TAX_PAYER_TYPE = {
    "final": "non_payer",
    "unico": "non_payer",
    "fiscal": "taxpayer",
    "gov": "governmental",
    "special": "special",
    "export": "foreigner",
}

# v12 → v17 l10n_do chart xmlids of account.tax.group (without company prefix)
TAX_GROUP_XMLIDS = {
    "group_itbis": "tax_group_itbis",
    "group_isr": "tax_group_isr",
    "group_ret": "tax_group_ret",
    "group_tax": "tax_group_other_tax",
}

# v12 account.journal.purchase_type values that require an NCF
FISCAL_PURCHASE_TYPES = ("normal", "minor", "informal", "exterior")

NEW_COLUMNS = {
    "account_move": [
        ("l10n_do_fiscal_number", "varchar"),
        ("l10n_do_sequence_prefix", "varchar"),
        ("l10n_do_sequence_number", "int4"),
        ("l10n_latam_manual_document_number", "bool"),
        ("l10n_do_expense_type", "varchar"),
        ("l10n_do_income_type", "varchar"),
        ("l10n_do_cancellation_type", "varchar"),
        ("l10n_do_origin_ncf", "varchar"),
        ("l10n_do_ncf_expiration_date", "date"),
        ("is_ecf_invoice", "bool"),
        ("l10n_do_electronic_stamp", "varchar"),
    ],
    "account_move_line": [
        ("l10n_do_itbis_amount", "numeric"),
    ],
    "account_journal": [
        ("l10n_do_payment_form", "varchar"),
    ],
    "res_partner": [
        ("l10n_do_dgii_tax_payer_type", "varchar"),
        ("l10n_do_expense_type", "varchar"),
    ],
}


def _is_from_ncf_manager(version):
    return bool(version) and version.split(".")[0] in ("11", "12")


def create_new_columns(cr):
    for table, columns in NEW_COLUMNS.items():
        for column, column_type in columns:
            if not column_exists(cr, table, column):
                create_column(cr, table, column, column_type)


def migrate_account_move(cr):
    """Copy the NCF and the DGII fields of the v12 invoices."""
    has_invoice_table = table_exists(cr, "account_invoice") and column_exists(
        cr, "account_invoice", "move_id"
    )
    if has_invoice_table:
        # account_invoice.reference is the NCF as typed in v12. The upgrade
        # copied it to payment_reference (customer) or ref (vendor), but those
        # can also hold free text, so they are only a fallback.
        cr.execute(
            """
            UPDATE account_move am
               SET l10n_do_fiscal_number = UPPER(TRIM(ai.reference))
              FROM account_invoice ai
             WHERE ai.move_id = am.id
               AND am.move_type IN ('out_invoice', 'out_refund',
                                    'in_invoice', 'in_refund')
               AND UPPER(TRIM(ai.reference)) ~ %s
            """,
            [NCF_REGEX],
        )
        _logger.info("%s NCF copied from account_invoice.reference", cr.rowcount)

    cr.execute(
        """
        UPDATE account_move
           SET l10n_do_fiscal_number = UPPER(TRIM(
                   CASE WHEN move_type IN ('out_invoice', 'out_refund')
                        THEN payment_reference ELSE ref END))
         WHERE l10n_do_fiscal_number IS NULL
           AND move_type IN ('out_invoice', 'out_refund', 'in_invoice', 'in_refund')
           AND UPPER(TRIM(CASE WHEN move_type IN ('out_invoice', 'out_refund')
                               THEN payment_reference ELSE ref END)) ~ %s
        """,
        [NCF_REGEX],
    )
    _logger.info("%s NCF copied from payment_reference/ref", cr.rowcount)

    cr.execute(
        """
        UPDATE account_move
           SET l10n_do_sequence_prefix = LEFT(l10n_do_fiscal_number, 3),
               l10n_do_sequence_number = SUBSTRING(l10n_do_fiscal_number FROM 4)::int8
         WHERE l10n_do_fiscal_number IS NOT NULL
           AND SUBSTRING(l10n_do_fiscal_number FROM 4)::int8 <= 2147483647
        """
    )

    # v12 column → v17 column, both on account_move
    renamed = [
        ("expense_type", "l10n_do_expense_type"),
        ("income_type", "l10n_do_income_type"),
        ("anulation_type", "l10n_do_cancellation_type"),
        ("origin_out", "l10n_do_origin_ncf"),
        ("ncf_expiration_date", "l10n_do_ncf_expiration_date"),
    ]
    for old, new in renamed:
        if not column_exists(cr, "account_move", old):
            continue
        cr.execute(
            f"""
            UPDATE account_move
               SET {new} = {old}
             WHERE {new} IS NULL
               AND {old} IS NOT NULL
               AND move_type <> 'entry'
            """
        )
        _logger.info("account_move.%s → %s: %s rows", old, new, cr.rowcount)

    # Fields with a default in v17 are only meaningful on their side
    cr.execute(
        """
        UPDATE account_move
           SET l10n_do_income_type = NULL
         WHERE move_type NOT IN ('out_invoice', 'out_refund')
        """
    )
    cr.execute(
        """
        UPDATE account_move
           SET l10n_do_expense_type = NULL
         WHERE move_type NOT IN ('in_invoice', 'in_refund')
        """
    )
    cr.execute(
        """
        UPDATE account_move
           SET is_ecf_invoice = COALESCE(l10n_do_fiscal_number LIKE 'E%', FALSE),
               l10n_latam_manual_document_number = FALSE
        """
    )


def migrate_account_journal(cr):
    if column_exists(cr, "account_journal", "payment_form"):
        cr.execute(
            """
            UPDATE account_journal
               SET l10n_do_payment_form = payment_form
             WHERE payment_form IS NOT NULL
            """
        )

    # The journal → ir.sequence link (with ncf_control) was dropped by the
    # upgrade: a journal uses fiscal documents when it has invoices with NCF,
    # or when it is a sale journal with an NCF controlled sequence (v12 named
    # it "<code> Secuencia" with prefix "<code>/..."), or a purchase journal
    # of a fiscal purchase type.
    params = {"purchase_types": FISCAL_PURCHASE_TYPES}
    ncf_sequence = "FALSE"
    if column_exists(cr, "ir_sequence", "ncf_control"):
        ncf_sequence = """
            EXISTS (
                SELECT 1 FROM ir_sequence s
                 WHERE s.ncf_control
                   AND s.prefix LIKE aj.code || '/%%'
                   AND s.company_id = aj.company_id
            )
        """
    purchase_type = "FALSE"
    if column_exists(cr, "account_journal", "purchase_type"):
        purchase_type = "aj.purchase_type IN %(purchase_types)s"
    cr.execute(
        f"""
        UPDATE account_journal aj
           SET l10n_latam_use_documents = TRUE
          FROM res_company c
          JOIN res_partner p ON p.id = c.partner_id
          JOIN res_country co ON co.id = p.country_id
         WHERE c.id = aj.company_id
           AND co.code = 'DO'
           AND (
               EXISTS (
                   SELECT 1 FROM account_move am
                    WHERE am.journal_id = aj.id
                      AND am.l10n_do_fiscal_number IS NOT NULL
               )
               OR (aj.type = 'sale' AND {ncf_sequence})
               OR (aj.type = 'purchase' AND {purchase_type})
           )
           AND aj.type IN ('sale', 'purchase')
        RETURNING aj.code
        """,
        params,
    )
    codes = [row[0] for row in cr.fetchall()]
    _logger.info("Journals using fiscal documents: %s", ", ".join(codes))


def migrate_res_partner(cr):
    if column_exists(cr, "res_partner", "sale_fiscal_type"):
        for old, new in PARTNER_TAX_PAYER_TYPE.items():
            cr.execute(
                """
                UPDATE res_partner
                   SET l10n_do_dgii_tax_payer_type = %s
                 WHERE sale_fiscal_type = %s
                """,
                [new, old],
            )
        # Credit fiscal partners with an RNC starting with 4 are nonprofit
        # organizations in l10n_do_accounting (_compute_l10n_do_dgii_payer_type)
        cr.execute(
            """
            UPDATE res_partner
               SET l10n_do_dgii_tax_payer_type = 'nonprofit'
             WHERE sale_fiscal_type = 'fiscal'
               AND vat ~ '^4[0-9]{8}$'
            """
        )
    if column_exists(cr, "res_partner", "expense_type"):
        cr.execute(
            """
            UPDATE res_partner
               SET l10n_do_expense_type = expense_type
             WHERE expense_type IS NOT NULL
            """
        )


def remove_v12_views(cr):
    """Delete the ncf_manager/ncf_invoice_template views and their inheriting
    views. Their xmlids now belong to l10n_do_accounting: left as they are,
    they would be combined with the v17 views (and validated against them)
    while referencing v12 fields. The views of the other modules are created
    again when those modules are updated."""
    cr.execute(
        """
        WITH RECURSIVE views AS (
            SELECT res_id AS id
              FROM ir_model_data
             WHERE module = 'l10n_do_accounting'
               AND model = 'ir.ui.view'
            UNION
            SELECT v.id
              FROM ir_ui_view v
              JOIN views ON v.inherit_id = views.id
        )
        SELECT id FROM views
        """
    )
    view_ids = tuple(row[0] for row in cr.fetchall())
    if not view_ids:
        return
    cr.execute(
        "DELETE FROM ir_model_data WHERE model = 'ir.ui.view' AND res_id IN %s",
        [view_ids],
    )
    # Children first: ir_ui_view.inherit_id is ON DELETE RESTRICT
    cr.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT id, 0 AS depth FROM ir_ui_view WHERE id IN %s
            UNION
            SELECT v.id, tree.depth + 1
              FROM ir_ui_view v
              JOIN tree ON v.inherit_id = tree.id
        )
        SELECT id FROM tree GROUP BY id ORDER BY MAX(depth) DESC
        """,
        [view_ids],
    )
    for (view_id,) in cr.fetchall():
        cr.execute("DELETE FROM ir_ui_view WHERE id = %s", [view_id])
    _logger.info("%s v12 views removed", len(view_ids))


def rename_tax_group_xmlids(cr):
    """The v12 l10n_do chart named some tax groups differently than the v17
    template (l10n_do/data/template/account.tax.group-do.csv), and
    l10n_do_accounting looks them up by xmlid (account.<company>_tax_group_itbis).
    """
    for old, new in TAX_GROUP_XMLIDS.items():
        cr.execute(
            """
            UPDATE ir_model_data d
               SET name = SUBSTRING(d.name FROM '^[0-9]+_') || %(new)s
             WHERE d.module = 'account'
               AND d.model = 'account.tax.group'
               AND d.name ~ ('^[0-9]+_' || %(old)s || '$')
               AND NOT EXISTS (
                   SELECT 1 FROM ir_model_data d2
                    WHERE d2.module = 'account'
                      AND d2.name = SUBSTRING(d.name FROM '^[0-9]+_') || %(new)s
               )
            """,
            {"old": old, "new": new},
        )
        if cr.rowcount:
            _logger.info("account.tax.group xmlid %s → %s: %s", old, new, cr.rowcount)


def migrate(cr, version):
    if not _is_from_ncf_manager(version):
        return

    _logger.info("Migrating ncf_manager %s data to l10n_do_accounting", version)
    rename_tax_group_xmlids(cr)
    remove_v12_views(cr)
    create_new_columns(cr)
    migrate_account_move(cr)
    migrate_account_journal(cr)
    migrate_res_partner(cr)
