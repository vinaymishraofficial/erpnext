# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import timeout

from erpnext.manufacturing.doctype.bom_update_log import bom_updation_utils
from erpnext.manufacturing.doctype.bom_update_log.test_bom_update_log import (
	update_cost_in_all_boms_in_test,
)
from erpnext.manufacturing.doctype.bom_update_tool.bom_update_tool import (
	enqueue_link_sub_assembly_boms,
	enqueue_replace_bom,
	get_unlinked_sub_assemblies,
)
from erpnext.manufacturing.doctype.production_plan.test_production_plan import make_bom
from erpnext.stock.doctype.item.test_item import create_item
from erpnext.tests.permission_test_utils import (
	as_user,
	assert_refused,
	assert_refused_for_names,
	assert_refused_without,
	make_fenced_user,
)
from erpnext.tests.utils import ERPNextTestSuite


class TestBOMUpdateTool(ERPNextTestSuite):
	"Test major functions run via BOM Update Tool."

	def setUp(self):
		self.load_test_records("BOM")

	@timeout
	def test_replace_bom(self):
		current_bom = "BOM-_Test Item Home Desktop Manufactured-001"

		bom_doc = frappe.copy_doc(self.globalTestRecords["BOM"][0])
		bom_doc.items[1].item_code = "_Test Item"
		bom_doc.insert()

		boms = frappe._dict(current_bom=current_bom, new_bom=bom_doc.name)
		enqueue_replace_bom(boms=boms)

		self.assertFalse(frappe.db.exists("BOM Item", {"bom_no": current_bom, "docstatus": 1}))
		self.assertTrue(frappe.db.exists("BOM Item", {"bom_no": bom_doc.name, "docstatus": 1}))

	@timeout
	def test_bom_cost(self):
		for item in ["BOM Cost Test Item 1", "BOM Cost Test Item 2", "BOM Cost Test Item 3"]:
			item_doc = create_item(item, valuation_rate=100)
			if item_doc.valuation_rate != 100.00:
				frappe.db.set_value("Item", item_doc.name, "valuation_rate", 100)

		bom_no = frappe.db.get_value("BOM", {"item": "BOM Cost Test Item 1"}, "name")
		if not bom_no:
			doc = make_bom(
				item="BOM Cost Test Item 1",
				raw_materials=["BOM Cost Test Item 2", "BOM Cost Test Item 3"],
				currency="INR",
			)
		else:
			doc = frappe.get_doc("BOM", bom_no)

		self.assertEqual(doc.total_cost, 200)

		frappe.db.set_value("Item", "BOM Cost Test Item 2", "valuation_rate", 200)
		update_cost_in_all_boms_in_test()

		doc.load_from_db()
		self.assertEqual(doc.total_cost, 300)

		frappe.db.set_value("Item", "BOM Cost Test Item 2", "valuation_rate", 100)
		update_cost_in_all_boms_in_test()

		doc.load_from_db()
		self.assertEqual(doc.total_cost, 200)

	def test_replace_bom_requires_every_rewritten_bom_to_be_writable(self):
		current_bom = "BOM-_Test Item Home Desktop Manufactured-001"
		parent_bom = frappe.db.get_value("BOM Item", {"bom_no": current_bom, "parenttype": "BOM"}, "parent")
		bom_doc = frappe.copy_doc(self.globalTestRecords["BOM"][0])
		bom_doc.items[1].item_code = "_Test Item"
		bom_doc.insert()

		def current_kwargs(name):
			return {"boms": {"current_bom": name, "new_bom": bom_doc.name}}

		def new_kwargs(name):
			return {"boms": {"current_bom": current_bom, "new_bom": name}}

		fenced = make_fenced_user(
			"bom-fenced@example.com", ["Manufacturing Manager"], [("BOM", current_bom), ("BOM", bom_doc.name)]
		)
		with as_user(fenced):
			assert_refused_without(
				self,
				[parent_bom],
				enqueue_replace_bom,
				boms={"current_bom": current_bom, "new_bom": bom_doc.name},
			)
			assert_refused_for_names(self, enqueue_replace_bom, current_kwargs, [], caller_supplied=True)
			assert_refused_for_names(self, enqueue_replace_bom, new_kwargs, [], caller_supplied=True)
		allowed = make_fenced_user(
			"bom-fenced@example.com",
			["Manufacturing Manager"],
			[("BOM", current_bom), ("BOM", bom_doc.name), ("BOM", parent_bom)],
		)
		with as_user(allowed):
			enqueue_replace_bom(boms={"current_bom": current_bom, "new_bom": bom_doc.name})
		self.assertTrue(frappe.db.exists("BOM Item", {"bom_no": bom_doc.name, "parent": parent_bom}))


