<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import {
		AdminDeniedError,
		type AdminDocument,
		type AdminSubmission,
		type AdminVerification,
		type AuditEntry,
		type Enablement
	} from '$lib/api/client';
	import { intlLocale, locale, t } from '$lib/i18n';
	import type { PageData } from './$types';

	const { data }: { data: PageData } = $props();
	const api = $derived(data.api);
	const id = $derived(page.params.id!);
	const can = $derived(data.can);

	// Codes come from the API; an unknown one is shown raw rather than hidden.
	function label(group: string, code: string): string {
		return $t(`admin.${group}.${code}`, { default: code });
	}

	// Mirrors workflows/engine.TRANSITIONS. The server is the authority; this only
	// decides which buttons to offer, so an operator is not shown an action that
	// can only 422.
	const NEXT: Record<string, Array<{ target: string; label: string; tone: string }>> = {
		submitted: [
			{ target: 'under_review', label: 'admin.detail.take', tone: 'secondary' },
			{ target: 'rejected', label: 'admin.detail.reject', tone: 'danger' }
		],
		under_review: [
			{ target: 'approved', label: 'admin.detail.approve', tone: 'primary' },
			{ target: 'rejected', label: 'admin.detail.reject', tone: 'danger' }
		],
		rejected: [{ target: 'submitted', label: 'admin.detail.reopen', tone: 'secondary' }]
	};

	let submission = $state<AdminSubmission | null>(null);
	let enablement = $state<Enablement | null>(null);
	let documents = $state<AdminDocument[]>([]);
	let auditEntries = $state<AuditEntry[]>([]);
	let revealed = $state(false);
	let notes = $state('');
	let busy = $state<string | null>(null);
	let errorMsg = $state('');
	let successMsg = $state('');
	let rejectReason = $state('');
	let showReject = $state(false);
	let verifications = $state<AdminVerification[]>([]);
	let verifyMethod = $state<AdminVerification['method']>('offline');
	let verifyDocument = $state('');
	let verifyNote = $state('');

	// A verification is evidence for a pending decision; the API refuses one after.
	const verifiable = $derived(
		submission?.status === 'submitted' || submission?.status === 'under_review'
	);

	async function refresh() {
		submission = await api.getSubmission(id, revealed);
		notes = (submission.notes as string) ?? '';
		try {
			enablement = await api.enablement(id);
		} catch {
			enablement = null;
		}
		try {
			verifications = await api.verifications(id);
		} catch {
			verifications = [];
		}
	}

	onMount(async () => {
		try {
			await refresh();
			documents = await api.documents(id);
		} catch (e) {
			errorMsg = describe(e);
		}
		if (can('audit.read')) {
			try {
				auditEntries = await api.submissionAudit(id);
			} catch {
				// Not fatal — the record is still readable from the audit page.
			}
		}
	});

	function describe(e: unknown): string {
		if (e instanceof AdminDeniedError) return e.message;
		return e instanceof Error ? e.message : String(e);
	}

	async function act(name: string, fn: () => Promise<void>) {
		busy = name;
		errorMsg = '';
		successMsg = '';
		try {
			await fn();
		} catch (e) {
			errorMsg = describe(e);
		} finally {
			busy = null;
		}
	}

	const transition = (target: string, reason?: string) =>
		act(target, async () => {
			await api.transition(id, target, reason);
			await refresh();
			successMsg = $t('admin.detail.transitioned', { status: label('status', target) });
			showReject = false;
			rejectReason = '';
		});

	const toggleReveal = () =>
		act('reveal', async () => {
			revealed = !revealed;
			await refresh();
		});

	const retry = (step?: string) =>
		act('retry', async () => {
			enablement = await api.retryEnablement(id, step);
			// Approval can be blocked by a step; re-reading the submission keeps the
			// status shown here in step with what the retry changed.
			submission = await api.getSubmission(id, revealed);
		});

	const revoke = () =>
		act('revoke', async () => {
			if (!confirm($t('admin.detail.revoke_confirm')))
				return;
			enablement = await api.revokeEnablement(id);
		});

	const recordVerification = () =>
		act('verify', async () => {
			await api.recordVerification(id, {
				method: verifyMethod,
				...(verifyMethod === 'uploaded-document' ? { document_id: verifyDocument } : {}),
				...(verifyNote.trim() ? { note: verifyNote.trim() } : {})
			});
			await refresh();
			verifyNote = '';
			successMsg = $t('admin.detail.verification_recorded');
		});

	function documentName(documentId: string | null): string {
		return documents.find((d) => d.id === documentId)?.original_filename ?? '—';
	}

	const saveNotes = () =>
		act('notes', async () => {
			submission = await api.updateSubmission(id, { notes });
			successMsg = $t('admin.detail.notes_saved');
		});

	function formatDate(value?: string | null): string {
		if (!value) return '—';
		const date = new Date(value);
		return Number.isNaN(date.getTime())
			? '—'
			: new Intl.DateTimeFormat(intlLocale($locale), {
					dateStyle: 'medium',
					timeStyle: 'short'
				}).format(date);
	}

	function consentRow(label: string, given: unknown, at: unknown, version: unknown) {
		return { label, given: Boolean(given), at: at as string | null, version: version as string | null };
	}

	const consents = $derived(
		submission
			? [
					consentRow($t('admin.detail.consent_gdpr'), submission.gdpr_consent, submission.gdpr_consent_at, submission.gdpr_consent_version),
					consentRow($t('admin.detail.consent_policy'), submission.policy_consent, submission.policy_consent_at, submission.policy_consent_version),
					consentRow($t('admin.detail.consent_statute'), submission.statute_consent, submission.statute_consent_at, submission.statute_consent_version),
					consentRow(
						$t('admin.detail.consent_data_sharing'),
						submission.data_sharing_consent,
						submission.data_sharing_consent_at,
						submission.data_sharing_consent_text_version
					)
				]
			: []
	);
