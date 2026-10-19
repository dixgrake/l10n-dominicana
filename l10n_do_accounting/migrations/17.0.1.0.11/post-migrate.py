"""Finish the ncf_manager (Odoo 11/12) migration, see pre-migrate.py.

Runs once the l10n_do_accounting data (document types) is loaded:
- set the document type of the invoices from their NCF prefix,
- create the document types of the fiscal journals with the v12 NCF
  expiration dates,
- compute the taxpayer type of the partners that had none in v12,
- remove the ncf_manager records that the update does not remove.
"""
import logging

from odoo import api, fields, SUPERUSER_ID
from odoo.tools.sql import column_exists, table_exists

_logger = logging.getLogger(__name__)

# v12 ir.sequence.date_range.sale_fiscal_type → NCF prefix
DATE_RANGE_PREFIX = {
    "final": "B02",
    "fiscal": "B01",
    "gov": "B15",
    "special": "B14",
    "unico": "B12",
    "export": "B16",
    "minor": "B13",
    "informal": "B11",
    "exterior": "B17",
}

# Document types numbered by the company even on vendor bills
# (account.move._is_l10n_do_manual_document_number)
INTERNAL_NCF_TYPES = (
    "minor",
    "e-minor",
    "informal",
    "e-informal",
    "exterior",
    "e-exterior",
)


def _is_from_ncf_manager(version):
    return bool(version) and version.split(".")[0] in ("11", "12")


def assign_document_types(cr):
    """Link the invoices to the DO document type of their NCF prefix.

    The unique indexes of account.move only apply to moves with a document
    type: when the same NCF appears twice in the same scope (e.g. a cancelled
    and a posted invoice), only the posted/latest one gets the document type
    and the others are logged.
    """
    cr.execute(
        """
        WITH doc AS (
            SELECT dt.id, dt.doc_code_prefix, dt.l10n_do_ncf_type
              FROM l10n_latam_document_type dt
              JOIN res_country c ON c.id = dt.country_id
             WHERE c.code = 'DO'
        ),
        candidates AS (
            SELECT am.id,
                   doc.id AS document_type_id,
                   am.move_type IN ('in_invoice', 'in_refund')
                       AND doc.l10n_do_ncf_type NOT IN %(internal)s AS manual,
                   am.state,
                   am.company_id,
                   am.commercial_partner_id,
                   am.l10n_do_fiscal_number
              FROM account_move am
              JOIN account_journal aj ON aj.id = am.journal_id
              JOIN doc ON doc.doc_code_prefix = LEFT(am.l10n_do_fiscal_number, 3)
             WHERE am.l10n_do_fiscal_number IS NOT NULL
               AND am.l10n_latam_document_type_id IS NULL
               AND aj.l10n_latam_use_documents
        ),
        ranked AS (
            SELECT c.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY c.company_id, c.l10n_do_fiscal_number, c.manual,
                                    CASE WHEN c.manual THEN c.commercial_partner_id END
                       ORDER BY c.state = 'posted' DESC, c.id DESC
                   ) AS rank
              FROM candidates c
        )
        UPDATE account_move am
           SET l10n_latam_document_type_id = ranked.document_type_id,
               l10n_latam_manual_document_number = ranked.manual
          FROM ranked
         WHERE ranked.id = am.id
           AND ranked.rank = 1
        """,
        {"internal": INTERNAL_NCF_TYPES},
    )
    _logger.info("Document type set on %s invoices", cr.rowcount)

    cr.execute(
        """
        UPDATE account_move am
           SET is_ecf_invoice = COALESCE(dt.l10n_do_ncf_type LIKE 'e-%', FALSE)
          FROM l10n_latam_document_type dt
         WHERE dt.id = am.l10n_latam_document_type_id
           AND am.l10n_do_fiscal_number IS NOT NULL
        """
    )

    cr.execute(
        """
        SELECT am.id, am.move_type, am.state, am.l10n_do_fiscal_number
          FROM account_move am
          JOIN account_journal aj ON aj.id = am.journal_id
         WHERE am.l10n_do_fiscal_number IS NOT NULL
           AND am.l10n_latam_document_type_id IS NULL
           AND aj.l10n_latam_use_documents
        """
    )
    for move_id, move_type, state, ncf in cr.fetchall():
        _logger.warning(
            "account.move %s (%s, %s): NCF %s left without document type "
            "(unknown prefix or duplicated NCF)",
            move_id,
            move_type,
            state,
            ncf,
        )


