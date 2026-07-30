import { error } from '@sveltejs/kit';
import { createRecAdminApi } from '$lib/api/client';
import type { LayoutLoad } from './$types';

export const load: LayoutLoad = async ({ params, parent }) => {
	const { me } = await parent();
	const access = me?.recs.find((r) => r.slug === params.rec);

	// `/me` already answered which communities this operator may administer, so a
	// slug that is not among them is refused here rather than by letting every
	// endpoint on the page 403 separately.
	if (!access) {
		error(403, `Non hai permessi sulla comunita' '${params.rec}'.`);
	}

	return {
		rec: params.rec,
		access,
		api: createRecAdminApi(params.rec),
		can: (capability: string) => access.capabilities.includes(capability)
	};
};
