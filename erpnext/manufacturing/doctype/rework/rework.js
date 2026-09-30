// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Rework", {
	setup(frm) {
		frm.set_query("fg_serial_no", () => ({
			filters: { status: "Active" },
		}));

		frm.set_query("return_warehouse", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("issue_warehouse", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("supplier_warehouse", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));

		frm.set_query("serial_no", "components_added", () => ({
			filters: { status: "Active", warehouse: frm.doc.issue_warehouse || "" },
		}));

		frm.set_query("operation", () => ({
			filters: frm.doc.work_order ? { name: ["in", get_work_order_operations(frm)] } : {},
		}));
	},

	refresh(frm) {
		if (frm.doc.stock_entry) {
			frm.add_custom_button(__("Component Stock Entry"), () =>
				frappe.set_route("Form", "Stock Entry", frm.doc.stock_entry)
			);
		}
		if (frm.doc.job_card) {
			frm.add_custom_button(__("Corrective Job Card"), () =>
				frappe.set_route("Form", "Job Card", frm.doc.job_card)
			);
		}
		if (frm.doc.sent_stock_entry) {
			frm.add_custom_button(__("Sent Stock Entry"), () =>
				frappe.set_route("Form", "Stock Entry", frm.doc.sent_stock_entry)
			);
		}
		if (frm.doc.received_stock_entry) {
			frm.add_custom_button(__("Received Stock Entry"), () =>
				frappe.set_route("Form", "Stock Entry", frm.doc.received_stock_entry)
			);
		}

		if (
			frm.doc.docstatus === 1 &&
			frm.doc.rework_house === "Bought Out" &&
			frm.doc.sent_stock_entry &&
			!frm.doc.received_stock_entry
		) {
			frm.add_custom_button(__("Mark as Received"), () => mark_received(frm));
		}

		if (frm.doc.fg_serial_no) {
			frm.add_custom_button(__("Current Composition"), () =>
				frappe.call({
					method: "erpnext.manufacturing.doctype.rework.rework.get_fg_current_composition",
					args: { fg_serial_no: frm.doc.fg_serial_no },
					callback(r) {
						show_composition_dialog(frm.doc.fg_serial_no, r.message);
					},
				})
			);
		}
	},

	rework_house(frm) {
		frm.trigger("refresh");
	},
});

function get_work_order_operations(frm) {
	// Populated via a quick client-side fetch so the Operation link is scoped to this Work
	// Order's own operations instead of every Operation in the system.
	if (!frm.doc.work_order) return [];
	if (frm._wo_operations_for === frm.doc.work_order && frm._wo_operations) {
		return frm._wo_operations;
	}
	frappe.db
		.get_list("Work Order Operation", {
			filters: { parent: frm.doc.work_order },
			fields: ["operation"],
			limit: 50,
		})
		.then((rows) => {
			frm._wo_operations_for = frm.doc.work_order;
			frm._wo_operations = rows.map((r) => r.operation);
		});
	return frm._wo_operations || [];
}

function mark_received(frm) {
	frappe.prompt(
		[
			{
				fieldname: "received_date",
				fieldtype: "Date",
				label: __("Received Date"),
				default: frappe.datetime.get_today(),
			},
		],
		(values) => {
			frappe.call({
				method: "mark_received",
				doc: frm.doc,
				args: { received_date: values.received_date },
				freeze: true,
				freeze_message: __("Recording receipt..."),
				callback() {
					frm.reload_doc();
				},
			});
		},
		__("Mark as Received"),
		__("Confirm")
	);
}

function show_composition_dialog(fg_serial_no, data) {
	if (!data) return;
	const rows = (data.current_components || [])
		.map(
			(c) =>
				`<tr><td>${c.item_code}</td><td>${c.serial_no || c.batch_no || ""}</td><td>${c.qty}</td><td>${
					c.source
				}</td></tr>`
		)
		.join("");

	const baseline_note = data.baseline_reliable
		? ""
		: `<p class="text-muted">${__(
				"Original baseline could not be reliably attributed (this FG was produced alongside other units in the same batch) - components below reflect Rework history only."
		  )}</p>`;

	new frappe.ui.Dialog({
		title: __("Current Composition: {0}", [fg_serial_no]),
		fields: [
			{
				fieldtype: "HTML",
				options: `${baseline_note}
					<table class="table table-bordered">
						<thead><tr><th>${__("Item")}</th><th>${__("Serial/Batch")}</th><th>${__("Qty")}</th><th>${__(
					"Source"
				)}</th></tr></thead>
						<tbody>${rows || `<tr><td colspan="4">${__("No data")}</td></tr>`}</tbody>
					</table>`,
			},
		],
	}).show();
}
