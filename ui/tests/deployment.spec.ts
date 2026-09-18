import { spawnSync } from 'node:child_process';
import { test, expect, type Page } from '@playwright/test';

/**
 * One person, all the way through a **deployed** community's wizard.
 *
 * `onboarding.spec.ts` walks the hermetic fixture `scripts/e2e.sh` builds: four
 * steps, no SMS, no extraction, a community seeded for the occasion. That run
 * proves the wizard compiles and submits. It cannot prove that the wizard a
 * community actually configured — its steps, its coverage rules, its consent
 * documents, its sharing offers — is walkable by a human being, because there
 * is no such community in this repository and there must not be one.
 *
 * So this file walks whatever wizard the environment points it at, in whatever
 * order that deployment declared, and ends by reading the row back out of the
 * API. The two specs are separate on purpose: the fixture's four steps and a
 * deployment's seven are different wizards, and folding them together would
 * mean a run that silently skips the steps it cannot find.
 *
 * ## Nothing below has a default, and that is deliberate
 *
 * This service is open source and ships no community of its own. A default here
 * would have to name one deployment's slug, its covered addresses, and the
 * container its SMS provider logs to — telling every reader that some stack they
 * cannot see is the one this suite means, and reporting green about a wizard
 * nobody deploys. Absent is the only answer that is true on every checkout. The
 * deployment supplies the values; this file supplies the behaviour.
 */

/** The variable, or null when it is unset or blank.
 *
 * Blank counts as unset so that an environment file carrying an empty
 * assignment skips the run rather than driving a browser at `""`.
 */
function env(name: string): string | null {
	return (process.env[name] ?? '').trim() || null;
}

/** Where the deployment's onboarding UI is served. Also tells `playwright.config.ts`
 *  not to start `pnpm dev`, which would have no API behind it. */
const BASE_URL = env('PLAYWRIGHT_BASE_URL');
/** The community slug the wizard lives under: `/{rec}/onboarding`. */
const REC = env('E2E_DEPLOYMENT_REC');
/** A street address the deployment's coverage rules accept. Geocoded for real,
 *  by the deployment's own geocoder, against the deployment's own rules. */
const ADDRESS = env('E2E_DEPLOYMENT_ADDRESS');
/** What that address must geocode to. Without it the eligibility step passes on
 *  any deployment whose rules happen to say yes to everything. */
const MUNICIPALITY = env('E2E_DEPLOYMENT_MUNICIPALITY');
/** The container whose log the OTP is printed to.
 *
 * Its presence is the run's licence to invent a phone number: only a deployment
 * running a *logging* SMS provider has such a container, and only there does an
 * OTP for a made-up number go to a log file instead of to a stranger's handset.
 * A deployment with a real gateway has nothing to put here and skips.
 */
const SMS_LOG_CONTAINER = env('E2E_DEPLOYMENT_SMS_LOG_CONTAINER');

const REQUIRED: Record<string, string | null> = {
	PLAYWRIGHT_BASE_URL: BASE_URL,
	E2E_DEPLOYMENT_REC: REC,
	E2E_DEPLOYMENT_ADDRESS: ADDRESS,
	E2E_DEPLOYMENT_MUNICIPALITY: MUNICIPALITY,
	E2E_DEPLOYMENT_SMS_LOG_CONTAINER: SMS_LOG_CONTAINER
};

const ABSENT = Object.entries(REQUIRED)
	.filter(([, value]) => !value)
	.map(([name]) => name);

/** Name every missing variable, not the first one.
 *
 * One run should tell somebody everything they have to set. Reporting them one
 * at a time turns pointing the suite at a deployment into a guessing game, and
 * a guessing game is what gets a check switched off.
 */
const SKIP_REASON =
	'\n  DEPLOYMENT WIZARD RUN DID NOT RUN — no deployment is configured\n' +
	`  unset: ${ABSENT.join(', ')}\n` +
	'  This is a skip, not a pass. Nobody walked the funnel. Point the suite at a\n' +
	'  deployment — the deployment repository owns these values, not this one —\n' +
	'  and run again.\n';