class TestLinkSubAssemblyBOMs(ERPNextTestSuite):
	"Link sub-assemblies that got their own BOM after they were used in other BOMs."

	def setUp(self):
		self.tag = frappe.generate_hash(length=6).upper()

	def item(self, name):
		return create_item(f"_Test Link SA {self.tag} {name}", valuation_rate=100).name

	def bom(self, item, rows, **fields):
		"""rows: (item_code, extra BOM Item fields); fields: extra BOM fields"""
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": item,
				"company": "_Test Company",
				"currency": frappe.get_cached_value("Company", "_Test Company", "default_currency"),
				"quantity": 1,
				"is_active": 1,
				"is_default": 1,
				"items": [{"item_code": code, "qty": 2, **extra} for code, extra in rows],
				**fields,
			}
		)
		bom.insert()
		bom.submit()
		return bom

	def preview(self, boms):
		preview = get_unlinked_sub_assemblies()
		return (
			[(row.parent, row.sub_bom) for row in preview["rows"] if row.parent in boms],
			[row.parent for row in preview["loops"] if row.parent in boms],
		)

	def waiting(self, boms):
		return [
			(row.parent, row.sub_bom)
			for row in get_unlinked_sub_assemblies()["zero_cost"]
			if row.parent in boms
		]

	def link(self):
		"""Runs the job inline and returns the progress it reported."""
		events = []
		with patch.object(
			bom_updation_utils.frappe,
			"publish_progress",
			lambda percent, **kwargs: events.append((percent, kwargs["description"])),
		):
			bom_updation_utils.link_sub_assembly_boms()
		return events

	def test_links_sub_assembly_and_explodes_it(self):
		fg, sa, rm1, rm2 = (self.item(name) for name in ("FG", "SA", "RM1", "RM2"))
		fg_bom = self.bom(fg, [(sa, {}), (rm1, {})])
		sa_bom = self.bom(sa, [(rm2, {})])
		self.assertEqual(self.preview({fg_bom.name}), ([(fg_bom.name, sa_bom.name)], []))

		self.link()

		fg_bom.reload()
		self.assertEqual(next(row for row in fg_bom.items if row.item_code == sa).bom_no, sa_bom.name)
		self.assertEqual({row.item_code for row in fg_bom.exploded_items}, {rm1, rm2})
		self.assertEqual(self.preview({fg_bom.name}), ([], []))

	def test_updates_the_boms_above_children_first(self):
		top, fg, sa, rm = (self.item(name) for name in ("TOP", "FG", "SA", "RM"))
		fg_bom = self.bom(fg, [(sa, {})])
		top_bom = self.bom(top, [(fg, {"bom_no": fg_bom.name})])
		self.bom(sa, [(rm, {})])

		events = self.link()

		top_bom.reload()
		self.assertEqual({row.item_code for row in top_bom.exploded_items}, {rm})
		percents = [percent for percent, _description in events]
		self.assertEqual(percents, sorted(percents))
		self.assertEqual(events[-1], (100, "Done"))
		described = [description for _percent, description in events]
		self.assertLess(
			next(i for i, d in enumerate(described) if fg_bom.name in d),
			next(i for i, d in enumerate(described) if top_bom.name in d),
		)

	def test_waits_while_the_sub_assembly_bom_costs_0(self):
		"""Linking would turn the row's rate from the item's own rate into the sub-BOM's 0."""
		fg, sa, rm = (self.item(name) for name in ("FG", "SA", "RM"))
		frappe.db.set_value("Item", rm, "valuation_rate", 0)
		fg_bom = self.bom(fg, [(sa, {})])
		sa_bom = self.bom(sa, [(rm, {})])
		self.assertEqual(sa_bom.total_cost, 0)
		self.assertEqual(self.preview({fg_bom.name}), ([], []))
		self.assertEqual(self.waiting({fg_bom.name}), [(fg_bom.name, sa_bom.name)])

		self.link()
		fg_bom.reload()
		self.assertFalse(fg_bom.items[0].bom_no)

		# Once the sub-assembly BOM has a cost, the row is linked.
		frappe.db.set_value("Item", rm, "valuation_rate", 4)
		sa_bom.reload()
		sa_bom.update_cost(update_parent=False, from_child_bom=True)
		self.assertEqual(self.preview({fg_bom.name}), ([(fg_bom.name, sa_bom.name)], []))

	def test_links_a_zero_cost_sub_assembly_when_the_bom_does_not_take_its_rate(self):
		fg, sa, rm = (self.item(name) for name in ("FG", "SA", "RM"))
		frappe.db.set_value("Item", rm, "valuation_rate", 0)
		fg_bom = self.bom(fg, [(sa, {})], set_rate_of_sub_assembly_item_based_on_bom=0)
		sa_bom = self.bom(sa, [(rm, {})])
		self.assertEqual(self.preview({fg_bom.name}), ([(fg_bom.name, sa_bom.name)], []))

	def test_skips_do_not_explode_rows(self):
		fg, sa, rm = (self.item(name) for name in ("FG", "SA", "RM"))
		fg_bom = self.bom(fg, [(sa, {"do_not_explode": 1})])
		self.bom(sa, [(rm, {})])
		self.assertEqual(self.preview({fg_bom.name}), ([], []))

	def test_skips_a_link_that_puts_a_bom_inside_itself(self):
		a, b = self.item("A"), self.item("B")
		a_bom = self.bom(a, [(b, {})])
		self.bom(b, [(a, {"bom_no": a_bom.name})])
		self.assertEqual(self.preview({a_bom.name}), ([], [a_bom.name]))

	def test_closes_progress_when_it_fails(self):
		fg, sa, rm = (self.item(name) for name in ("FG", "SA", "RM"))
		self.bom(fg, [(sa, {})])
		self.bom(sa, [(rm, {})])

		events = []
		with (
			patch(
				"erpnext.manufacturing.doctype.bom.bom.BOM.update_exploded_items",
				side_effect=RuntimeError("boom"),
			),
			patch.object(
				bom_updation_utils.frappe,
				"publish_progress",
				lambda percent, **kwargs: events.append((percent, kwargs["description"])),
			),
			self.assertRaises(RuntimeError),
		):
			bom_updation_utils.link_sub_assembly_boms()
		self.assertEqual(events[-1], (100, "Failed"))

	def test_needs_bom_update_tool_permission(self):
		user = make_fenced_user("link-sa-no-role@example.com", ["Stock User"])
		with as_user(user):
			assert_refused(self, get_unlinked_sub_assemblies)
			assert_refused(self, enqueue_link_sub_assembly_boms)

	def test_needs_write_on_every_bom_it_rewrites(self):
		fg, sa, rm = (self.item(name) for name in ("FG", "SA", "RM"))
		fg_bom = self.bom(fg, [(sa, {})])
		sa_bom = self.bom(sa, [(rm, {})])
		# May write the sub-assembly's BOM, but not the BOM the link would rewrite.
		user = make_fenced_user(
			"link-sa-fenced@example.com", ["Manufacturing Manager"], [("BOM", sa_bom.name)]
		)
		with as_user(user):
			assert_refused_without(self, [fg_bom.name], enqueue_link_sub_assembly_boms)
