import { test, expect } from '@playwright/test';

/**
 * The participant wizard, which is anonymous and must stay that way.
 *
 * These previously walked `/` → `/onboarding`, which stopped being the shape of
 * the app when it became multi-community: the landing page is a community
 * picker, and the wizard lives under `/{rec}/onboarding`.
 */

const REC = process.env.E2E_REC ?? 'example';

test.describe('Landing', () => {
	test('renders the community finder', async ({ page }) => {
		await page.goto('/');
		// Role-scoped: the app title appears both in the header link and as the
		// page heading, and a bare text match is ambiguous.
		await expect(page.getByRole('heading', { name: 'CER Onboarding' })).toBeVisible();
	});
});

test.describe('Community page', () => {
	test('offers the wizard', async ({ page }) => {
		await page.goto(`/${REC}`);
		const start = page.getByRole('link', { name: /inizia|start/i });
		await expect(start).toBeVisible();
		await start.click();
		await expect(page).toHaveURL(new RegExp(`/${REC}/onboarding`));
	});

	test('links to the console, which is now behind authentication', async ({ page }) => {
		await page.goto(`/${REC}`);
		const console_ = page.getByRole('link', { name: /console operatori/i });
		await expect(console_).toBeVisible();
		await expect(console_).toHaveAttribute('href', `/admin/${REC}`);
	});
});

test.describe('Wizard', () => {
	test('renders the first step', async ({ page }) => {
		await page.goto(`/${REC}/onboarding`);
		await expect(page.getByText(/adesione|membership/i).first()).toBeVisible();
	});

	test('the language switcher works', async ({ page }) => {
		// On the community page: "Inizia adesione" is the wizard CTA, not landing copy.
		await page.goto(`/${REC}`);
		await expect(page.getByText('Inizia adesione')).toBeVisible();
		await page.getByRole('button', { name: 'EN' }).click();
		await expect(page.getByText('Start onboarding')).toBeVisible();
		await page.getByRole('button', { name: 'IT' }).click();
		await expect(page.getByText('Inizia adesione')).toBeVisible();
	});
});

test.describe('A community that publishes no statute', () => {
	test('asks for the documents it declares and not for a statute', async ({ page }) => {
		await page.goto(`/${REC}-no-statute/onboarding`);
		const gdpr = page.getByLabel('Acconsento al trattamento dei dati personali ai sensi del GDPR');
		const policy = page.getByLabel('Accetto il regolamento della comunita energetica');
		await expect(gdpr).toBeVisible();
		await expect(policy).toBeVisible();
		await expect(page.getByLabel('Accetto lo statuto della comunita energetica')).toHaveCount(0);

		const next = page.getByRole('button', { name: 'Avanti' });
		await gdpr.check();
		await expect(next).toBeDisabled();
		await policy.check();
		await expect(next).toBeEnabled();
	});
});

test.describe('Wizard without document processing or SMS', () => {
	// Needs the live backend `scripts/e2e.sh` starts, which runs with both switches off
	// (no EXTRACTION_ENABLED or LLM_BASE_URL; a real SMS_PROVIDER without
	// DPA_SMS_SIGNED). Against `pnpm dev` there is no API to submit to.
	test.skip(!process.env.PLAYWRIGHT_BASE_URL, 'run through scripts/e2e.sh ui');

	test('a full run submits with no upload control, no phone step and no request for either', async ({ page }) => {
		const config = await (await page.request.get(`/api/${REC}/config`)).json();
		// Otherwise this passes vacuously on a deployment where scanning is on.
		expect(config.features).toEqual({
			document_upload: false,
			document_scan: false,
			id_document_upload: false,
			id_document_scan: false,
			phone_verification: false
		});
		// The seeded community asks for `phone_verify`; with SMS off the step is left out.
		expect(JSON.stringify(config.steps)).toContain('phone_verify');

		const documentRequests: string[] = [];
		page.on('request', (req) => {
			const path = new URL(req.url()).pathname;
			if (/\/(extract|extract-id|documents|extractions|verify-phone|confirm-phone)(\/|$)/.test(path)) {
				documentRequests.push(`${req.method()} ${path}`);
			}
		});
		const noUploadControl = () => expect(page.locator('input[type="file"]')).toHaveCount(0);

		await page.goto(`/${REC}/onboarding`);

		// consents
		await noUploadControl();
		for (const label of [
			'Acconsento al trattamento dei dati personali ai sensi del GDPR',
			'Accetto il regolamento della comunita energetica',
			'Accetto lo statuto della comunita energetica'
		]) {
			await page.getByLabel(label).check();
		}
		await page.getByRole('button', { name: 'Avanti' }).click();

		// personal: the manual fields only
		await expect(page.locator('input[name="first_name"]')).toBeVisible();
		await noUploadControl();
		await page.locator('input[name="first_name"]').fill('Mario');
		await page.locator('input[name="last_name"]').fill('Rossi');
		await page.locator('input[name="fiscal_code"]').fill('RSSMRA85T10A562S');
		await page.locator('input[name="pod_code"]').fill('IT001E12345678');
		await page.locator('input[name="email"]').fill('wizard-e2e@example.org');
		await page.getByRole('button', { name: 'Avanti' }).click();

		// review
		await noUploadControl();
		await page.getByRole('button', { name: 'Invia' }).click();

		await expect(page.getByText('Adesione inviata con successo!')).toBeVisible();
		expect(documentRequests).toEqual([]);
	});
});

/**
 * The ID-card upload follows `features.id_document_upload`, not the bill's switch.
 *
@verifies REQ-0051
 *
 * The harness runs with document processing off, so the bill's features are turned
 * on in the `/config` answer the page reads; what is under test is what the wizard
 * renders from them. Nothing is uploaded.
 */
test.describe('The identity document has its own switch', () => {
	test.skip(!process.env.PLAYWRIGHT_BASE_URL, 'run through scripts/e2e.sh ui');

	for (const idDocument of [false, true]) {
		test(`with the ID document ${idDocument ? 'on' : 'off'} the personal step offers ${idDocument ? 'both uploads' : 'the bill alone'}`, async ({ page }) => {
			await page.route(`**/api/${REC}/config`, async (route) => {
				const response = await route.fetch();
				const config = await response.json();
				config.features = {
					...config.features,
					document_upload: true,
					document_scan: true,
					id_document_upload: idDocument,
					id_document_scan: idDocument
				};
				await route.fulfill({ response, json: config });
			});

			// The community's layout loads `/config` on the server for a direct visit,
			// where no route reaches it; arriving by a client-side navigation makes the
			// browser load it, through the route above.
			await page.goto('/');
			await page.waitForLoadState('networkidle');
			await page.evaluate((href) => {
				const link = Object.assign(document.createElement('a'), { href, id: 'e2e-to-wizard' });
				link.textContent = 'wizard';
				document.querySelector('body > div')!.appendChild(link);
			}, `/${REC}/onboarding`);
			await page.locator('#e2e-to-wizard').click();
			await expect(page).toHaveURL(new RegExp(`/${REC}/onboarding$`));

			// Every box on the consents step, whatever this community declares.
			for (const box of await page.getByRole('checkbox').all()) await box.check();
			await page.getByRole('button', { name: 'Avanti' }).click();

			await expect(page.locator('input[name="first_name"]')).toBeVisible();
			await expect(page.getByText('Carica la tua bolletta')).toBeVisible();
			await expect(page.getByText("Carica il tuo documento d'identità")).toHaveCount(idDocument ? 1 : 0);
			await expect(page.locator('input[type="file"]')).toHaveCount(idDocument ? 2 : 1);
		});
	}
});