// **Printed, not just attached to the skip.** Playwright's reporters show the
// skipped test and count it; none of them prints the reason a `test.skip`
// carries. A skip whose reason is invisible is a line of dashes that scrolls
// past, which is how a check stops running without anybody deciding it should.
if (ABSENT.length > 0) {
	process.stderr.write(SKIP_REASON);
}

// ── the identity ────────────────────────────────────────────────────────────
//
// **Fresh every run, though nothing forces it to be.** There is no unique index
// on `fiscal_code`, `pod_code` or `email`, and neither the create nor the update
// route looks for a duplicate — the only uniqueness on `submissions` is `ref`,
// which the server generates. So a second run for the same person is accepted,
// and re-runnability does not depend on any of this.
//
// It is still generated per run, for two reasons that are not about the schema:
// the OTP is found by grepping a shared log for **this run's** number, and a
// deployment's operators have to be able to tell one test row from the next
// without reading timestamps.

const RUN_ID = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

const FIRST_NAMES = ['Marco', 'Luca', 'Anna', 'Giulia', 'Paolo', 'Elena', 'Davide', 'Chiara'];
const LAST_NAMES = ['Rossi', 'Bianchi', 'Ferrari', 'Esposito', 'Romano', 'Colombo', 'Greco'];
/** Birthplace codes of large, unrelated cities. The wizard never checks that the
 *  birthplace has anything to do with the community, and picking one of the
 *  community's own comuni would put a deployment's geography in this file. */
const BIRTHPLACES = ['H501', 'F205', 'L219', 'D612', 'G273', 'A662'];
const MONTH_LETTERS = 'ABCDEHLMPRST';
/** Character values for the odd (1-based) positions of the fiscal code. Digits
 *  reuse the first ten entries, which is why one table serves both. */
// prettier-ignore
const ODD_VALUES = [
	1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18,
	20, 11, 3, 6, 8, 12, 14, 16, 10, 22, 25, 24, 23
];

function pick<T>(items: T[]): T {
	return items[Math.floor(Math.random() * items.length)];
}

const VOWELS = 'AEIOU';
const isVowel = (c: string) => VOWELS.includes(c);

/** The surname triple: consonants first, then vowels, padded with X. */
function surnameCode(surname: string): string {
	const s = surname.toUpperCase().replace(/[^A-Z]/g, '');
	const consonants = [...s].filter((c) => !isVowel(c));
	const vowels = [...s].filter(isVowel);
	return [...consonants, ...vowels, 'X', 'X', 'X'].slice(0, 3).join('');
}

/** The forename triple, which differs from the surname's in one place: with four
 *  or more consonants the second is dropped. MARCO has three and keeps MRC;
 *  STEFANO has four and gives SFN, not STF. */
function firstNameCode(name: string): string {
	const s = name.toUpperCase().replace(/[^A-Z]/g, '');
	const consonants = [...s].filter((c) => !isVowel(c));
	if (consonants.length >= 4) return consonants[0] + consonants[2] + consonants[3];
	const vowels = [...s].filter(isVowel);
	return [...consonants, ...vowels, 'X', 'X', 'X'].slice(0, 3).join('');
}

/**
 * The sixteenth character, computed rather than guessed.
 *
 * The server validates the shape only — `validators/fiscal_code.py` is a regex
 * whose last term is a bare `[A-Z]`, so any letter would be accepted today. It
 * is computed anyway: a code that is wrong in a way nothing currently looks at
 * is a row of test data that becomes invalid the day somebody adds the check,
 * and the point of this run is to leave a row that stands up.
 */
function checkCharacter(fifteen: string): string {
	let sum = 0;
	for (let i = 0; i < 15; i++) {
		const c = fifteen[i];
		const index = c >= '0' && c <= '9' ? c.charCodeAt(0) - 48 : c.charCodeAt(0) - 65;
		// Positions are 1-based: index 0 is the first character, and it is odd.
		sum += i % 2 === 0 ? ODD_VALUES[index] : index;
	}
	return String.fromCharCode(65 + (sum % 26));
}

