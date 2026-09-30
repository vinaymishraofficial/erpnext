# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Add/remove components on an already-serialized Finished Good, without re-serializing it.

Distinct from Part to Part Transfer (which converts an item's own identity while keeping its
serial). Rework keeps both the FG's item AND its serial completely unchanged - only its internal
composition changes. Matches SAP's own Rework Order pattern: components move in/out under the
umbrella of an order/event (movement types 261/262), while the FG's own serial/equipment number
is never touched by those movements.

Two paths, selected by `rework_house`:

- In-House: a Repack Stock Entry does the actual component swap (removed components come back
  into `return_warehouse`, replacement components go out from `issue_warehouse`) - the FG's own
  serial never appears as a row in that Stock Entry, so its own Bin/stock position is untouched.
  Labor/costing is optional and reuses ERPNext's existing Corrective Job Card mechanism
  (`work_order` + `operation` -> a real Job Card, `is_corrective_job_card=1`) rather than
  reinventing time-tracking or cost rollup into FG valuation - both already exist in core.

- Bought Out: the FG's own serial leaves and later returns, unchanged throughout - modeled as two
  plain Material Transfer Stock Entries (FG out to a Supplier-linked warehouse on submit, the
  SAME serial back in later via `mark_received()`), matching SAP's own external-refurbishment
  pattern (Subcontracting Type "Refurbishment" + Material Provision Indicator "S": the exact unit
  goes to the vendor and must come back as itself). What the vendor actually did to the unit is
  outside this system's visibility, so `components_removed`/`components_added` are purely
  informational here - populated from what the vendor reports on return, never verified against
  a stock movement the way the In-House path's are.
