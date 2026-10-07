from . import common
from odoo.tests import tagged
from odoo.exceptions import RedirectWarning


@tagged("-at_install", "post_install")
class AccountJournalTest(common.L10nDOTestsCommon):
    def test_001_raise_redirect(self):
        """
        Checks Journal raises RedirectWarning if trying to
        setup fiscal journal without company vat
        """

        journal = self.env["account.journal"].search(
            [
                ("type", "=", "sale"),
                ("company_id", "=", self.do_company.id),
            ],
            limit=1,
        )

        with self.assertRaises(RedirectWarning):
            self.do_company.vat = False
            journal._get_journal_ncf_types()

    def test_002_fiscal_journal_without_vat_does_not_block(self):
        """A DO company without VAT can still get fiscal journals.

        Installing the chart of accounts creates the sale and purchase journals
        with l10n_latam_use_documents before the user had a chance to set the VAT;
        raising there made a fresh database impossible to set up. The document
        types are created once the VAT is set.
        """
        company = self.env["res.company"].create(
            {"name": "DO company without VAT", "country_id": self.env.ref("base.do").id}
        )
        self.env.user.company_ids |= company
        journal = self.env["account.journal"].with_company(company).create(
            {
                "name": "Fiscal sales",
                "code": "FSV",
                "type": "sale",
                "company_id": company.id,
                "l10n_latam_use_documents": True,
            }
        )
        self.assertFalse(journal.l10n_do_document_type_ids)

        company.vat = "131793916"
        self.assertTrue(journal.l10n_do_document_type_ids)