def _v12_expiration_dates(cr):
    """Return {(journal_id, ncf prefix): expiration date} from v12 data.

    The v12 NCF sequences (ir.sequence.date_range by sale_fiscal_type) are
    used when they can still be matched to their journal, otherwise the last
    expiration date of the journal invoices.
    """
    dates = {}
    cr.execute(
        """
        SELECT journal_id, l10n_do_sequence_prefix, MAX(l10n_do_ncf_expiration_date)
          FROM account_move
         WHERE l10n_do_ncf_expiration_date IS NOT NULL
           AND l10n_do_sequence_prefix IS NOT NULL
         GROUP BY journal_id, l10n_do_sequence_prefix
        """
    )
    for journal_id, prefix, date in cr.fetchall():
        dates[(journal_id, prefix)] = date

    if column_exists(cr, "ir_sequence_date_range", "sale_fiscal_type"):
        cr.execute(
            """
            SELECT aj.id, dr.sale_fiscal_type, MAX(dr.date_to)
              FROM ir_sequence_date_range dr
              JOIN ir_sequence s ON s.id = dr.sequence_id
              JOIN account_journal aj ON s.prefix LIKE aj.code || '/%%'
                                     AND s.company_id = aj.company_id
             WHERE dr.sale_fiscal_type IS NOT NULL
               AND dr.date_to IS NOT NULL
             GROUP BY aj.id, dr.sale_fiscal_type
            """
        )
        for journal_id, fiscal_type, date in cr.fetchall():
            prefix = DATE_RANGE_PREFIX.get(fiscal_type)
            key = (journal_id, prefix)
            if prefix and (key not in dates or dates[key] < date):
                dates[key] = date
    return dates


def create_journal_document_types(env):
    journals = env["account.journal"].search(
        [
            ("l10n_latam_use_documents", "=", True),
            ("type", "in", ("sale", "purchase")),
            ("company_id.partner_id.country_id.code", "=", "DO"),
        ]
    )
    for journal in journals:
        journal._l10n_do_create_document_types()

    # Document types used by the v12 invoices but not proposed by default for
    # the journal (e.g. vendor B01 bills or customer credit notes).
    env.cr.execute(
        """
        SELECT DISTINCT am.journal_id, am.l10n_latam_document_type_id
          FROM account_move am
         WHERE am.journal_id IN %s
           AND am.l10n_latam_document_type_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM l10n_do_account_journal_document_type jdt
                WHERE jdt.journal_id = am.journal_id
                  AND jdt.l10n_latam_document_type_id = am.l10n_latam_document_type_id
           )
        """,
        [tuple(journals.ids) or (0,)],
    )
    JournalDocumentType = env["l10n_do.account.journal.document_type"].sudo()
    for journal_id, document_type_id in env.cr.fetchall():
        JournalDocumentType.create(
            {"journal_id": journal_id, "l10n_latam_document_type_id": document_type_id}
        )

    today = fields.Date.context_today(journals)
    expiration_dates = _v12_expiration_dates(env.cr)
    for journal_document_type in journals.l10n_do_document_type_ids:
        prefix = journal_document_type.l10n_latam_document_type_id.doc_code_prefix
        date = expiration_dates.get((journal_document_type.journal_id.id, prefix))
        if not date:
            continue
        if date < today:
            _logger.warning(
                "Journal %s, %s: the v12 NCF expiration date %s is past, the "
                "default %s is kept. Check it before issuing new invoices.",
                journal_document_type.journal_id.code,
                prefix,
                date,
                journal_document_type.l10n_do_ncf_expiration_date,
            )
            continue
        journal_document_type.l10n_do_ncf_expiration_date = date
    _logger.info(
        "%s document types configured on journals %s",
        len(journals.l10n_do_document_type_ids),
        ", ".join(journals.mapped("code")),
    )


def compute_missing_tax_payer_types(env):
    partners = env["res.partner"].with_context(active_test=False).search(
        [("l10n_do_dgii_tax_payer_type", "=", False)]
    )
    if partners:
        partners._compute_l10n_do_dgii_payer_type()
        _logger.info("Taxpayer type computed for %s partners", len(partners))


def remove_v12_records(env):
    """Records declared noupdate by ncf_manager, so Odoo does not remove them
    at the end of the update although l10n_do_accounting no longer has them."""
    for xmlid in (
        "l10n_do_accounting.ir_cron_set_ncf_warning",
        "l10n_do_accounting.ir_cron_set_ncf_warning_ir_actions_server",
        "l10n_do_accounting.query_dgii_parameter",
    ):
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            record.unlink()
            _logger.info("v12 record %s removed", xmlid)


def log_obsolete_data(cr):
    """The legacy v12 columns and the account_invoice table are kept: they are
    the source of this migration and can be dropped once it is validated."""
    if table_exists(cr, "account_invoice"):
        _logger.info(
            "account_invoice (v12) table kept for reference, drop it once the "
            "migration is validated"
        )


def migrate(cr, version):
    if not _is_from_ncf_manager(version):
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    assign_document_types(cr)
    env.invalidate_all()
    create_journal_document_types(env)
    compute_missing_tax_payer_types(env)
    remove_v12_records(env)
    env.flush_all()
    log_obsolete_data(cr)
