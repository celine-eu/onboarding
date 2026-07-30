import { redirect } from '@sveltejs/kit';
import { getAdminMe, AdminDeniedError, type AdminMe } from '$lib/api/client';
import type { LayoutLoad } from './$types';

// The gate runs in the browser, not on the server. Client-side navigation into
// /admin never touches the ingress, so a server-side check would be skipped
// exactly when somebody arrives from the wizard — which is why `apps/grid` puts
// the gate here too.
export const ssr = false;

export const load: LayoutLoad = async ({ url }) => {
	// Public: reachable precisely when the caller has been refused everything else.
	if (url.pathname === '/admin/denied') {
		return { me: null as AdminMe | null };
	}

	try {
		return { me: await getAdminMe() };
	} catch (e) {
		// 403 means signed in and granted nothing — a different screen, not a
		// login loop. Anything else (401, network) means go and get a session;
		// `getAdminMe` has already navigated on a 401, so reaching here with one
		// is unusual but harmless to repeat.
		if (e instanceof AdminDeniedError) {
			redirect(302, '/admin/denied?reason=' + encodeURIComponent(e.message));
		}
		redirect(302, '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.href));
	}
};
