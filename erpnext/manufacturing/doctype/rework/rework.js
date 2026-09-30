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

		// Removed components are expected to already be "inside" something else (not sitting
		// free in a warehouse) - excluding Active serials keeps the picker from listing
		// obviously-wrong candidates. Added components must be Active, available stock in
		// Issue Warehouse.
		frm.set_query("serial_no", "components_removed", () => ({
			filters: { status: ["!=", "Active"] },
		}));
		frm.set_query("serial_no", "components_added", () => ({
			filters: { status: "Active", warehouse: frm.doc.issue_warehouse || "" },
		}));

		frm.set_query("operation", () => ({
			filters: frm.doc.work_order ? { name: ["in", get_work_order_operations(frm)] } : {},
		}));

		frm.set_query("purchase_order", () => ({
			filters: { supplier: frm.doc.supplier || "" },
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

		render_current_composition(frm);
	},

	rework_house(frm) {
		// Only clear each path's own input fields - components_removed/components_added apply
		// to both paths (informational for Bought Out) and must survive the toggle.
		if (frm.doc.rework_house === "In-House") {
			frm.set_value("supplier", "");
			frm.set_value("supplier_warehouse", "");
		} else {
			frm.set_value("return_warehouse", "");
			frm.set_value("issue_warehouse", "");
			frm.set_value("work_order", "");
			frm.set_value("operation", "");
		}
		frm.trigger("refresh");
	},

	fg_serial_no(frm) {
		render_current_composition(frm);
	},

	issue_warehouse(frm) {
		// Serial picker for components_added is scoped to issue_warehouse - already-picked
		// rows from a different warehouse would otherwise only get caught at save time,
		// disconnected from the field change that made them stale.
		frm.clear_table("components_added");
		frm.refresh_field("components_added");
	},
});

frappe.ui.form.on("Rework Component", {
	serial_no(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.serial_no) return;
		frappe.db.get_value("Serial No", row.serial_no, "item_code").then(({ message }) => {
			frappe.model.set_value(cdt, cdn, "item_code", message.item_code);
			frappe.model.set_value(cdt, cdn, "batch_no", "");
		});
	},

	batch_no(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.batch_no) return;
		frappe.db.get_value("Batch", row.batch_no, "item").then(({ message }) => {
			frappe.model.set_value(cdt, cdn, "item_code", message.item);
			frappe.model.set_value(cdt, cdn, "serial_no", "");
		});
	},
});

function render_current_composition(frm) {
	const wrapper = frm.get_field("current_composition_html")?.$wrapper;
	if (!wrapper) return;

	if (!frm.doc.fg_serial_no) {
		wrapper.empty();
		return;
	}

	wrapper.html(`<p class="text-muted">${__("Loading current composition...")}</p>`);
	frappe.call({
		method: "erpnext.manufacturing.doctype.rework.rework.get_fg_current_composition",
		args: { fg_serial_no: frm.doc.fg_serial_no },
		callback(r) {
			wrapper.html(render_composition_html(frm.doc.fg_serial_no, r.message));
		},
	});
}

function render_composition_html(fg_serial_no, data) {
	if (!data) return "";

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
		: `<p class="text-muted small">${__(
				"Original baseline could not be reliably attributed (this FG was produced alongside other units in the same batch) - the list below reflects Rework history only, not the original manufacture."
		  )}</p>`;

	const history_note =
		data.rework_history && data.rework_history.length
			? `<p class="text-muted small">${__("Rework history")}: ${data.rework_history.join(", ")}</p>`
			: "";

	return `
		<div class="rework-current-composition">
			<p><strong>${__("Currently in {0}", [fg_serial_no])}:</strong></p>
			${baseline_note}
			<table class="table table-bordered table-sm">
				<thead><tr><th>${__("Item")}</th><th>${__("Serial/Batch")}</th><th>${__("Qty")}</th><th>${__(
		"Since"
	)}</th></tr></thead>
				<tbody>${rows || `<tr><td colspan="4">${__("No components recorded")}</td></tr>`}</tbody>
			</table>
			${history_note}
		</div>
	`;
}

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
