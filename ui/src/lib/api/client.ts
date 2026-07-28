export interface SubmissionResponse {
	id: string;
	ref?: string;
	status: string;
	session_token?: string;
	[key: string]: unknown;
}

export interface AdminSubmission extends SubmissionResponse {
	ref: string;
	rec_slug: string;
	first_name?: string | null;
	last_name?: string | null;
	email?: string | null;
	phone?: string | null;
	fiscal_code?: string | null;
	pod_code?: string | null;
	supply_municipality?: string | null;
	extra_data?: Record<string, unknown> | null;
	notes?: string | null;
	created_at: string;
	updated_at: string;
	consent_ip?: string | null;
	dataspace_subject_id?: string | null;
	dataspace_did?: string | null;
	dataspace_vc_id?: string | null;
	dataspace_vc_issued_at?: string | null;
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
	consent: Record<
		string,
		{ version?: string; file?: string; url?: string; required: boolean; offers?: string[] }
	>;
	steps: (string | { custom: string; title: string })[];
	content: Record<string, string>;
}

export interface SharingOffer {
	id: string;
	purpose: string;
	purpose_broader: string[];
	legal_basis: string;
	requires_consent: boolean;
	recipients: {
		controller: string;
		controller_role: string | null;
		processors: { category: string };
	};
	subject_scope: string;
	measures: string[];
	resolution: string | null;
	coverage: { retrospective: string | null; prospective: string | null };
	consent_text_version: string;
	revocable: boolean;
	retention: string | null;
	user_visible_hash: string;
	dataset_count: number;
	fallback_text_en: {
		purpose_label: string;
		purpose_definition: string;
		processor_category: string;
	};
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
	getSharingOffers: () => Promise<SharingOffer[]>;
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
	verifyPhone: (submissionId: string, phone?: string) => Promise<PhoneVerifyStatus>;
	confirmPhone: (
		submissionId: string,
		code: string,
		phone?: string
	) => Promise<PhoneVerifyStatus>;
}

export interface PhoneVerifyStatus {
	phone_verified: boolean;
	sent: boolean;
	phone_verified_at?: string | null;
}

/** Thrown by phone verification with the human-readable detail from the API. */
export class PhoneVerifyError extends Error {
	status: number;
	constructor(status: number, detail: string) {
		super(detail);
		this.name = 'PhoneVerifyError';
		this.status = status;
	}
}

export interface RecAdminApi {
	listSubmissions: () => Promise<AdminSubmission[]>;
	updateSubmissionStatus: (id: string, status: string) => Promise<AdminSubmission>;
}

function adminRequest<T>(path: string, token: string, options?: RequestInit): Promise<T> {
	return request<T>(path, {
		...options,
		headers: {
			Authorization: `Bearer ${token}`,
			...(options?.headers as Record<string, string> | undefined)
		}
	});
}

export function createRecAdminApi(recSlug: string, token: string): RecAdminApi {
	const base = `/api/${recSlug}/admin`;

	return {
		listSubmissions: () => adminRequest<AdminSubmission[]>(`${base}/submissions`, token),
		updateSubmissionStatus: (id, status) =>
			adminRequest<AdminSubmission>(`${base}/submissions/${id}`, token, {
				method: 'PATCH',
				body: JSON.stringify({ status })
			})
	};
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

		getSharingOffers: () => request<SharingOffer[]>(`${base}/sharing-offers`),

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
		},

		verifyPhone: (submissionId, phone) =>
			phoneRequest(`${base}/submissions/${submissionId}/verify-phone`, { phone: phone ?? null }),

		confirmPhone: (submissionId, code, phone) =>
			phoneRequest(`${base}/submissions/${submissionId}/confirm-phone`, {
				code,
				phone: phone ?? null
			})
	};
}

/** Phone endpoints return a plain {detail} on error; surface it verbatim. */
async function phoneRequest(path: string, body: Record<string, unknown>): Promise<PhoneVerifyStatus> {
	const headers: Record<string, string> = { 'Content-Type': 'application/json' };
	if (sessionToken) headers['X-Session-Token'] = sessionToken;
	const res = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body) });
	if (res.status === 410) {
		window.location.reload();
		throw new Error('Session expired');
	}
	if (!res.ok) {
		let detail = `Error ${res.status}`;
		try {
			const parsed = await res.json();
			if (typeof parsed?.detail === 'string') detail = parsed.detail;
		} catch {
			/* keep default */
		}
		throw new PhoneVerifyError(res.status, detail);
	}
	return res.json();
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
