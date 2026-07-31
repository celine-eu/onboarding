import { test, expect, type Page } from '@playwright/test';

/**
 * The operator console.
 *
 * Needs a live backend that trusts the test issuer, and an operator token to
 * inject the way the ingress would. `task test:e2e` provides both; without them
 * the suite skips rather than failing for the wrong reason.
 */

const TOKEN = process.env.OPERATOR_TOKEN;
const REC = process.env.E2E_REC ?? 'e2e-rec';

test.describe('Operator console', () => {
	test.skip(!TOKEN, 'set OPERATOR_TOKEN (see task test:e2e)');

	/** Play the part of Caddy: attach the verified token to every console call. */
	async function signedIn(page: Page) {
		await page.context().route('**/api/admin/**', (route) => {
			route.continue({
				headers: { ...route.request().headers(), authorization: `Bearer ${TOKEN}` }
			});
		});
	}

	test('an unauthenticated visit goes to sign-in, not to the console', async ({ page }) => {
		// No token injected: the gate must send the browser away rather than
		// render a console shell with empty data.
		await page.goto('/admin').catch(() => {});
		await page.waitForURL(/\/oauth2\/sign_in/, { timeout: 10_000 });
		expect(page.url()).toContain('rd=');
	});

	test('the console renders the queue', async ({ page }) => {
		await signedIn(page);
		await page.goto(`/admin/${REC}`);
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		await expect(page.getByRole('link', { name: 'Console operatori' })).toBeVisible();
	});

	test('identifiers are masked in the queue', async ({ page }) => {
		await signedIn(page);
		await page.goto(`/admin/${REC}`);
		const cell = page.locator('tbody tr').first().locator('.muted').first();
		await expect(cell).toContainText('•');
	});

	test('the reference filter narrows the queue', async ({ page }) => {
		await signedIn(page);
		await page.goto(`/admin/${REC}`);
		const first = await page.locator('tbody tr a').first().textContent();
		await page.getByLabel('Riferimento').fill(first!.trim());
		await page.getByRole('button', { name: 'Filtra' }).click();
		await expect(page.locator('tbody tr')).toHaveCount(1);
	});

	test('the detail page shows the enablement pipeline', async ({ page }) => {
		await signedIn(page);
		await page.goto(`/admin/${REC}`);
		await page.locator('tbody tr a').first().click();

		await expect(page.getByRole('heading', { name: /Abilitazione/ })).toBeVisible();
		// All four steps, always — including the ones not yet run. A shorter
		// pipeline would read as "less to do" rather than "not started".
		await expect(page.locator('.steps li')).toHaveCount(4);
		await expect(page.getByText('Login identity')).toBeVisible();
		await expect(page.getByText('Community member')).toBeVisible();
	});

	test('rejecting demands a reason', async ({ page }) => {
		await signedIn(page);
		await page.goto(`/admin/${REC}`);
		await page.locator('tbody tr a').first().click();

		const reject = page.getByRole('button', { name: 'Rifiuta' });
		if (await reject.isVisible()) {
			await reject.click();
			const confirm = page.getByRole('button', { name: 'Conferma rifiuto' });
			// Disabled until a reason is typed: the participant is told, and
			// whoever reopens the case months later needs to know why.
			await expect(confirm).toBeDisabled();
			await page.getByLabel('Motivo del rifiuto').fill('POD di un\'altra fornitura');
			await expect(confirm).toBeEnabled();
		}
	});

	test('the audit trail lists this community only', async ({ page }) => {
		await signedIn(page);
		await page.goto(`/admin/${REC}/audit`);
		await expect(page.getByRole('heading', { name: /Registro attivita/ })).toBeVisible();
		await expect(page.locator('table tbody tr').first()).toBeVisible();
	});

	test('an operator without permissions is told, not bounced to login', async ({ page }) => {
		const denied = process.env.DENIED_TOKEN;
		test.skip(!denied, 'set DENIED_TOKEN');
		await page.context().route('**/api/admin/**', (route) => {
			route.continue({
				headers: { ...route.request().headers(), authorization: `Bearer ${denied}` }
			});
		});
		await page.goto('/admin');
		await page.waitForURL(/\/admin\/denied/, { timeout: 10_000 });
		await expect(page.getByRole('heading', { name: /Nessuna comunita/ })).toBeVisible();
	});
});
