import { test, expect, type Page } from '@playwright/test';

/**
 * The wizard on a phone: 360px, the narrowest common Android width, with touch.
 *
 * Measured on 2026-10-02 before these checks existed: the step labels made a
 * 422px row on a 360px screen, so every step of the wizard scrolled sideways (and
 * a mobile browser zoomed the whole page out to fit it); every input was 15px,
 * which iOS Safari answers by zooming into the field on focus; and the language
 * buttons were 23px tall. Each check below is one of those, asserted on every
 * step a participant passes through, so a new step or field inherits them.
 */

const REC = process.env.E2E_REC ?? 'example';

/** Everything a page must satisfy to be usable on the phone. */
async function expectFitsAPhone(page: Page, where: string) {
	const m = await page.evaluate(() => {
		const vw = document.documentElement.clientWidth;
		const visible = (e: Element) => (e as HTMLElement).offsetParent !== null;
		const name = (e: Element) =>
			(e as HTMLInputElement).name || e.id || (e as HTMLElement).innerText?.trim().slice(0, 30) || e.tagName;
		return {
			overflow: document.documentElement.scrollWidth - vw,
			beyondTheEdge: [...document.querySelectorAll('body *')]
				.filter((e) => visible(e) && e.getBoundingClientRect().right > vw + 1)
				.map(name)
				.slice(0, 5),
			smallFields: [
				...document.querySelectorAll(
					'input:not([type=checkbox]):not([type=radio]):not([type=file]):not([type=hidden]), select, textarea'
				)
			]
				.filter(visible)
				.filter((e) => parseFloat(getComputedStyle(e).fontSize) < 16)
				.map((e) => `${name(e)} ${getComputedStyle(e).fontSize}`),
			smallLocaleButtons: [...document.querySelectorAll('.locale-btn')]
				.map((e) => e.getBoundingClientRect())
				.filter((r) => r.width < 44 || r.height < 44)
				.map((r) => `${Math.round(r.width)}x${Math.round(r.height)}`),
			// A checkbox is small; what a finger hits is the label wrapped around it.
			smallCheckboxTargets: [...document.querySelectorAll('input[type=checkbox]')]
				.filter(visible)
				.map((e) => e.closest('label') ?? e)
				.filter((l) => l.getBoundingClientRect().height < 24)
				.map(name)
		};
	});
	expect(m.overflow, `${where}: scrolls sideways (${m.beyondTheEdge.join(', ')})`).toBeLessThanOrEqual(0);
	expect(m.smallFields, `${where}: fields under 16px make iOS zoom in`).toEqual([]);
	expect(m.smallLocaleButtons, `${where}: language buttons under 44px`).toEqual([]);
	expect(m.smallCheckboxTargets, `${where}: checkbox targets under 24px`).toEqual([]);
}

test.describe('The wizard on a phone', () => {
	// Needs the live backend `scripts/e2e.sh ui` starts: the wizard renders its
	// steps from the community's config, which `pnpm dev` alone does not serve.
	test.skip(!process.env.PLAYWRIGHT_BASE_URL, 'run through scripts/e2e.sh ui');
	test.use({ viewport: { width: 360, height: 760 }, isMobile: true, hasTouch: true });

	test('the landing and community pages fit', async ({ page }) => {
		await page.goto('/');
		await expectFitsAPhone(page, 'landing');
		await page.goto(`/${REC}`);
		await expectFitsAPhone(page, 'community page');
	});

	test('every step fits, names the step it is on, and a long value wraps on review', async ({ page }) => {
		await page.goto(`/${REC}/onboarding`);

		// consents
		await expect(page.locator('.step-current')).toHaveText(/^1\/\d+ · /);
		await expectFitsAPhone(page, 'consents');
		for (const label of [
			'Acconsento al trattamento dei dati personali ai sensi del GDPR',
			'Accetto il regolamento della comunita energetica',
			'Accetto lo statuto della comunita energetica'
		]) {
			await page.getByLabel(label).check();
		}
		await page.getByRole('button', { name: 'Avanti' }).click();

		// personal
		await expect(page.locator('input[name="first_name"]')).toBeVisible();
		await expect(page.locator('.step-current')).toHaveText(/^2\/\d+ · /);
		await expectFitsAPhone(page, 'personal');
		await page.locator('input[name="first_name"]').fill('Mario');
		await page.locator('input[name="last_name"]').fill('Rossi');
		await page.locator('input[name="fiscal_code"]').fill('RSSMRA85T10A562S');
		await page.locator('input[name="pod_code"]').fill('IT001E12345678');
		// Long and unbroken on purpose: the review row is label and value side by side.
		await page
			.locator('input[name="email"]')
			.fill('a-rather-long-address-to-check-a-narrow-screen-wraps@example.org');
		await page.getByRole('button', { name: 'Avanti' }).click();

		// review
		await expect(page.getByRole('button', { name: 'Invia' })).toBeVisible();
		await expectFitsAPhone(page, 'review');
	});
});