function makeIdentity() {
	const firstName = pick(FIRST_NAMES);
	const lastName = pick(LAST_NAMES);
	const year = String(50 + Math.floor(Math.random() * 50)).padStart(2, '0');
	const month = pick([...MONTH_LETTERS]);
	// 1..28 only. Days 29-31 are legal in some months and not others, and the
	// server's regex encodes exactly which — staying inside the month every
	// month has keeps a generated code from failing one run in forty.
	const day = String(1 + Math.floor(Math.random() * 28)).padStart(2, '0');
	const fifteen =
		surnameCode(lastName) + firstNameCode(firstName) + year + month + day + pick(BIRTHPLACES);
	return {
		firstName,
		lastName,
		fiscalCode: fifteen + checkCharacter(fifteen),
		// `IT` + 3 digits + `E` + 8-9 digits, which is all either side checks.
		podCode: `IT001E${String(Date.now()).slice(-8)}`,
		email: `wizard-e2e-${RUN_ID}@example.org`,
		// Italian mobile, because `normalize_mobile` parses with region IT and
		// refuses anything libphonenumber does not call a mobile line — a landline
		// would be rejected before an OTP was ever generated.
		phone: `+393${2 + Math.floor(Math.random() * 8)}${Array.from(
			{ length: 8 },
			() => Math.floor(Math.random() * 10)
		).join('')}`
	};
}

// ── the OTP ─────────────────────────────────────────────────────────────────

/**
 * The code this run's number was sent, taken from the container's log.
 *
 * A logging SMS provider writes one line per send and never sends anything, so
 * the log is the only place the code exists. Three things make reading it safe:
 * the line is matched on **this run's** E.164 number, so a concurrent run's code
 * cannot be picked up; the **last** match wins, so a resend supersedes the code
 * it replaced; and the window is short, so a number that somehow repeats across
 * days cannot serve a stale code.
 *
 * `docker logs` writes application output to stderr, so both streams are read.
 */
function otpFromLog(container: string, phone: string): string | null {
	const proc = spawnSync('docker', ['logs', '--since', '3m', container], {
		encoding: 'utf8',
		maxBuffer: 64 * 1024 * 1024
	});
	if (proc.error) {
		throw new Error(
			`Could not read the SMS log: running \`docker logs ${container}\` failed ` +
				`(${proc.error.message}). E2E_DEPLOYMENT_SMS_LOG_CONTAINER must name a ` +
				`container this user can read logs from.`
		);
	}
	if (proc.status !== 0) {
		throw new Error(
			`\`docker logs ${container}\` exited ${proc.status}: ${(proc.stderr || '').trim()}`
		);
	}
	const log = `${proc.stdout ?? ''}${proc.stderr ?? ''}`;
	// The provider's own wording, from `services/sms.py`. Matching the number as
	// well as the shape is what keeps two runs from reading each other's code.
	const escaped = phone.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	const pattern = new RegExp(`OTP for ${escaped} is (\\d{4,10})\\b`, 'g');
	const codes = [...log.matchAll(pattern)].map((m) => m[1]);
	return codes.length > 0 ? codes[codes.length - 1] : null;
}

/** Poll for it: the line is written before the response is sent, but `docker logs`
 *  reads a file the daemon flushes on its own schedule. */
async function waitForOtp(container: string, phone: string): Promise<string> {
	const deadline = Date.now() + 20_000;
	let code: string | null = null;
	while (Date.now() < deadline) {
		code = otpFromLog(container, phone);
		if (code) return code;
		await new Promise((r) => setTimeout(r, 500));
	}
	throw new Error(
		`No OTP for ${phone} in the last 3 minutes of \`docker logs ${container}\`.\n` +
			`  Either the deployment is not running a logging SMS provider (in which case a\n` +
			`  real message went to a made-up number and this suite must not be pointed here),\n` +
			`  or E2E_DEPLOYMENT_SMS_LOG_CONTAINER names the wrong container.`
	);
}

