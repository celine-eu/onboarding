import { test, expect, type Page } from '@playwright/test';

/**
 * An applicant who is already a member: the wizard spares them the POD, and the
 * operator completes it and the supply address from the register before
 * approving.
 *
@verifies REQ-0024
@verifies REQ-0025
@verifies REQ-0027
 *
 * The seeded community offers the declaration (`scripts/e2e.sh`). Needs the
 * wizard and the console both, so it runs only through `scripts/e2e.sh`.
 */

const TOKEN = process.env.OPERATOR_TOKEN;
const REC = process.env.E2E_REC ?? 'e2e-rec';

async function signedIn(page: Page) {
	await page.context().route('**/api/admin/**', (route) => {
		route.continue({
			headers: { ...route.request().headers(), authorization: `Bearer ${TOKEN}` }
		});
	});
}

test.describe('An existing member', () => {
	test.skip(!process.env.PLAYWRIGHT_BASE_URL || !TOKEN, 'run through scripts/e2e.sh ui');

	test('submits without a POD, and is approved once the operator completed it', async ({
		page
	}) => {
		const config = await (await page.request.get(`/api/${REC}/config`)).json();
		expect(config.existing_members).toEqual({ enabled: true, skip_steps: ['eligibility'] });

		// ── the wizard ───────────────────────────────────────────────────────
		await page.goto(`/${REC}/onboarding`);
		for (const label of [
			'Acconsento al trattamento dei dati personali ai sensi del GDPR',
			'Accetto il regolamento della comunita energetica',
			'Accetto lo statuto della comunita energetica'
		]) {
			await page.getByLabel(label).check();
		}
		await page.getByLabel(/Sono già socio\/a di E2E Community/).check();
		const created = page.waitForResponse(
			(res) => res.request().method() === 'POST' && /\/submissions$/.test(new URL(res.url()).pathname)
		);
		await page.getByRole('button', { name: 'Avanti' }).click();
		const ref = (await (await created).json()).ref as string;

		const email = `member-${Date.now()}@example.org`;
		await page.locator('input[name="first_name"]').fill('Ada');
		await page.locator('input[name="last_name"]').fill('Socia');
		await page.locator('input[name="fiscal_code"]').fill('RSSMRA85T10A562S');
		await page.locator('input[name="email"]').fill(email);
		// No POD: the community completes it.
		await page.getByRole('button', { name: 'Avanti' }).click();

		await expect(page.getByText('Lo completerà E2E Community')).toBeVisible();
		await page.getByRole('button', { name: 'Invia' }).click();
		await expect(page.getByText('Adesione inviata con successo!')).toBeVisible();

		// ── the console ──────────────────────────────────────────────────────
		await signedIn(page);
		await page.goto(`/admin/${REC}`);
		await page.getByLabel('Solo già soci').check();
		await page.getByLabel('Riferimento').fill(ref);
		await page.getByRole('button', { name: 'Filtra' }).click();
		await expect(page.locator('tbody tr')).toHaveCount(1);
		await expect(page.locator('tbody tr .member')).toHaveText('Già socio/a');
		await page.locator('tbody tr a').first().click();

		await expect(page.getByText('Dichiara di essere già socio/a.')).toBeVisible();
		await expect(page.locator('.existing-member')).toContainText('POD, Indirizzo di fornitura');

		await page.getByRole('button', { name: 'Prendi in carico' }).click();
		const participant = page.getByRole('group', {
			name: 'Come è stato verificato il partecipante?'
		});
		await participant.getByLabel('Verificato dalla comunità fuori dalla piattaforma').check();
		await page.getByRole('button', { name: 'Registra verifica' }).click();
		await expect(page.getByText('Verifica registrata.')).toBeVisible();

		// Verified, but the register's data is still missing.
		const approve = page.getByRole('button', { name: 'Approva' });
		await expect(approve).toBeDisabled();

		// Rejecting offers an email in the applicant's words, sent by the operator.
		await page.getByRole('button', { name: 'Rifiuta' }).click();
		const mail = page.getByRole('link', { name: "apri l'email" });
		await expect(mail).toHaveAttribute('href', new RegExp(`^mailto:${encodeURIComponent(email)}\\?subject=`));
		await expect(mail).toHaveAttribute('href', new RegExp(encodeURIComponent(ref)));
		await page.getByRole('button', { name: 'Annulla' }).click();

		// The first missing field is offered first: the POD, then the address.
		for (const [field, value, recorded] of [
			['pod_code', 'IT001E12345679', 1],
			['supply_address', 'Via Example 2, Example Town', 2]
		] as const) {
			await expect(page.getByLabel('Campo')).toHaveValue(field);
			await page.getByLabel('Valore corretto').fill(value);
			await page.getByLabel('Nota (obbligatoria)').fill('Dal registro dei soci');
			await page.getByRole('button', { name: 'Registra correzione' }).click();
			await expect(page.locator('section.revisions ol.verifications li')).toHaveCount(recorded);
		}

		await expect(
			page.getByText('POD e indirizzo di fornitura completati dal registro dei soci.')
		).toBeVisible();
		await expect(approve).toBeEnabled();
		await approve.click();
		await expect(page.locator('.status')).toHaveText('Approvata');
	});
});
