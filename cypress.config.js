const { defineConfig } = require("cypress");

// Minimal erpnext-app Cypress config, mirroring apps/frappe/cypress.config.js's shape so
// `bench run-ui-tests erpnext --spec <path> --headless` works the same way it does for
// frappe core. baseUrl and adminPassword are overridden by bench via CYPRESS_baseUrl /
// CYPRESS_adminPassword env vars at run time - the values here are just safe fallbacks
// for `cypress open`.
module.exports = defineConfig({
	adminPassword: "admin",
	defaultCommandTimeout: 20000,
	pageLoadTimeout: 15000,
	viewportHeight: 960,
	viewportWidth: 1400,
	e2e: {
		baseUrl: "http://localhost:8000",
		specPattern: ["./cypress/integration/*.js"],
		supportFile: "../frappe/cypress/support/e2e.js",
	},
});
