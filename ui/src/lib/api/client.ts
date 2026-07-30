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

export interface AdminRecAccess {
	slug: string;
	name: string;
	organization: string | null;
	capabilities: string[];
}

export interface AdminMe {
	sub: string;
	email: string | null;
	name: string | null;
	preferred_username: string | null;
	locale: string | null;
	subject_type: string;
	organizations: string[];
	realm_groups: string[];
	recs: AdminRecAccess[];
}

export interface EnablementStep {
	step: string;
	label: string;
	fail_closed: boolean;
	status: 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped';
	external_ref: string | null;
	attempts: number;
	last_error: string | null;
	detail: string | null;
	started_at: string | null;
	completed_at: string | null;
}

export interface Enablement {
	submission_id: string;
	state: 'not_started' | 'partial' | 'complete' | 'failed';
	steps: EnablementStep[];
}

export interface AuditEntry {
	id: string;
	action: string;
	entity_type: string;
	entity_id: string | null;
	rec_slug: string | null;
	ip_address: string | null;
	detail: string | null;
	actor_type: string;
	actor_sub: string | null;
	actor_email: string | null;
	actor_client_id: string | null;
	created_at: string;
}

export interface RecStats {
	rec_slug: string;
	by_status: Record<string, number>;
	submissions_with_failed_steps: number;
}

export interface AdminDocument {
	id: string;
	doc_type: string;
	original_filename: string;
	mime_type: string;
	size_bytes: number;
	created_at: string;
}

export interface SubmissionPage {
	submissions: AdminSubmission[];
	total: number;
}

export interface QueueFilters {
	status?: string;
	ref?: string;
	skip?: number;
	limit?: number;
}

/** Signals that the caller is authenticated but administers nothing. */
export class AdminDeniedError extends Error {
	constructor(detail: string) {
		super(detail);
		this.name = 'AdminDeniedError';
	}
}

function signIn(): never {
	window.location.href = '/oauth2/sign_in?rd=' + encodeURIComponent(window.location.href);
	// The navigation is already underway; nothing downstream should run.
	throw new Error('Redirecting to sign in');
}

async function detailOf(res: Response): Promise<string> {
	try {
		const body = await res.json();
		return typeof body?.detail === 'string' ? body.detail : JSON.stringify(body);
	} catch {
		return res.statusText;
	}
}

/**
 * Admin requests carry no token of their own.
 *
 * The browser is authenticated by the oauth2-proxy session cookie; the ingress
 * turns it into a verified JWT header. A 401 therefore means the session lapsed,
 * and the only useful response is to go and get a new one — the same thing
 * `apps/grid` does. A 403 means signed in but not permitted, which is a
 * different screen, so it must not trigger a login loop.
 */
async function adminFetch(path: string, options?: RequestInit): Promise<Response> {
	const res = await fetch(path, {
		credentials: 'include',
		...options,
		headers: {
			'Content-Type': 'application/json',
			...(options?.headers as Record<string, string> | undefined)
		}
	});

	if (res.status === 401) signIn();
	if (res.status === 403) throw new AdminDeniedError(await detailOf(res));
	if (!res.ok) throw new Error(await detailOf(res));
	return res;
}

async function adminRequest<T>(path: string, options?: RequestInit): Promise<T> {
	const res = await adminFetch(path, options);
	if (res.status === 204) return undefined as T;
	return res.json() as Promise<T>;
}

export const getAdminMe = () => adminRequest<AdminMe>('/api/admin/me');
export const getAdminRecs = () => adminRequest<AdminRecAccess[]>('/api/admin/recs');
export const adminPing = () => adminRequest<{ ok: boolean }>('/api/admin/ping');

export function createRecAdminApi(recSlug: string) {
	const base = `/api/admin/${recSlug}`;

	return {
		stats: () => adminRequest<RecStats>(`${base}/stats`),

		async listSubmissions(filters: QueueFilters = {}): Promise<SubmissionPage> {
			const params = new URLSearchParams();
			if (filters.status) params.set('status', filters.status);
			if (filters.ref) params.set('ref', filters.ref);
			params.set('skip', String(filters.skip ?? 0));
			params.set('limit', String(filters.limit ?? 25));
			const res = await adminFetch(`${base}/submissions?${params}`);
			return {
				submissions: (await res.json()) as AdminSubmission[],
				// The header is what lets the console paginate at all — without it
				// there is no telling a full last page from a page that is merely full.
				total: Number(res.headers.get('X-Total-Count') ?? 0)
			};
		},

		getSubmission: (id: string, reveal = false) =>
			adminRequest<AdminSubmission>(`${base}/submissions/${id}?reveal=${reveal}`),

		transition: (id: string, target: string, reason?: string) =>
			adminRequest<AdminSubmission>(`${base}/submissions/${id}/transition`, {
				method: 'POST',
				body: JSON.stringify({ target, reason })
			}),

		updateSubmission: (id: string, patch: Record<string, unknown>) =>
			adminRequest<AdminSubmission>(`${base}/submissions/${id}`, {
				method: 'PATCH',
				body: JSON.stringify(patch)
			}),

		purge: (id: string) =>
			adminRequest<void>(`${base}/submissions/${id}`, { method: 'DELETE' }),

		enablement: (id: string) => adminRequest<Enablement>(`${base}/submissions/${id}/enablement`),

		retryEnablement: (id: string, step?: string) =>
			adminRequest<Enablement>(`${base}/submissions/${id}/enablement/retry`, {
				method: 'POST',
				body: JSON.stringify({ step: step ?? null })
			}),

		revokeEnablement: (id: string) =>
			adminRequest<Enablement>(`${base}/submissions/${id}/enablement/revoke`, {
				method: 'POST'
			}),

		documents: (id: string) =>
			adminRequest<AdminDocument[]>(`${base}/submissions/${id}/documents`),

		documentUrl: (id: string, documentId: string) =>
			`${base}/submissions/${id}/documents/${documentId}`,

		pdfUrl: (id: string) => `${base}/submissions/${id}/pdf`,

		auditLogs: (limit = 100) => adminRequest<AuditEntry[]>(`${base}/audit-logs?limit=${limit}`),

		submissionAudit: (id: string) =>
			adminRequest<AuditEntry[]>(`${base}/audit-logs?limit=200`).then((rows) =>
				rows.filter((r) => r.entity_id === id)
			),

		async exportCsv(recipientRef?: string): Promise<Blob> {
			const res = await adminFetch(`${base}/exports/csv`, {
				method: 'POST',
				body: JSON.stringify({ recipient_ref: recipientRef || null })
			});
			return res.blob();
		},

		async exportPodList(offerId: string, recipientRef: string): Promise<Blob> {
			const res = await adminFetch(`${base}/exports/pod-list`, {
				method: 'POST',
				body: JSON.stringify({ offer_id: offerId, recipient_ref: recipientRef })
			});
			return res.blob();
		}
	};
}

export type RecAdminApi = ReturnType<typeof createRecAdminApi>;

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
