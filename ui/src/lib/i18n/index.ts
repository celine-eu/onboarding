import i18n from 'sveltekit-i18n';
import type { Config } from 'sveltekit-i18n';

export const supportedLocales = ['it', 'en', 'es'] as const;
export type SupportedLocale = (typeof supportedLocales)[number];

export const defaultLocale: SupportedLocale = 'it';

// One loader per (locale, namespace). The `admin` namespace is routed to the
// console so the public wizard never downloads it.
const namespaces: Array<{ key: string; routes?: RegExp[] }> = [
	{ key: 'common' },
	{ key: 'onboarding' },
	{ key: 'admin', routes: [/^\/admin/] }
];

// Values interpolated into `{{placeholder}}`s. Any name is allowed; the type only
// has to admit them, since a missing one renders empty rather than failing.
type Payload = Record<string, string | number | null | undefined>;

const config: Config<Payload> = {
	loaders: supportedLocales.flatMap((locale) =>
		namespaces.map(({ key, routes }) => ({
			locale,
			key,
			routes,
			loader: async () => (await import(`./${locale}/${key}.json`)).default
		}))
	)
};

export const { t, locale, locales, loading, loadTranslations } = new i18n(config);

if (typeof document !== 'undefined') {
	locale.subscribe((value) => {
		if (value) document.documentElement.lang = value;
	});
}

const STORAGE_KEY = 'onboarding.locale';

function isSupported(value: unknown): value is SupportedLocale {
	return typeof value === 'string' && (supportedLocales as readonly string[]).includes(value);
}

/** The locale this browser last chose, or the default. Never throws: storage can
 *  be blocked, and a blocked store is simply a browser that chose nothing. */
export function storedLocale(): SupportedLocale {
	try {
		const value = globalThis.localStorage?.getItem(STORAGE_KEY);
		return isSupported(value) ? value : defaultLocale;
	} catch {
		return defaultLocale;
	}
}

/** Switch the UI language and remember the choice for this browser. */
export function setLocale(value: string): void {
	if (!isSupported(value)) return;
	locale.set(value);
	try {
		globalThis.localStorage?.setItem(STORAGE_KEY, value);
	} catch {
		// Remembering is a convenience; the switch itself has already happened.
	}
}

/** The BCP 47 tag `Intl` formatters take for a UI locale. */
export function intlLocale(value: string): string {
	return { it: 'it-IT', en: 'en-GB', es: 'es-ES' }[value as SupportedLocale] ?? 'it-IT';
}
