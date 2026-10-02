import { ApiError } from './client';

/** What the wizard shows for a failure: a sentence in the participant's language,
 *  and — under it — what support needs to find the cause. */
export interface ShownError {
	message: string;
	/** The API's error reference, also in the server log. */
	reference: string | null;
	/** The HTTP status, when there was a response at all. */
	status: number | null;
}

type Translate = (key: string) => string;

/** Turn anything a call threw into a `ShownError`. A server's own wording, a
 *  traceback or an upstream message never becomes the sentence: a 4xx `detail`
 *  the API wrote for people is the one exception, and only for a 4xx. */
export function describeError(e: unknown, t: Translate): ShownError {
	if (e instanceof ApiError) {
		const shown = { reference: e.reference, status: e.status };
		if (e.code === 'extraction_unavailable') {
			return { message: t('onboarding.error_extraction_unavailable'), ...shown };
		}
		if (e.status === 429) return { message: t('onboarding.error_rate_limited'), ...shown };
		if (e.status >= 500) return { message: t('onboarding.error_server'), ...shown };
		return { message: e.detail ?? t('onboarding.error_generic'), ...shown };
	}
	// `fetch` rejects with a TypeError when no response came back at all.
	if (e instanceof TypeError) {
		return { message: t('onboarding.error_network'), reference: null, status: null };
	}
	return { message: t('onboarding.error_generic'), reference: null, status: null };
}
