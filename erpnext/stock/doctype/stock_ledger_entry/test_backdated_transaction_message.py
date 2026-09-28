# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

"""Regression test for the unfilled ``{}`` in the backdated-transaction error
message.

Bug: StockLedgerEntry.validate_with_last_transaction_posting_time() builds

    _("Please contact any of the following users to {} this transaction.")

with no ``.format(...)`` call -- every sibling ``msg +=`` line in this method
does call ``.format(...)``, this one alone doesn't, so the literal ``{}``
renders as-is in the thrown "Backdated Stock Entry" error message shown to
the user, instead of the word "authorize".
"""

from __future__ import annotations

import importlib
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe
import erpnext.stock.doctype.stock_ledger_entry.stock_ledger_entry as sle_module


class TestBackdatedTransactionMessage(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# See test_stock_reservation_dedup.py for why this reload is needed: a
		# site with a custom app that setattr-patches this exact method at app
		# boot leaves the class attribute permanently overridden for the rest
		# of the process, so re-importing the module alone isn't enough.
		importlib.reload(sle_module)
		cls.StockLedgerEntry = sle_module.StockLedgerEntry

	def test_message_has_no_unfilled_placeholder(self):
		fake_self = SimpleNamespace(
			item_code="ITEM-1",
			warehouse="WH-1",
			posting_date="2026-01-10",
			get=lambda key, default=None: "10:00:00" if key == "posting_time" else default,
		)

		with (
			mock.patch.object(
				frappe, "get_single_value", return_value="Stock Manager"
			),
			mock.patch.object(sle_module, "get_users", return_value=["manager@example.com"]),
			mock.patch.object(frappe, "session", SimpleNamespace(user="someone.else@example.com")),
			mock.patch.object(
				frappe.db, "sql", return_value=[["2026-01-15 10:00:00"]]
			),
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self.StockLedgerEntry.validate_with_last_transaction_posting_time(fake_self)

		message = str(ctx.exception)
		self.assertNotIn("{}", message)
		self.assertIn("authorize", message)


if __name__ == "__main__":
	unittest.main()
