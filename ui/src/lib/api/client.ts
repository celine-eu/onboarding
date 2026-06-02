export interface SubmissionResponse {
	id: string;
	status: string;
	[key: string]: unknown;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
	const res = await fetch(path, {
		headers: { 'Content-Type': 'application/json', ...options?.headers },
		...options
	});

	if (!res.ok) {
		const body = await res.text();
		throw new Error(`API error ${res.status}: ${body}`);
	}

	return res.json();
}

export interface SiteConfig {
	slug: string;
	name: string;
	locale: string;
	branding: { primary_color?: string; logo?: string };
	fields: { extra: Array<{ key: string; label: string; type: string; step: string; required: boolean }>; hidden: string[] };
	consent: Record<string, { version: string; file?: string; url?: string; required: boolean }>;
	steps: (string | { custom: string; title: string })[];
	content: Record<string, string>;
}

export const api = {
	health: () => request<{ status: string }>('/api/health'),

	getConfig: () => request<SiteConfig>('/api/config'),

	createSubmission: (data: Record<string, unknown>) =>
		request<SubmissionResponse>('/api/submissions', {
			method: 'POST',
			body: JSON.stringify(data)
		}),

	getSubmission: (id: string) => request<SubmissionResponse>(`/api/submissions/${id}`),

	updateSubmission: (id: string, data: Record<string, unknown>) =>
		request<SubmissionResponse>(`/api/submissions/${id}`, {
			method: 'PATCH',
			body: JSON.stringify(data)
		}),

	uploadDocument: async (submissionId: string, file: File, docType: string) => {
		const form = new FormData();
		form.append('file', file);
		form.append('doc_type', docType);

		const res = await fetch(`/api/submissions/${submissionId}/documents`, {
			method: 'POST',
			body: form
		});

		if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
		return res.json();
	},

	checkEligibility: (data: { lat?: number; lng?: number; address?: string }) =>
		request<{ eligible: boolean; lat?: number; lng?: number; municipality?: string; reason?: string }>(
			'/api/eligibility',
			{ method: 'POST', body: JSON.stringify(data) }
		),

	extractBill: async (files: File[]): Promise<Record<string, string | null>> => {
		const form = new FormData();
		for (const file of files) {
			form.append('files', file);
		}

		const res = await fetch('/api/extract', {
			method: 'POST',
			body: form
		});

		if (!res.ok) {
			const body = await res.text();
			throw new Error(`Extraction failed: ${body}`);
		}
		return res.json();
	}
};