"""

import frappe
from frappe import _, bold
from frappe.model.document import Document
from frappe.utils import flt, nowdate, nowtime

from erpnext.stock.doctype.batch.batch import get_batch_qty


class Rework(Document):
	def insert(self, *args, **kwargs):
		if self.amended_from:
			# Same reason as Part to Part Transfer's insert() override: these no_copy links
			# would otherwise still point at documents the cancelled original already
			# cancelled, and Document.insert() rejects any link to a cancelled document
			# before before_insert() ever runs.
			self.stock_entry = None
			self.sent_stock_entry = None
			self.received_stock_entry = None
			self.job_card = None

		return super().insert(*args, **kwargs)

	def validate(self):
		self.set_posting_datetime()
		self.validate_fg_serial()
		if self.rework_house == "In-House":
			self.validate_inhouse()
		else:
			self.validate_bought_out()

	def set_posting_datetime(self):
		if not self.set_posting_time:
			self.posting_date = nowdate()
			self.posting_time = nowtime()

	def validate_fg_serial(self):
		serial = frappe.db.get_value(
			"Serial No", self.fg_serial_no, ["item_code", "status", "warehouse"], as_dict=True
		)
		if not serial:
			frappe.throw(_("FG Serial No {0} does not exist").format(bold(self.fg_serial_no)))

		if not frappe.get_cached_value("Item", serial.item_code, "has_serial_no"):
			frappe.throw(_("Item {0} is not a serialized item").format(bold(serial.item_code)))

		if serial.status != "Active":
			frappe.throw(
				_("FG Serial No {0} must be Active, not {1}").format(
					bold(self.fg_serial_no), bold(serial.status)
				)
			)

	def validate_inhouse(self):
		if not self.components_removed and not self.components_added:
			frappe.throw(_("Add at least one component to remove or add"))

		if self.components_removed and not self.return_warehouse:
			frappe.throw(_("Set Return Warehouse to record removed components"))

		if self.components_added and not self.issue_warehouse:
			frappe.throw(_("Set Issue Warehouse to record added components"))

		for row in self.components_removed:
			self.validate_removed_component(row)

		for row in self.components_added:
			self.validate_added_component(row)

		if bool(self.work_order) != bool(self.operation):
			frappe.throw(_("Set both Work Order and Operation, or neither"))

	def validate_removed_component(self, row):
		if not row.qty or row.qty <= 0:
			frappe.throw(_("Row #{0}: Qty must be greater than 0").format(row.idx))

		if row.serial_no:
			serial = frappe.db.get_value("Serial No", row.serial_no, ["item_code", "status"], as_dict=True)
			if not serial:
				frappe.throw(_("Row #{0}: Serial No {1} does not exist").format(row.idx, bold(row.serial_no)))
			if serial.item_code != row.item_code:
				frappe.throw(
					_("Row #{0}: Serial No {1} belongs to Item {2}, not {3}").format(
						row.idx, bold(row.serial_no), bold(serial.item_code), bold(row.item_code)
					)
				)
			if serial.status == "Active":
				frappe.throw(
					_(
						"Row #{0}: Serial No {1} is Active/free stock - it can't be removed from an FG "
						"it isn't currently inside"
					).format(row.idx, bold(row.serial_no))
				)
		elif row.batch_no:
			batch_item = frappe.db.get_value("Batch", row.batch_no, "item")
			if batch_item != row.item_code:
				frappe.throw(
					_("Row #{0}: Batch No {1} belongs to Item {2}, not {3}").format(
						row.idx, bold(row.batch_no), bold(batch_item), bold(row.item_code)
					)
				)
		else:
			frappe.throw(_("Row #{0}: Set a Serial No or Batch No").format(row.idx))

	def validate_added_component(self, row):
		if not row.qty or row.qty <= 0:
			frappe.throw(_("Row #{0}: Qty must be greater than 0").format(row.idx))

		if row.serial_no:
			serial = frappe.db.get_value(
				"Serial No", row.serial_no, ["item_code", "status", "warehouse"], as_dict=True
			)
			if not serial:
				frappe.throw(_("Row #{0}: Serial No {1} does not exist").format(row.idx, bold(row.serial_no)))
			if serial.item_code != row.item_code:
				frappe.throw(
					_("Row #{0}: Serial No {1} belongs to Item {2}, not {3}").format(
						row.idx, bold(row.serial_no), bold(serial.item_code), bold(row.item_code)
					)
				)
			if serial.status != "Active" or serial.warehouse != self.issue_warehouse:
				frappe.throw(
					_("Row #{0}: Serial No {1} is not available in Issue Warehouse {2}").format(
						row.idx, bold(row.serial_no), bold(self.issue_warehouse)
					)
				)
		elif row.batch_no:
			available_qty = flt(get_batch_qty(batch_no=row.batch_no, warehouse=self.issue_warehouse))
			if row.qty > available_qty:
				frappe.throw(
					_("Row #{0}: Only {1} of Batch No {2} is available in Issue Warehouse {3}").format(
						row.idx, available_qty, bold(row.batch_no), bold(self.issue_warehouse)
					)
				)
		else:
			frappe.throw(_("Row #{0}: Set a Serial No or Batch No").format(row.idx))

	def validate_bought_out(self):
		if not self.supplier:
			frappe.throw(_("Set Supplier for a Bought Out rework"))

		if not self.supplier_warehouse:
			frappe.throw(_("Set Supplier Warehouse for a Bought Out rework"))

		company = frappe.db.get_value("Warehouse", self.supplier_warehouse, "company")
		if company != self.company:
			frappe.throw(
				_("Supplier Warehouse {0} does not belong to Company {1}").format(
					bold(self.supplier_warehouse), bold(self.company)
				)
			)

	def on_submit(self):
		try:
			if self.rework_house == "In-House":
				self.submit_inhouse()
			else:
				self.send_to_supplier()
		except Exception:
			# Frappe already wrote docstatus=1 before calling on_submit - a failure here must
			# not leave this looking submitted with nothing actually done (same fail-safe
			# pattern as Part to Part Transfer's on_submit).
			self.db_set("docstatus", 0, update_modified=False)
			raise

	def submit_inhouse(self):
		if self.components_removed or self.components_added:
			self.make_component_stock_entry()
		if self.work_order and self.operation:
			self.make_corrective_job_card()

	def make_component_stock_entry(self):
		stock_entry = frappe.new_doc("Stock Entry")
		stock_entry.update(
			{
				"stock_entry_type": "Repack",
				"purpose": "Repack",
				"company": self.company,
				"set_posting_time": 1,
				"posting_date": self.posting_date,
				"posting_time": self.posting_time,
				"remarks": _("Rework {0} on FG Serial {1}").format(self.name, self.fg_serial_no),
			}
		)
		for row in self.components_removed:
			stock_entry.append("items", self.get_component_row(row, t_warehouse=self.return_warehouse))
		for row in self.components_added:
			stock_entry.append("items", self.get_component_row(row, s_warehouse=self.issue_warehouse))

		stock_entry.flags.ignore_permissions = True
		stock_entry.insert()
		try:
			stock_entry.submit()
		except Exception:
			self.cleanup_failed_stock_entry(stock_entry)
			raise
		self.db_set("stock_entry", stock_entry.name)

	def get_component_row(self, row, **kwargs):
		stock_uom = frappe.get_cached_value("Item", row.item_code, "stock_uom")
		d = {
			"item_code": row.item_code,
			"qty": row.qty,
			"uom": stock_uom,
			"stock_uom": stock_uom,
			"conversion_factor": 1,
			"use_serial_batch_fields": 1,
			**kwargs,
		}
		if row.serial_no:
			d["serial_no"] = row.serial_no
		if row.batch_no:
			d["batch_no"] = row.batch_no
		return d

	def cleanup_failed_stock_entry(self, stock_entry):
		try:
			stock_entry.reload()
			if stock_entry.docstatus == 1:
				stock_entry.flags.ignore_permissions = True
				stock_entry.cancel()
			if stock_entry.docstatus == 0:
				stock_entry.delete(ignore_permissions=True)
		except Exception:
			frappe.log_error(
				title="Rework: could not clean up a Repack Stock Entry left behind by a failed submit"
			)

	def make_corrective_job_card(self):
		from erpnext.manufacturing.doctype.job_card.job_card import (
			make_corrective_job_card as make_corrective,
		)

		original_job_card = frappe.db.get_value(
			"Job Card",
			{"work_order": self.work_order, "operation": self.operation, "is_corrective_job_card": 0},
			"name",
		)
		if not original_job_card:
			frappe.throw(
				_("No existing Job Card found for Work Order {0}, Operation {1}").format(
					bold(self.work_order), bold(self.operation)
				)
			)

		job_card = make_corrective(original_job_card, operation=self.operation, for_operation=self.operation)
		job_card.flags.ignore_permissions = True
		job_card.insert()
		self.db_set("job_card", job_card.name)

	def send_to_supplier(self):
		stock_entry = frappe.new_doc("Stock Entry")
		stock_uom = frappe.get_cached_value("Item", self.fg_item_code, "stock_uom")
		stock_entry.update(
			{
				"stock_entry_type": "Material Transfer",
				"purpose": "Material Transfer",
				"company": self.company,
				"set_posting_time": 1,
				"posting_date": self.sent_date or self.posting_date,
				"posting_time": self.posting_time,
				"remarks": _("Rework {0} - FG sent to Supplier {1}").format(self.name, self.supplier),
			}
		)
		stock_entry.append(
			"items",
			{
				"item_code": self.fg_item_code,
				"qty": 1,
				"uom": stock_uom,
				"stock_uom": stock_uom,
				"conversion_factor": 1,
				"s_warehouse": self.warehouse,
				"t_warehouse": self.supplier_warehouse,
				"use_serial_batch_fields": 1,
				"serial_no": self.fg_serial_no,
			},
		)
		stock_entry.flags.ignore_permissions = True
		stock_entry.insert()
		try:
			stock_entry.submit()
		except Exception:
			self.cleanup_failed_stock_entry(stock_entry)
			raise
		self.db_set("sent_stock_entry", stock_entry.name)
		if not self.sent_date:
			self.db_set("sent_date", stock_entry.posting_date, update_modified=False)

	@frappe.whitelist()
	def mark_received(self, received_date=None):
		"""Bought Out only: record the same FG serial coming back from the supplier.

		A separate action rather than part of on_submit(), because sending and receiving are
		two real-world events with a real gap between them - the Rework doc is submitted when
		the FG leaves, and this runs later, once it physically returns.
		"""
		if self.docstatus != 1:
			frappe.throw(_("Submit this Rework first"))
		if self.rework_house != "Bought Out":
			frappe.throw(_("mark_received is only for Bought Out rework"))
		if self.received_stock_entry:
			frappe.throw(_("Already marked as received via {0}").format(bold(self.received_stock_entry)))

		stock_entry = frappe.new_doc("Stock Entry")
		stock_uom = frappe.get_cached_value("Item", self.fg_item_code, "stock_uom")
		stock_entry.update(
			{
				"stock_entry_type": "Material Transfer",
				"purpose": "Material Transfer",
				"company": self.company,
				"set_posting_time": 1,
				"posting_date": received_date or nowdate(),
				"posting_time": nowtime(),
				"remarks": _("Rework {0} - FG received back from Supplier {1}").format(
					self.name, self.supplier
				),
			}
		)
		stock_entry.append(
			"items",
			{
				"item_code": self.fg_item_code,
				"qty": 1,
				"uom": stock_uom,
				"stock_uom": stock_uom,
				"conversion_factor": 1,
				"s_warehouse": self.supplier_warehouse,
				"t_warehouse": self.warehouse,
				"use_serial_batch_fields": 1,
				"serial_no": self.fg_serial_no,
			},
		)
		stock_entry.flags.ignore_permissions = True
		stock_entry.insert()
		stock_entry.submit()

		self.db_set("received_stock_entry", stock_entry.name, update_modified=False)
		self.db_set("received_date", received_date or stock_entry.posting_date, update_modified=False)

	def before_cancel(self):
		if self.rework_house == "Bought Out" and self.received_stock_entry:
			# The FG's serial has already come back and, per its own stock ledger, may well
			# have moved on again since (delivered, reworked again, etc.) - Stock Entry
			# cancellation's own backdated/later-transaction guard already protects against
			# cancelling out from under that, so nothing extra is needed here beyond ensuring
			# cancel order below actually respects it (received before sent).
			pass

	def on_cancel(self):
		try:
			for fieldname in ("stock_entry", "received_stock_entry", "sent_stock_entry"):
				voucher = self.get(fieldname)
				if not voucher:
					continue
				doc = frappe.get_doc("Stock Entry", voucher)
				if doc.docstatus == 1:
					doc.cancel()

			if self.job_card:
				job_card = frappe.get_doc("Job Card", self.job_card)
				if job_card.docstatus == 1:
					job_card.cancel()
		except Exception:
			# Frappe already wrote docstatus=2 before calling on_cancel - if a later
			# transaction blocks reversing one of the linked Stock Entries partway through,
			# nothing was actually reverted, so this must not be left looking cancelled either.
			self.db_set("docstatus", 1, update_modified=False)
			raise


@frappe.whitelist()
def get_fg_current_composition(fg_serial_no):
	"""Derive an FG serial's current composition on demand: best-effort original baseline
	(only when reliably attributable to this one serial) folded forward through every
	submitted Rework for it, in date order. Nothing here is stored redundantly - the Rework
	documents themselves are the permanent record; this is just a fast, correct query over
	them, the same way the Serial No & Batch Traceability report computes at query time
	rather than maintaining its own synced snapshot.
	"""
	baseline, baseline_reliable = get_original_baseline(fg_serial_no)

	current = {}
	for row in baseline:
		key = (row.item_code, row.serial_no, row.batch_no)
		current.setdefault(
			key,
			{
				"item_code": row.item_code,
				"serial_no": row.serial_no,
				"batch_no": row.batch_no,
				"qty": 0,
				"source": _("Original manufacture"),
			},
		)
		current[key]["qty"] += flt(row.qty)

	reworks = frappe.get_all(
		"Rework",
		filters={"fg_serial_no": fg_serial_no, "docstatus": 1},
		fields=["name", "posting_date", "posting_time"],
		order_by="posting_date, posting_time, creation",
	)
	for rework in reworks:
		removed = frappe.get_all(
			"Rework Component",
			filters={"parent": rework.name, "parentfield": "components_removed"},
			fields=["item_code", "serial_no", "batch_no", "qty"],
		)
		added = frappe.get_all(
			"Rework Component",
			filters={"parent": rework.name, "parentfield": "components_added"},
			fields=["item_code", "serial_no", "batch_no", "qty"],
		)
		for row in removed:
			key = (row.item_code, row.serial_no, row.batch_no)
			if key in current:
				current[key]["qty"] -= flt(row.qty)
				if current[key]["qty"] <= 0:
					del current[key]
		for row in added:
			key = (row.item_code, row.serial_no, row.batch_no)
			current.setdefault(
				key,
				{
					"item_code": row.item_code,
					"serial_no": row.serial_no,
					"batch_no": row.batch_no,
					"qty": 0,
					"source": rework.name,
				},
			)
			current[key]["qty"] += flt(row.qty)
			current[key]["source"] = rework.name

	return {
		"baseline_reliable": baseline_reliable,
		"current_components": list(current.values()),
		"rework_history": [r.name for r in reworks],
	}


def get_original_baseline(fg_serial_no):
	"""Best-effort original as-built baseline for a serial, via the same pattern the
	Traceability report uses (erpnext/stock/report/serial_no_and_batch_traceability). Only
	reliable when the Manufacture/Repack Stock Entry that produced this serial had exactly
	one finished-item row - raw materials are pooled/shared across FG units otherwise, with
	no way to attribute a specific input serial to a specific output serial in this data
	model. Returns (rows, is_reliable).
	"""
	bundle = frappe.qb.DocType("Serial and Batch Bundle")
	entry = frappe.qb.DocType("Serial and Batch Entry")
	receipt_voucher = (
		frappe.qb.from_(entry)
		.join(bundle)
		.on(bundle.name == entry.parent)
		.select(bundle.voucher_type, bundle.voucher_no)
		.where(
			(entry.serial_no == fg_serial_no)
			& (bundle.docstatus == 1)
			& (bundle.is_cancelled == 0)
			& (bundle.type_of_transaction == "Inward")
		)
		.orderby(bundle.posting_datetime)
		.limit(1)
		.run(as_dict=True)
	)
	if not receipt_voucher or receipt_voucher[0].voucher_type != "Stock Entry":
		return [], False

	voucher_no = receipt_voucher[0].voucher_no
	purpose = frappe.db.get_value("Stock Entry", voucher_no, "purpose")
	if purpose not in ("Manufacture", "Repack"):
		return [], False

	fg_row_count = frappe.db.count("Stock Entry Detail", {"parent": voucher_no, "is_finished_item": 1})
	if fg_row_count != 1:
		return [], False

	fg_row = frappe.db.get_value(
		"Stock Entry Detail", {"parent": voucher_no, "is_finished_item": 1}, ["qty"], as_dict=True
	)
	if fg_row and flt(fg_row.qty) != 1:
		# More than one unit produced by this single finished-item row - components are
		# still pooled across those units, so per-serial attribution isn't safe here either.
		return [], False

	stock_entry_detail = frappe.qb.DocType("Stock Entry Detail")
	rows = (
		frappe.qb.from_(stock_entry_detail)
		.join(entry)
		.on((stock_entry_detail.serial_and_batch_bundle == entry.parent) & (entry.docstatus == 1))
		.select(stock_entry_detail.item_code, entry.serial_no, entry.batch_no, entry.qty)
		.where((stock_entry_detail.parent == voucher_no) & (stock_entry_detail.s_warehouse.isnotnull()))
		.run(as_dict=True)
	)
	for row in rows:
		row.qty = abs(flt(row.qty))

	return rows, True
