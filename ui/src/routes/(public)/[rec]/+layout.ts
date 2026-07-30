import { createRecApi, type SiteConfig } from '$lib/api/client';
import { error } from '@sveltejs/kit';
import type { LayoutLoad } from './$types';

export const load: LayoutLoad = async ({ params, fetch: kitFetch }) => {
	const { rec } = params;

	let config: SiteConfig;
	try {
		const res = await kitFetch(`/api/${rec}/config`);
		if (!res.ok) {
			throw error(404, `REC '${rec}' not found`);
		}
		config = await res.json();
	} catch (e: unknown) {
		if (e && typeof e === 'object' && 'status' in e) throw e;
		throw error(404, `REC '${rec}' not found`);
	}

	const recApi = createRecApi(rec);

	return { rec, config, recApi };
};