// ── the run ─────────────────────────────────────────────────────────────────

interface Created {
	id: string;
	session_token: string;
}

test.describe('A deployed community wizard, walked end to end', () => {
	test.skip(ABSENT.length > 0, SKIP_REASON);

	// Geocoding, an OTP round trip and seven steps. The default 30s is not enough
	// and a timeout here reads as a broken wizard rather than a slow one.
	test.setTimeout(180_000);

	test('a person adheres, and the submission is there afterwards', async ({ page, request }) => {
		const rec = REC!;
		const identity = makeIdentity();

		const config = await (await request.get(`/api/${rec}/config`)).json();
		// This wizard is the one with SMS on. Asserted rather than assumed: with
		// phone verification off the step is dropped from `steps` and everything
		// below would pass while proving nothing about the funnel that exists.
		expect(config.features.phone_verification).toBe(true);
		// A community may declare a step as `{custom, title}` to relabel it; the key
		// is what the wizard branches on either way.
		const steps: string[] = config.steps.map((s: unknown) =>
			typeof s === 'string' ? s : (s as { custom: string }).custom
		);
		const at = (name: string) => {
			const i = steps.indexOf(name);
			expect(i, `the wizard this suite is pointed at has no \`${name}\` step`).toBeGreaterThan(-1);
			return i;
		};
		// The relative order this run walks in. Asserted rather than assumed
		// because a community reorders its own wizard, and a run that fills the
		// eligibility address on the statute step fails for the wrong reason.
		expect(at('consents')).toBeLessThan(at('personal'));
		expect(at('personal')).toBeLessThan(at('phone_verify'));
		expect(at('phone_verify')).toBeLessThan(at('eligibility'));
		expect(at('eligibility')).toBeLessThan(at('statute'));
		expect(steps[steps.length - 1]).toBe('review');
		// Whatever this community put between the phone and the address.
		const declaredSteps = steps.slice(at('phone_verify') + 1, at('eligibility'));

		// The session token is handed out **once**, in the body of the create
		// response, and the app keeps it in a module-level variable that no
		// `page.evaluate` can reach. Reading it off the wire is the only way to
		// query the row afterwards as something other than the page itself.
		let created: Created | null = null;
		page.on('response', async (res) => {
			if (res.request().method() !== 'POST' || res.status() !== 201) return;
			if (new URL(res.url()).pathname !== `/api/${rec}/submissions`) return;
			created = (await res.json()) as Created;
		});

		await page.goto(`/${rec}/onboarding`);

		// ── consents ────────────────────────────────────────────────────────
		// Only two here. A deployment that gives the statute its own step does not
		// render the statute checkbox on this one — it belongs to that step, and
		// checking for it here would fail on exactly the wizard this run exists for.
		await page
			.getByLabel('Acconsento al trattamento dei dati personali ai sensi del GDPR')
			.check();
		await page.getByLabel('Accetto il regolamento della comunita energetica').check();
		await page.getByRole('button', { name: 'Avanti' }).click();

		// Clicking past the consents is what creates the row.
		await expect.poll(() => created?.id, { timeout: 20_000 }).toBeTruthy();
		const { id, session_token: sessionToken } = created!;

		// ── personal ────────────────────────────────────────────────────────
		await expect(page.locator('input[name="first_name"]')).toBeVisible();
		await page.locator('input[name="first_name"]').fill(identity.firstName);
		await page.locator('input[name="last_name"]').fill(identity.lastName);
		await page.locator('input[name="fiscal_code"]').fill(identity.fiscalCode);
		await page.locator('input[name="pod_code"]').fill(identity.podCode);
		await page.locator('input[name="email"]').fill(identity.email);
		// Filled here as well as on the verification step: this is where the
		// wizard saves it, and the verification step binds the same value.
		await page.locator('input[name="phone"]').fill(identity.phone);
		await page.getByRole('button', { name: 'Avanti' }).click();

		// ── phone_verify ────────────────────────────────────────────────────
		const phoneInput = page.locator('#verify_phone');
		await expect(phoneInput).toBeVisible();
		await expect(phoneInput).toHaveValue(identity.phone);
		await page.getByRole('button', { name: 'Invia codice' }).click();
		// The hint only appears once the API has accepted the send, so waiting for
		// it means the log line is already written by the time it is read.
		await expect(page.getByText('Ti abbiamo inviato un codice via SMS')).toBeVisible();

		const code = await waitForOtp(SMS_LOG_CONTAINER!, identity.phone);
		await page.locator('#otp_code').fill(code);
		await page.getByRole('button', { name: 'Conferma' }).click();
		await expect(page.getByText('Numero di telefono verificato!')).toBeVisible();
		await page.getByRole('button', { name: 'Avanti' }).click();

		// ── whatever the deployment put between here and eligibility ────────
		// Extra-field steps are declared per community, so they are walked by what
		// the manifest says rather than by name. Only required fields are filled:
		// a wizard that blocks on something this loop does not know how to answer
		// should fail here, loudly, rather than be worked around.
		await fillExtraSteps(page, config, declaredSteps);

		// ── eligibility ─────────────────────────────────────────────────────
		const addressInput = page.locator('input[name="eligibility_address"]');
		await expect(addressInput).toBeVisible();
		await addressInput.fill(ADDRESS!);
		// Exact, or it also matches "Verifica in corso..." while the geocoder runs.
		await page.getByRole('button', { name: 'Verifica', exact: true }).click();
		await expect(page.getByText("Il tuo indirizzo e' nell'area coperta!")).toBeVisible({
			timeout: 60_000
		});
		// The geocoded comune, shown beside the verdict. Asserting it is what
		// separates "the coverage rules said yes" from "the geocoder found the
		// place this deployment's rules are written about".
		await expect(page.locator('.eligibility-detail')).toContainText(MUNICIPALITY!);
		await page.getByRole('button', { name: 'Avanti' }).click();

		// ── statute ─────────────────────────────────────────────────────────
		await page.getByLabel('Accetto lo statuto della comunita energetica').check();

		// The sharing offers render here and nowhere else in the wizard, and this
		// is the only step that can record a data-sharing consent. It is asserted
		// rather than treated as optional: a deployment that publishes offers and
		// never manages to show them has a broken funnel, and the "offers could not
		// be loaded" branch renders in this same place and would otherwise pass.
		await expect(page.getByText('Condividi i tuoi dati con la comunità')).toBeVisible();
		const offers = page.locator('.offer-card');
		await expect(offers.first()).toBeVisible();
		// The first card, deliberately. Where a community declares a primary offer
		// the others are inert until it is accepted, and the primary is ordered
		// first — so the first card is the one that can always be checked.
		await offers.first().getByRole('checkbox').check();
		await page.getByRole('button', { name: 'Avanti' }).click();

		// ── review ──────────────────────────────────────────────────────────
		await expect(page.getByText(identity.fiscalCode)).toBeVisible();
		await page.getByRole('button', { name: 'Invia' }).click();

		// The community's own success copy replaces the generic banner, so the
		// reference is what every deployment's success card has in common.
		const ref = page.locator('.success-ref');
		await expect(ref).toBeVisible({ timeout: 30_000 });
		const shownRef = (await ref.textContent())!.replace(/^Ref:\s*/, '').trim();
		expect(shownRef).not.toBe('');

		// ── the consequence ─────────────────────────────────────────────────
		//
		// **Read the row back, out of a different request.** A success card is a
		// screen; it renders from a promise that resolved. What this run exists to
		// prove is that a deployment which had never taken a submission now holds
		// one, so the claim is settled by asking the API for it.
		//
		// The session-gated read rather than the operator console: the console
		// wants a JWT carrying `submissions.read` from the deployment's realm, and
		// a run that has to be handed operator credentials is a run that stops
		// being used. This read costs nothing, crosses the same database, and
		// returns the stored values unmasked.
		const stored = await request.get(`/api/${rec}/submissions/${id}`, {
			headers: { 'X-Session-Token': sessionToken }
		});
		expect(stored.status()).toBe(200);
		const row = await stored.json();

		expect(row.ref).toBe(shownRef);
		expect(row.status).toBe('submitted');
		expect(row.rec_slug).toBe(rec);
		expect(row.first_name).toBe(identity.firstName);
		expect(row.last_name).toBe(identity.lastName);
		expect(row.fiscal_code).toBe(identity.fiscalCode);
		expect(row.pod_code).toBe(identity.podCode);
		expect(row.email).toBe(identity.email);
		// Rewritten to E.164 by the confirm, which is the evidence that the code
		// came back through the API rather than that the field was merely typed.
		expect(row.phone).toBe(identity.phone);
		expect(row.phone_verified).toBe(true);
		expect(row.phone_verified_at).not.toBeNull();
		// Persisted by the eligibility step, and the thing that later decides which
		// registry area the member is enrolled into.
		expect(row.supply_municipality).toBe(MUNICIPALITY);
		expect(row.gdpr_consent).toBe(true);
		expect(row.policy_consent).toBe(true);
		expect(row.statute_consent).toBe(true);
		// The evidence the backend refuses to record without: an offer id, the
		// version of the text that was shown, and a hash of what was rendered.
		expect(row.data_sharing_consent).toBe(true);
		expect(row.data_sharing_consent_offer_ids?.length).toBeGreaterThan(0);
		expect(row.data_sharing_consent_text_sha256).toMatch(/^[0-9a-f]{64}$/);
		expect(row.data_sharing_offers_presented?.length).toBeGreaterThan(0);
	});
});

