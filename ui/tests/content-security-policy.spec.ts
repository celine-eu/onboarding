import { test, expect } from '@playwright/test';

/**
 * The UI sends its own Content-Security-Policy (`kit.csp` in `svelte.config.js`): SvelteKit's
 * inline bootstrap carries the response's nonce, `script-src` allows no 'unsafe-inline', and a
 * page loads without the browser refusing anything. Needs no backend: the public landing and
 * the console's sign-in gate both render a page before any API call answers.
 */

function directive(policy: string, name: string): string | undefined {
	return policy
		.split(';')
		.map((part) => part.trim())
		.find((part) => part === name || part.startsWith(`${name} `));
}

for (const path of ['/', '/admin']) {
	test(`${path}: inline scripts run by nonce, not by 'unsafe-inline'`, async ({ page }) => {
		const refused: string[] = [];
		await page.addInitScript(() => {
			document.addEventListener('securitypolicyviolation', (event) => {
				console.error(`csp-refused ${event.effectiveDirective} ${event.blockedURI}`);
			});
		});
		page.on('console', (message) => {
			if (message.text().startsWith('csp-refused')) refused.push(message.text());
		});

		const response = await page.goto(path);
		expect(response).not.toBeNull();
		const policy = (await response!.allHeaders())['content-security-policy'];
		expect(policy, 'the UI sends a Content-Security-Policy').toBeTruthy();

		const scriptSrc = directive(policy, 'script-src');
		expect(scriptSrc).toBeTruthy();
		expect(scriptSrc).not.toContain("'unsafe-inline'");
		const nonce = /'nonce-([^']+)'/.exec(scriptSrc!)?.[1];
		expect(nonce, 'script-src carries a nonce').toBeTruthy();
		expect(directive(policy, 'object-src')).toBe("object-src 'none'");
		expect(directive(policy, 'frame-ancestors')).toBe("frame-ancestors 'none'");

		const html = await response!.text();
		const inline = [...html.matchAll(/<script\b([^>]*)>/g)]
			.map((match) => match[1])
			.filter((attributes) => !/\ssrc=/.test(attributes) && !/application\/json/.test(attributes));
		expect(inline.length).toBeGreaterThan(0);
		for (const attributes of inline) {
			expect(attributes).toContain(`nonce="${nonce}"`);
		}

		await page.waitForLoadState('load');
		await page.waitForTimeout(1_000);
		expect(refused).toEqual([]);
	});
}
