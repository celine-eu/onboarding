export interface SubmissionResponse {
	id: string;
	status: string;
	session_token?: string;
	[key: string]: unknown;
}

export class ValidationError extends Error {
	fieldErrors: Record<string, string>;

	constructor(fieldErrors: Record<string, string>) {
		super('Validation failed');
		this.name = 'ValidationError';
		this.fieldErrors = fieldErrors;
	}
}

function parseValidationErrors(body: string): Record<string, string> | null {
	try {
		const parsed = JSON.parse(body);
		if (!Array.isArray(parsed?.detail)) return null;
		const fieldErrors: Record<string, string> = {};
		for (const err of parsed.detail) {
			if (!Array.isArray(err.loc) || !err.msg) continue;
			const field = err.loc[err.loc.length - 1];
			if (typeof field !== 'string') continue;
			fieldErrors[field] = String(err.msg).replace(/^Value error,\s*/i, '');
		}
		return Object.keys(fieldErrors).length > 0 ? fieldErrors : null;
	} catch {
		return null;
	}
}

let sessionToken: string | null = null;

export function setSessionToken(token: string) {
	sessionToken = token;
}

export function getSessionToken(): string | null {
	return sessionToken;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
	const headers: Record<string, string> = {
		'Content-Type': 'application/json',
		...(options?.headers as Record<string, string>)
	};
	if (sessionToken) {
		headers['X-Session-Token'] = sessionToken;
	}

	const res = await fetch(path, { ...options, headers });

	if (res.status === 410) {
		window.location.reload();
		throw new Error('Session expired');
	}

	if (!res.ok) {
		const body = await res.text();
		if (res.status === 422) {
			const fieldErrors = parseValidationErrors(body);
			if (fieldErrors) throw new ValidationError(fieldErrors);
		}
		throw new Error(`API error ${res.status}: ${body}`);
	}

	return res.json();
}

export interface SiteConfig {
	slug: string;
	name: string;
	locale: string;
	branding: { primary_color?: string; logo?: string };
	fields: {
		extra: Array<{
			key: string;
			label: string;
			type: string;
			step: string;
			required?: boolean;
			options?: Array<{ value: string; label: string }>;
			show_if?: { key: string; value: unknown };
			placeholder?: string;
			suffix?: string;
			[k: string]: unknown;
		}>;
		hidden: string[];
	};
	consent: Record<string, { version: string; file?: string; url?: string; required: boolean }>;
	steps: (string | { custom: string; title: string })[];
	content: Record<string, string>;
}

export interface RecSummary {
	slug: string;
	name: string;
	locale: string;
	branding: { primary_color?: string; logo?: string };
}

export interface RecMatch extends RecSummary {
	matched_rule?: string;
	matched_value?: string;
}

export interface RecApi {
	getConfig: () => Promise<SiteConfig>;
	createSubmission: (data: Record<string, unknown>) => Promise<SubmissionResponse>;
	getSubmission: (id: string) => Promise<SubmissionResponse>;
	updateSubmission: (id: string, data: Record<string, unknown>) => Promise<SubmissionResponse>;
	uploadDocument: (submissionId: string, file: File, docType: string) => Promise<unknown>;
	checkEligibility: (data: { lat?: number; lng?: number; address?: string }) => Promise<{
		eligible: boolean;
		lat?: number;
		lng?: number;
		municipality?: string;
		postal_code?: string;
		state?: string;
		reason?: string;
	}>;
	extractBill: (files: File[]) => Promise<Record<string, string | null>>;
	extractIdCard: (files: File[]) => Promise<Record<string, string | null>>;
}

export function createRecApi(recSlug: string): RecApi {
	const base = `/api/${recSlug}`;

	async function fetchWithSession(path: string, init: RequestInit): Promise<Response> {
		const headers: Record<string, string> = {};
		if (sessionToken) headers['X-Session-Token'] = sessionToken;
		Object.assign(headers, init.headers || {});
		const res = await fetch(path, { ...init, headers });
		if (res.status === 410) {
			window.location.reload();
			throw new Error('Session expired');
		}
		if (!res.ok) {
			const body = await res.text();
			throw new Error(`API error ${res.status}: ${body}`);
		}
		return res;
	}

	return {
		getConfig: () => request<SiteConfig>(`${base}/config`),

		createSubmission: (data) =>
			request<SubmissionResponse>(`${base}/submissions`, {
				method: 'POST',
				body: JSON.stringify(data)
			}),

		getSubmission: (id) => request<SubmissionResponse>(`${base}/submissions/${id}`),

		updateSubmission: (id, data) =>
			request<SubmissionResponse>(`${base}/submissions/${id}`, {
				method: 'PATCH',
				body: JSON.stringify(data)
			}),

		uploadDocument: async (submissionId, file, docType) => {
			const form = new FormData();
			form.append('file', file);
			form.append('doc_type', docType);
			const res = await fetchWithSession(`${base}/submissions/${submissionId}/documents`, {
				method: 'POST',
				body: form
			});
			return res.json();
		},

		checkEligibility: (data) =>
			request(`${base}/eligibility`, { method: 'POST', body: JSON.stringify(data) }),

		extractBill: async (files) => {
			const form = new FormData();
			for (const file of files) form.append('files', file);
			const res = await fetchWithSession(`${base}/extract`, { method: 'POST', body: form });
			return res.json();
		},

		extractIdCard: async (files) => {
			const form = new FormData();
			for (const file of files) form.append('files', file);
			const res = await fetchWithSession(`${base}/extract-id`, { method: 'POST', body: form });
			return res.json();
		}
	};
}

export const globalApi = {
	health: () => request<{ status: string }>('/api/health'),
	listRecs: () => request<RecSummary[]>('/api/recs'),
	findRecsByAddress: (address: string) =>
		request<RecMatch[]>('/api/recs/find-by-address', {
			method: 'POST',
			body: JSON.stringify({ address })
		})
};
