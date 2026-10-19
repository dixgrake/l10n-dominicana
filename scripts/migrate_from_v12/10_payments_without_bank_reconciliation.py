"""Post payments directly on the bank account, as in Odoo 11/12.

Since Odoo 14, a payment is posted on an "Outstanding Receipts/Payments"
account and only reaches the bank account when it is reconciled with a bank
statement line. The upgrade keeps the payments before the lock date on the
bank account, but moves the later ones to new outstanding accounts, and their
invoices stay "In Payment". A company that does not reconcile bank statements
ends up with bank balances short of those payments.

For every bank/cash journal without statement lines, this script:
- sets the journal account as outstanding account of its payment methods, so
  that new payments are posted on the bank account (see the Odoo upgrade
  notice and account.payment._compute_reconciliation_status),
- moves the lines of its existing payments from the outstanding accounts to
  the journal account (only after the lock date, each move stays balanced),
- recomputes the payment matching and the payment state of their invoices.

Run it with:

    odoo shell -d <db> --no-http < 10_payments_without_bank_reconciliation.py

Set DRY_RUN = True to only print what would change.
"""
DRY_RUN = False

Journal = env["account.journal"]
Payment = env["account.payment"]
Line = env["account.move.line"]
cr = env.cr

journals = Journal.search(
    [("type", "in", ("bank", "cash")), ("default_account_id", "!=", False)]
)
journals = journals.filtered(
    lambda j: not env["account.bank.statement.line"].search_count(
        [("journal_id", "=", j.id)]
    )
)

fixed_payments = Payment
for journal in journals:
    bank_account = journal.default_account_id
    method_lines = (
        journal.inbound_payment_method_line_ids
        | journal.outbound_payment_method_line_ids
    )
    outstanding_accounts = method_lines.payment_account_id - bank_account
    lock_date = journal.company_id._get_user_fiscal_lock_date()

    payments = Payment.search([("journal_id", "=", journal.id)])
    lines = Line.search(
        [
            ("payment_id", "in", payments.ids),
            ("account_id", "in", outstanding_accounts.ids),
        ]
    )
    locked = lines.filtered(lambda l: l.date <= lock_date)
    lines -= locked
    posted = lines.filtered(lambda l: l.parent_state == "posted")
    print(
        "%-6s %-40s outstanding %s: %s lines (%s posted, balance %.2f), "
        "%s skipped before lock date %s"
        % (
            journal.code,
            bank_account.display_name,
            ", ".join(outstanding_accounts.mapped("code")) or "-",
            len(lines),
            len(posted),
            sum(posted.mapped("balance")),
            len(locked),
            lock_date,
        )
    )
    if DRY_RUN:
        continue

    method_lines.write({"payment_account_id": bank_account.id})
    if lines:
        # SQL: account_id of posted lines cannot be written through the ORM.
        # Debit/credit do not change, so every move stays balanced.
        cr.execute(
            "UPDATE account_move_line SET account_id = %s WHERE id IN %s",
            [bank_account.id, tuple(lines.ids)],
        )
        cr.execute(
            "UPDATE account_payment SET outstanding_account_id = %s WHERE id IN %s",
            [bank_account.id, tuple(lines.payment_id.ids)],
        )
        fixed_payments |= lines.payment_id

if not DRY_RUN and fixed_payments:
    env.invalidate_all()
    fixed_lines = fixed_payments.move_id.line_ids
    env.add_to_compute(Line._fields["account_root_id"], fixed_lines)

    journal_payments = Payment.search([("journal_id", "in", journals.ids)])
    for field in ("is_reconciled", "is_matched"):
        env.add_to_compute(Payment._fields[field], journal_payments)

    invoices = (
        journal_payments.reconciled_invoice_ids
        | journal_payments.reconciled_bill_ids
    )
    env.add_to_compute(env["account.move"]._fields["payment_state"], invoices)
    env.flush_all()

    print(
        "%s payments fixed, %s invoices still In Payment"
        % (
            len(fixed_payments),
            env["account.move"].search_count([("payment_state", "=", "in_payment")]),
        )
    )

if DRY_RUN:
    env.cr.rollback()
else:
    env.cr.commit()
