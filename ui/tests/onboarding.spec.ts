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
