import adapter from '@sveltejs/adapter-node';

/**
 * Content-Security-Policy: the UI sends it, not the ingress. SvelteKit renders every page on
 * request and gives its inline bootstrap a fresh nonce, so `script-src` carries no
 * 'unsafe-inline'. The ingress may not send this header as well: ingress-nginx's
 * `custom-headers` replaces a header of the same name. Styles keep 'unsafe-inline' for Svelte's
 * `style=` attributes and transitions. README, "HTTP hardening".
 */
const csp = {
	mode: 'nonce',
	directives: {
		'default-src': ['self'],
		'script-src': ['self'],
		'style-src': ['self', 'unsafe-inline'],
		'img-src': ['self', 'data:', 'blob:'],
		'font-src': ['self'],
		'connect-src': ['self'],
		'object-src': ['none'],
		'base-uri': ['self'],
		'form-action': ['self'],
		'frame-ancestors': ['none']
	}
};

/** @type {import('@sveltejs/kit').Config} */
const config = {
	compilerOptions: {
		// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
		runes: ({ filename }) => (filename.split(/[/\\]/).includes('node_modules') ? undefined : true)
	},
	kit: {
		// adapter-auto only supports some environments, see https://svelte.dev/docs/kit/adapter-auto for a list.
		// If your environment is not supported, or you settled on a specific environment, switch out the adapter.
		// See https://svelte.dev/docs/kit/adapters for more information about adapters.
		adapter: adapter(),
		csp
	}
};

export default config;
