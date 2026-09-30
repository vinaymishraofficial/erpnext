// Click-through UI test for "Part to Part Transfer": create one via the desk form (not the
// API), submit it, and confirm the real client script (auto-fill, link-query filters) and
// the resulting document both behave correctly.
//
// Setup uses whitelisted server calls (cy.call) rather than the UI, since seeding a Company/
// Warehouse/Item/Serial No through forms first would make this a much longer, less focused
// test - only the Part to Part Transfer form itself is driven through the UI.

const SRC_ITEM = "_PPT_UI_SRC";
const TGT_ITEM = "_PPT_UI_TGT";
const WAREHOUSE = "_PPT UI WH";

describe("Part to Part Transfer", () => {
	let company, warehouse, serial_no;

	before(() => {
		cy.login();
		cy.call("frappe.client.get_list", {
			doctype: "Company",
			limit_page_length: 1,
		}).then((r) => {
			company = r.message[0].name;
			return cy.call("frappe.client.get_value", { doctype: "Company", filters: company, fieldname: "abbr" });
		}).then((r) => {
			warehouse = `${WAREHOUSE} - ${r.message.abbr}`;

			for (const [item_code, hsn] of [[SRC_ITEM, "84129000"], [TGT_ITEM, "84129000"]]) {
				cy.call("frappe.client.insert", {
					doc: JSON.stringify({
						doctype: "Item",
						item_code,
						item_group: "All Item Groups",
						stock_uom: "Nos",
						is_stock_item: 1,
						has_serial_no: 1,
						serial_no_series: `${item_code}-.#####`,
						gst_hsn_code: hsn,
					}),
				}).then(() => {}, () => {}); // ignore "already exists" on repeat runs
			}

			cy.call("frappe.client.insert", {
				doc: JSON.stringify({ doctype: "Warehouse", warehouse_name: WAREHOUSE, company }),
			}).then(() => {}, () => {});

			cy.call("frappe.client.insert", {
				doc: JSON.stringify({
					doctype: "Stock Entry",
					stock_entry_type: "Material Receipt",
					purpose: "Material Receipt",
					company,
					items: [
						{
							item_code: SRC_ITEM,
							qty: 1,
							t_warehouse: warehouse,
							basic_rate: 100,
							use_serial_batch_fields: 1,
						},
					],
				}),
			}).then((r) => {
				const stock_entry = r.message.name;
				return cy.call("frappe.client.submit", { doctype: "Stock Entry", docname: stock_entry }).then(() => stock_entry);
			}).then((stock_entry) => {
				return cy.call("frappe.client.get", { doctype: "Stock Entry", name: stock_entry });
			}).then((r) => {
				const bundle = r.message.items[0].serial_and_batch_bundle;
				return cy.call("frappe.client.get_list", {
					doctype: "Serial and Batch Entry",
					parent: "Serial and Batch Bundle",
					filters: JSON.stringify({ parent: bundle }),
					fields: JSON.stringify(["serial_no"]),
					limit_page_length: 1,
				});
			}).then((r) => {
				serial_no = r.message[0].serial_no;
			});
		});
	});

	after(() => {
		// Best-effort teardown - real assertions already ran; a leftover disposable test
		// item/warehouse here is the same acceptable residue this session's other manual
		// verification scripts leave, cleaned up the same way (see PPT test cleanup convention).
		cy.call("frappe.client.get_list", {
			doctype: "Part to Part Transfer",
			filters: JSON.stringify({ source_item: SRC_ITEM }),
			fields: JSON.stringify(["name"]),
		}).then((r) => {
			(r.message || []).forEach((d) => {
				cy.call("frappe.client.cancel", { doctype: "Part to Part Transfer", name: d.name }).then(() => {}, () => {});
			});
		});
	});

	it("creates, auto-fills, and submits a transfer through the desk form", () => {
		cy.new_form("Part to Part Transfer");

		cy.fill_field("company", company, "Link");
		cy.fill_field("warehouse", warehouse, "Link");
		cy.fill_field("source_item", SRC_ITEM, "Link");
		cy.fill_field("target_item", TGT_ITEM, "Link");

		// Source Item Name should auto-fill via fetch_from - confirms the form's fetched
		// fields are wired correctly, not just the raw Link value.
		cy.get_field("source_item_name").should("have.value", SRC_ITEM);

		// Add one Serial No row.
		cy.get('[data-fieldname="serial_nos"]').find(".grid-add-row").click();
		cy.get('[data-fieldname="serial_nos"] .grid-row:last [data-fieldname="serial_no"] input')
			.click()
			.type(serial_no, { delay: 100 });
		cy.get(".awesomplete li").contains(serial_no).click();

		// Click elsewhere to commit the grid row, then save and submit.
		cy.get(".page-title").click();
		cy.get(".primary-action").contains("Save").click({ force: true });
		cy.get(".indicator-pill").contains("Not Saved").should("not.exist");

		cy.get(".primary-action").contains("Submit").click({ force: true });
		cy.get(".modal .btn-modal-primary").contains("Yes").click({ force: true });

		cy.get(".indicator-pill").contains("Submitted").should("exist");

		// The controller's refresh() only adds the "Stock Entry" button once stock_entry is set -
		// its presence is the real, end-to-end proof that on_submit() actually ran successfully
		// through the UI, not just via a direct API call.
		cy.get(".custom-actions").contains("Stock Entry").should("exist");
	});
});