</script>

<svelte:head><title>{submission?.ref ?? $t('admin.detail.page_title')}</title></svelte:head>

<a class="back" href="/admin/{data.rec}">{$t('admin.detail.back')}</a>

{#if errorMsg}<p class="message error">{errorMsg}</p>{/if}
{#if successMsg}<p class="message success">{successMsg}</p>{/if}

{#if submission}
	<header class="head">
		<div>
			<h1>{submission.ref}</h1>
			<span class="status" data-status={submission.status}>
				{label('status', submission.status)}
			</span>
		</div>

		<div class="actions">
			{#if can('submissions.review')}
				{#each NEXT[submission.status] ?? [] as action}
					{#if action.target === 'rejected'}
						<button class="danger" onclick={() => (showReject = true)} disabled={busy !== null}>
							{$t(action.label)}
						</button>
					{:else}
						<!-- Approval waits for the REC's recorded verification; the API refuses
						     without one, so the button says why instead of offering a 422. -->
						<button
							class={action.tone}
							disabled={busy !== null ||
								(action.target === 'approved' && !submission.verification)}
							title={action.target === 'approved' && !submission.verification
								? $t('admin.detail.verification_needed_to_approve')
								: undefined}
							onclick={() => transition(action.target)}
						>
							{busy === action.target ? '…' : $t(action.label)}
						</button>
					{/if}
				{/each}
			{/if}
			<a class="secondary button" href={api.pdfUrl(id)}>PDF</a>
		</div>
	</header>

	{#if submission.status === 'under_review' && !submission.verification && can('submissions.review')}
		<p class="message notice">{$t('admin.detail.verification_needed_to_approve')}</p>
	{/if}

	{#if showReject}
		<form
			class="reject"
			onsubmit={(e) => {
				e.preventDefault();
				void transition('rejected', rejectReason);
			}}
		>
			<label>
				<span>{$t('admin.detail.reject_reason')}</span>
				<!-- Required by the API too. The participant is told, and whoever
				     reopens the case months later needs to know why. -->
				<input
					bind:value={rejectReason}
					required
					placeholder={$t('admin.detail.reject_placeholder')}
				/>
			</label>
			<button type="submit" class="danger" disabled={busy !== null || !rejectReason.trim()}>
				{$t('admin.detail.reject_confirm')}
			</button>
			<button type="button" class="secondary" onclick={() => (showReject = false)}>
				{$t('admin.cancel')}
			</button>
		</form>
	{/if}

	<div class="columns">
		<div class="col">
			<section class="panel">
				<h2>{$t('admin.detail.applicant')}</h2>
				<dl>
					<dt>{$t('admin.detail.name')}</dt>
					<dd>{[submission.first_name, submission.last_name].filter(Boolean).join(' ') || '—'}</dd>
					<dt>{$t('admin.detail.email')}</dt>
					<dd>{submission.email ?? '—'}</dd>
					<dt>{$t('admin.detail.phone')}</dt>
					<dd>
						{submission.phone ?? '—'}
						{#if submission.phone_verified}<span class="ok">{$t('admin.detail.verified')}</span>{/if}
						{#if submission.phone_verification_waived}<span class="waived">{$t('admin.detail.phone_verification_waived')}</span>{/if}
					</dd>
					<dt>{$t('admin.detail.fiscal_code')}</dt>
					<dd class="mono">{submission.fiscal_code ?? '—'}</dd>
					<dt>{$t('admin.detail.pod')}</dt>
					<dd class="mono">{submission.pod_code ?? '—'}</dd>
					<dt>{$t('admin.detail.supply_municipality')}</dt>
					<dd>{submission.supply_municipality ?? '—'}</dd>
				</dl>
				{#if can('submissions.reveal')}
					<button class="secondary small" onclick={toggleReveal} disabled={busy !== null}>
						{revealed ? $t('admin.detail.hide_identifiers') : $t('admin.detail.reveal_identifiers')}
					</button>
					<p class="hint">{$t('admin.detail.reveal_audited')}</p>
				{:else}
					<p class="hint">{$t('admin.detail.reveal_forbidden')}</p>
				{/if}
			</section>

			<section class="panel">
				<h2>{$t('admin.detail.consents')}</h2>
				<table class="mini">
					<tbody>
						{#each consents as consent}
							<tr>
								<td>{consent.label}</td>
								<td>{consent.given ? '✓' : '—'}</td>
								<td class="muted">{formatDate(consent.at)}</td>
								<td class="muted">{consent.version ?? ''}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</section>

			<section class="panel">
				<h2>{$t('admin.detail.documents')}</h2>
				{#if documents.length === 0}
					<p class="muted">{$t('admin.detail.no_documents')}</p>
				{:else}
					<ul class="docs">
						{#each documents as doc}
							<li>
								<a href={api.documentUrl(id, doc.id)}>{doc.original_filename}</a>
								<span class="muted">{doc.doc_type} · {Math.round(doc.size_bytes / 1024)} kB</span>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section class="panel verification">
				<h2>{$t('admin.detail.verification')}</h2>
				<p class="hint">{$t('admin.detail.verification_hint')}</p>

				{#if verifications.length === 0}
					<p class="muted">{$t('admin.detail.verification_none')}</p>
				{:else}
					<ol class="verifications">
						{#each [...verifications].reverse() as v, i (v.id)}
							<li data-current={i === 0}>
								<strong>{$t(`admin.detail.verification_${v.method.replace('-', '_')}`)}</strong>
								{#if i > 0}<span class="muted small">{$t('admin.detail.verification_superseded')}</span>{/if}
								<span class="muted small">
									{v.actor_email ?? v.actor_sub ?? v.actor_type} · {formatDate(v.created_at)}
								</span>
								{#if v.document_id}
									<span class="small">{$t('admin.detail.verification_document')}: {documentName(v.document_id)}</span>
								{/if}
								{#if v.note}<span class="small">{v.note}</span>{/if}
							</li>
						{/each}
					</ol>
				{/if}

				{#if can('submissions.review') && verifiable}
					<form
						class="verify"
						onsubmit={(e) => {
							e.preventDefault();
							void recordVerification();
						}}
					>
						<fieldset>
							<legend>{$t('admin.detail.verification_method')}</legend>
							<label>
								<input type="radio" bind:group={verifyMethod} value="offline" />
								{$t('admin.detail.verification_offline')}
							</label>
							<label>
								<input
									type="radio"
									bind:group={verifyMethod}
									value="uploaded-document"
									disabled={documents.length === 0}
								/>
								{$t('admin.detail.verification_uploaded_document')}
							</label>
						</fieldset>
						{#if verifyMethod === 'uploaded-document'}
							<label>
								<span>{$t('admin.detail.verification_document')}</span>
								<select bind:value={verifyDocument} required>
									<option value="">—</option>
									{#each documents as doc (doc.id)}
										<option value={doc.id}>{doc.original_filename}</option>
									{/each}
								</select>
							</label>
						{/if}
						<label>
							<span>{$t('admin.detail.verification_note')}</span>
							<textarea bind:value={verifyNote} rows="2" maxlength="1000"></textarea>
						</label>
						<button
							type="submit"
							class="secondary small"
							disabled={busy !== null ||
								(verifyMethod === 'uploaded-document' && !verifyDocument)}
						>
							{busy === 'verify'
								? '…'
								: submission.verification
									? $t('admin.detail.verification_record_new')
									: $t('admin.detail.verification_record')}
						</button>
					</form>
				{/if}
			</section>

			{#if can('submissions.write')}
				<section class="panel">
					<h2>{$t('admin.detail.notes')}</h2>
					<textarea bind:value={notes} rows="4"></textarea>
					<button class="secondary small" onclick={saveNotes} disabled={busy !== null}>
						{busy === 'notes' ? $t('admin.detail.notes_saving') : $t('admin.detail.notes_save')}
					</button>
				</section>
			{/if}
		</div>

		<div class="col">
			<section class="panel">
				<h2>
					{$t('admin.detail.enablement')}
					{#if enablement}
						<span class="state" data-state={enablement.state}>
							{label('enablement_state', enablement.state)}
						</span>
					{/if}
				</h2>
				<p class="hint">{$t('admin.detail.enablement_hint')}</p>

				{#if enablement}
					<ul class="steps">
						{#each enablement.steps as step}
							<li data-status={step.status}>
								<div class="step-head">
									<strong>{$t(`admin.step.${step.step}`, { default: step.label })}</strong>
									<span class="badge" data-status={step.status}>
										{label('step_status', step.status)}
									</span>
								</div>
								<div class="step-meta">
									{#if !step.fail_closed}<span class="soft">{$t('admin.detail.non_blocking')}</span>{/if}
									{#if step.attempts > 0}<span>{$t('admin.detail.attempts', { count: step.attempts })}</span>{/if}
									{#if step.external_ref}<span class="mono">{step.external_ref}</span>{/if}
								</div>
								{#if step.last_error}
									<p class="step-error">{step.last_error}</p>
								{:else if step.invitation}
									<!-- The code, translated; `detail` is the same outcome in English for the CLI. -->
									<p class="muted small" data-invitation={step.invitation}>
										{label('invitation', step.invitation)}
									</p>
								{:else if step.detail}
									<p class="muted small">{step.detail}</p>
								{/if}
								<!-- A failed send is the one succeeded step a named retry re-runs, so the
								     operator can send it again; a cooldown or a missing address is not. -->
								{#if (step.status === 'failed' || (step.step === 'keycloak_user' && step.status === 'succeeded' && step.invitation === 'send_failed')) && can('enablement.retry')}
									<button
										class="secondary small"
										disabled={busy !== null}
										onclick={() => retry(step.step)}
									>
										{$t('admin.detail.retry_step')}
									</button>
								{/if}
							</li>
						{/each}
					</ul>

					<div class="step-actions">
						{#if can('enablement.retry') && enablement.state === 'failed'}
							<button class="primary small" disabled={busy !== null} onclick={() => retry()}>
								{busy === 'retry' ? $t('admin.detail.in_progress') : $t('admin.detail.retry_all')}
							</button>
						{/if}
						{#if can('enablement.revoke') && enablement.state !== 'not_started'}
							<button class="danger small" disabled={busy !== null} onclick={revoke}>
								{$t('admin.detail.revoke')}
							</button>
						{/if}
					</div>
				{:else}
					<p class="muted">{$t('admin.detail.enablement_unavailable')}</p>
				{/if}
			</section>

			{#if auditEntries.length > 0}
				<section class="panel">
					<h2>{$t('admin.detail.history')}</h2>
					<ul class="audit">
						{#each auditEntries as entry}
							<li>
								<span class="mono small">{formatDate(entry.created_at)}</span>
								<strong>{entry.action}</strong>
								<span class="muted">
									{entry.actor_email ?? entry.actor_sub ?? entry.actor_type}
								</span>
								{#if entry.detail}<p class="muted small">{entry.detail}</p>{/if}
							</li>
						{/each}
					</ul>
				</section>
			{/if}
		</div>
	</div>
{:else if !errorMsg}
	<p class="muted">{$t('admin.loading')}</p>
{/if}

<style>
	.back {
		display: inline-block;
		margin-bottom: 1rem;
		font-size: 0.875rem;
		color: var(--celine-text-secondary);
	}

	.head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 1rem;
		flex-wrap: wrap;
		margin-bottom: 1.25rem;
	}

	h1 {
		font-size: 1.5rem;
		margin: 0 0 0.25rem;
	}

	h2 {
		font-size: 0.9375rem;
		margin: 0 0 0.75rem;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.columns {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
		gap: 1rem;
		align-items: start;
	}

	.col {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.panel {
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		padding: 1rem;
	}

	dl {
		display: grid;
		grid-template-columns: 10rem 1fr;
		gap: 0.375rem 0.75rem;
		margin: 0 0 0.75rem;
		font-size: 0.875rem;
	}

	dt {
		color: var(--celine-text-secondary);
	}

	dd {
		margin: 0;
	}

	.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}

	.small {
		font-size: 0.8125rem;
	}

	.muted {
		color: var(--celine-text-secondary);
	}

	.hint {
		font-size: 0.75rem;
		color: var(--celine-text-secondary);
		margin-top: 0.5rem;
		line-height: 1.5;
	}

	.ok {
		color: #166534;
		font-size: 0.75rem;
		margin-left: 0.375rem;
	}

	.waived {
		display: block;
		color: var(--celine-text-secondary);
		font-size: 0.75rem;
		line-height: 1.4;
	}

	.actions,
	.step-actions {
		display: flex;
		gap: 0.5rem;
		flex-wrap: wrap;
	}

	button,
	.button {
		min-height: 2.25rem;
		padding: 0 0.875rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		font-size: 0.875rem;
		font-weight: 600;
		cursor: pointer;
		text-decoration: none;
		display: inline-flex;
		align-items: center;
	}

	button.small {
		min-height: 1.875rem;
		font-size: 0.8125rem;
		margin-top: 0.5rem;
	}

	button.primary {
		background: var(--celine-primary);
		border-color: var(--celine-primary);
		color: #fff;
	}

	button.danger {
		border-color: #dc2626;
		color: #dc2626;
	}

	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.status,
	.badge,
	.state {
		display: inline-block;
		padding: 0.125rem 0.5rem;
		border-radius: 999px;
		font-size: 0.75rem;
		font-weight: 600;
		background: var(--celine-bg);
	}

	.status[data-status='approved'],
	.badge[data-status='succeeded'],
	.state[data-state='complete'] {
		background: #dcfce7;
		color: #166534;
	}

	.status[data-status='rejected'],
	.badge[data-status='failed'],
	.state[data-state='failed'] {
		background: #fee2e2;
		color: #991b1b;
	}

	.status[data-status='under_review'],
	.state[data-state='partial'] {
		background: #fef3c7;
		color: #92400e;
	}

	.badge[data-status='skipped'] {
		color: var(--celine-text-secondary);
	}

	.reject {
		display: flex;
		gap: 0.75rem;
		align-items: flex-end;
		flex-wrap: wrap;
		padding: 1rem;
		margin-bottom: 1rem;
		border: 1px solid #fecaca;
		border-radius: var(--celine-radius-sm);
		background: #fef2f2;
	}

	.reject label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		flex: 1;
		min-width: 16rem;
	}

	.reject span {
		font-size: 0.75rem;
		font-weight: 600;
	}

	input,
	textarea {
		width: 100%;
		min-height: 2.25rem;
		padding: 0.375rem 0.5rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		font-size: 0.875rem;
		font-family: inherit;
	}

	table.mini {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.8125rem;
	}

	table.mini td {
		padding: 0.25rem 0.375rem 0.25rem 0;
	}

	.docs,
	.steps,
	.audit {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.625rem;
	}

	.verifications {
		list-style: none;
		margin: 0 0 0.75rem;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.verifications li {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
		padding: 0.5rem 0.625rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		font-size: 0.875rem;
	}

	.verifications li[data-current='false'] {
		opacity: 0.7;
	}

	.verify {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.verify fieldset {
		border: 0;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		font-size: 0.875rem;
	}

	.verify legend,
	.verify label > span {
		font-size: 0.75rem;
		font-weight: 600;
	}

	.verify fieldset input {
		width: auto;
		min-height: 0;
		margin-right: 0.375rem;
	}

	.verify select {
		width: 100%;
		min-height: 2.25rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		font: inherit;
		font-size: 0.875rem;
	}

	.docs li {
		display: flex;
		justify-content: space-between;
		gap: 0.5rem;
		font-size: 0.875rem;
	}

	.steps li {
		padding: 0.625rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
	}

	.steps li[data-status='failed'] {
		border-color: #fecaca;
		background: #fef2f2;
	}

	.step-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.875rem;
	}

	.step-meta {
		display: flex;
		gap: 0.625rem;
		flex-wrap: wrap;
		font-size: 0.75rem;
		color: var(--celine-text-secondary);
		margin-top: 0.25rem;
	}

	.soft {
		font-style: italic;
	}

	.step-error {
		margin: 0.375rem 0 0;
		font-size: 0.8125rem;
		color: #991b1b;
		word-break: break-word;
	}

	.audit li {
		font-size: 0.8125rem;
		border-bottom: 1px solid var(--celine-border);
		padding-bottom: 0.5rem;
	}

	.audit p {
		margin: 0.25rem 0 0;
	}

	.message {
		padding: 0.75rem 1rem;
		border-radius: var(--celine-radius-sm);
		margin-bottom: 1rem;
	}

	.message.error {
		background: #fee2e2;
		color: #991b1b;
	}

	.message.success {
		background: #dcfce7;
		color: #166534;
	}

	.message.notice {
		background: #fef9c3;
		color: #854d0e;
	}
</style>
