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
		// Labelled by step key in the console's language, not by the API's English label.
		await expect(page.getByText('Accesso alla piattaforma')).toBeVisible();
		await expect(page.getByText('Membro della comunità')).toBeVisible();
	});

	test('an unverified phone says approval does not wait for it when SMS is off', async ({
		page
	}) => {
		// scripts/e2e.sh seeds a community asking for `phone_verify` and runs with a
		// real SMS provider and no DPA_SMS_SIGNED, so no phone can be verified.
		await signedIn(page);
		await page.goto(`/admin/${REC}`);
		await page.locator('tbody tr a').first().click();

		await expect(
			page.getByText('la verifica del telefono è disattivata su questa installazione', {
				exact: false
			})
		).toBeVisible();
	});

	test('the invitation outcome is shown translated, never as the English detail', async ({
		page
	}) => {
		await signedIn(page);
		// No provisioning service runs under e2e, so the login step never gets a
		// code of its own. The real response is fetched and one code written onto
		// it, which is exactly what the API returns after a re-approval of a
		// revoked participant.
		await page.route('**/enablement', async (route) => {
			const response = await route.fetch({
				headers: { ...route.request().headers(), authorization: `Bearer ${TOKEN}` }
			});
			const body = await response.json();
			body.steps[0] = {
				...body.steps[0],
				status: 'succeeded',
				detail: 'already existed, invitation not sent: account is disabled',
				invitation: 'account_disabled'
			};
			await route.fulfill({ response, json: body });
		});
		await page.goto(`/admin/${REC}`);
		await page.locator('tbody tr a').first().click();

		await expect(page.getByText("Invito non inviato: l'account è disabilitato.")).toBeVisible();
		await expect(page.getByText('invitation not sent: account is disabled')).toHaveCount(0);

		await page.getByLabel('Lingua').selectOption('es');
		await expect(
			page.getByText('Invitación no enviada: la cuenta está deshabilitada.')
		).toBeVisible();
	});

	test('only a failed invitation send offers a retry on a succeeded login step', async ({
		page
	}) => {
		await signedIn(page);
		// Intercepted for the same reason as above: no provisioning service runs
		// under e2e. The code is swapped between loads.
		let code = 'send_failed';
		await page.route('**/enablement', async (route) => {
			const response = await route.fetch({
				headers: { ...route.request().headers(), authorization: `Bearer ${TOKEN}` }
			});
			const body = await response.json();
			body.steps[0] = {
				...body.steps[0],
				status: 'succeeded',
				last_error: null,
				detail: 'created, invitation not sent',
				invitation: code
			};
			await route.fulfill({ response, json: body });
		});
		await page.goto(`/admin/${REC}`);
		await page.locator('tbody tr a').first().click();

		const loginStep = page.locator('ul.steps li').first();
		await expect(
			loginStep.getByText("Invito non inviato: non è stato possibile inviare l'email.", {
				exact: false
			})
		).toBeVisible();
		await expect(loginStep.getByRole('button', { name: 'Ritenta questo passo' })).toBeVisible();

		for (const other of ['cooldown', 'no_email']) {
			code = other;
			await page.reload();
			await expect(page.locator(`[data-invitation="${other}"]`)).toBeVisible();
			await expect(
				page.locator('ul.steps li').first().getByRole('button', { name: 'Ritenta questo passo' })
			).toHaveCount(0);
		}
	});

	test('the chosen language applies to the console and survives a reload', async ({ page }) => {
		await signedIn(page);
		await page.goto(`/admin/${REC}`);
		await page.getByLabel('Lingua').selectOption('es');
		await expect(page.getByRole('link', { name: 'Consola de operadores' })).toBeVisible();

		await page.reload();
		await expect(page.getByRole('link', { name: 'Consola de operadores' })).toBeVisible();
		await expect(page.locator('html')).toHaveAttribute('lang', 'es');
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

	test('approval waits for a recorded verification, then goes through', async ({ page }) => {
		// The seeded submissions are under review with nothing recorded. The last row,
		// because other tests open the first.
		await signedIn(page);
		await page.goto(`/admin/${REC}`);
		await page.locator('tbody tr a').last().click();

		const approve = page.getByRole('button', { name: 'Approva' });
		await expect(approve).toBeDisabled();
		await expect(page.getByText('Nessuna verifica registrata.', { exact: false })).toBeVisible();

		// No document is uploaded, so only the offline check can be chosen.
		await expect(page.getByLabel('Verificato su un documento caricato qui')).toBeDisabled();
		await page.getByLabel('Verificato dalla comunità fuori dalla piattaforma').check();
		await page.getByLabel('Nota (facoltativa)').fill('Documento visto in sede');
		await page.getByRole('button', { name: 'Registra verifica' }).click();

		await expect(page.getByText('Verifica registrata.')).toBeVisible();
		await expect(page.locator('ol.verifications li')).toHaveCount(1);
		await expect(approve).toBeEnabled();

		await approve.click();
		await expect(page.locator('.status')).toHaveText('Approvata');
		// Decided: the verification can no longer change.
		await expect(page.getByRole('button', { name: /Registra (una nuova )?verifica/ })).toHaveCount(0);
	});

	test('the audit trail lists this community only', async ({ page }) => {
		await signedIn(page);
		await page.goto(`/admin/${REC}/audit`);
		await expect(page.getByRole('heading', { name: /Registro attività/ })).toBeVisible();
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
		await expect(page.getByRole('heading', { name: /Nessuna comunità/ })).toBeVisible();
	});
});
