from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    l10n_do_dgii_start_date = fields.Date("Activities Start Date")
    l10n_do_ecf_issuer = fields.Boolean(
        "Is e-CF issuer",
        help="When activating this field, NCF issuance is disabled.",
    )
    l10n_do_ecf_deferred_submissions = fields.Boolean(
        "Deferred submissions",
        help="Identify taxpayers who have been previously authorized "
        "to have sales through offline mobile devices such as "
        "sales with Handheld, enter others.",
    )

    def _localization_use_documents(self):
        """Dominican localization uses documents"""
        self.ensure_one()
        return (
            True
            if self.country_id == self.env.ref("base.do")
            else super()._localization_use_documents()
        )

    def write(self, vals):
        res = super().write(vals)
        if vals.get("vat"):
            # Fiscal journals created before the VAT was set have no document types
            # yet (account.journal._l10n_do_create_document_types): create them now.
            journals = self.env["account.journal"].sudo().search(
                [
                    ("company_id", "in", self.ids),
                    ("type", "in", ("sale", "purchase")),
                    ("l10n_latam_use_documents", "=", True),
                ]
            )
            for journal in journals:
                journal._l10n_do_create_document_types()
        return res

