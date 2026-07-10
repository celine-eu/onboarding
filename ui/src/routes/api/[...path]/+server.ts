import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';

const API_BASE_URL = env.API_BASE_URL ?? 'http://localhost:8040';

function forwardHeaders(request: Request): Headers {
	const headers = new Headers(request.headers);
	headers.delete('host');
	headers.delete('connection');
	headers.delete('content-length');
	return headers;
}

async function proxy({ request, params, url }: Parameters<RequestHandler>[0]): Promise<Response> {
	const target = new URL(`/api/${params.path ?? ''}${url.search}`, API_BASE_URL);
	const body = ['GET', 'HEAD'].includes(request.method) ? undefined : await request.arrayBuffer();
	const upstream = await fetch(target, {
		method: request.method,
		headers: forwardHeaders(request),
		body,
		redirect: 'manual'
	});
	return new Response(upstream.body, {
		status: upstream.status,
		statusText: upstream.statusText,
		headers: upstream.headers
	});
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const PUT = proxy;
export const DELETE = proxy;
export const OPTIONS = proxy;