/**
 * Walk the community-declared steps that sit between the known ones.
 *
 * `fields.extra` entries carry the step they belong to, so the steps that exist
 * only in a manifest are answered from the manifest. Anything not marked
 * required is left alone: the run is a person filling in what they are asked
 * for, not a fixture loader.
 */
async function fillExtraSteps(
	page: Page,
	config: { fields?: { extra?: ExtraField[] } },
	steps: string[]
): Promise<void> {
	const builtIn = new Set([
		'consents',
		'utility',
		'personal',
		'phone_verify',
		'eligibility',
		'statute',
		'review'
	]);
	const extras = config.fields?.extra ?? [];

	for (const step of steps) {
		// A built-in step here means the wizard is shaped in a way this run does
		// not know how to walk. Say so instead of clicking Avanti at it.
		expect(
			builtIn.has(step),
			`\`${step}\` sits between the phone and the address steps, and this run ` +
				`only knows how to answer community-declared steps there`
		).toBe(false);
		const fields = extras.filter((f) => f.step === step);
		for (const field of fields) {
			if (!field.required) continue;
			await answerField(page, field);
		}
		await page.getByRole('button', { name: 'Avanti' }).click();
	}
}

interface ExtraField {
	key: string;
	step: string;
	type: string;
	required?: boolean;
	options?: { value: string }[];
}

async function answerField(page: Page, field: ExtraField): Promise<void> {
	// Every control the wizard renders for an extra field carries the field key
	// as its id, which is the only handle that survives a relabelled manifest.
	const control = page.locator(`#${field.key}`);
	switch (field.type) {
		case 'boolean':
			await control.check();
			return;
		case 'select':
			// The first real option: index 0 is the empty placeholder, which is
			// exactly what a required field refuses.
			await control.selectOption(field.options?.[0]?.value ?? '');
			return;
		case 'number':
			await control.fill('1');
			return;
		default:
			await control.fill(`e2e-${RUN_ID}`);
	}
}
