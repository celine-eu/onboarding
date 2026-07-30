<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import {
		AdminDeniedError,
		type AdminDocument,
		type AdminSubmission,
		type AuditEntry,
		type Enablement
	} from '$lib/api/client';
	import type { PageData } from './$types';

	const { data }: { data: PageData } = $props();
	const api = $derived(data.api);
	const id = $derived(page.params.id!);
	const can = $derived(data.can);

	const STATUS_LABELS: Record<string, string> = {
		draft: 'Bozza',
		submitted: 'Inviata',
		under_review: 'In valutazione',
		approved: 'Approvata',
		rejected: 'Rifiutata'
	};

	// Mirrors workflows/engine.TRANSITIONS. The server is the authority; this only
	// decides which buttons to offer, so an operator is not shown an action that
	// can only 422.
	const NEXT: Record<string, Array<{ target: string; label: string; tone: string }>> = {
		submitted: [
			{ target: 'under_review', label: 'Prendi in carico', tone: 'secondary' },
			{ target: 'rejected', label: 'Rifiuta', tone: 'danger' }
		],
		under_review: [
			{ target: 'approved', label: 'Approva', tone: 'primary' },
			{ target: 'rejected', label: 'Rifiuta', tone: 'danger' }
		],
		rejected: [{ target: 'submitted', label: 'Riapri', tone: 'secondary' }]
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

	async function refresh() {
		submission = await api.getSubmission(id, revealed);
		notes = (submission.notes as string) ?? '';
		try {
			enablement = await api.enablement(id);
		} catch {
			enablement = null;
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
			successMsg = `Pratica ${STATUS_LABELS[target] ?? target}.`;
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
			if (!confirm('Revocare l’abilitazione? Credenziale e iscrizione verranno annullate.'))
				return;
			enablement = await api.revokeEnablement(id);
		});

	const saveNotes = () =>
		act('notes', async () => {
			submission = await api.updateSubmission(id, { notes });
			successMsg = 'Note salvate.';
		});

	function formatDate(value?: string | null): string {
		if (!value) return '—';
		const date = new Date(value);
		return Number.isNaN(date.getTime())
			? '—'
			: new Intl.DateTimeFormat('it-IT', {
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
					consentRow('GDPR', submission.gdpr_consent, submission.gdpr_consent_at, submission.gdpr_consent_version),
					consentRow('Privacy policy', submission.policy_consent, submission.policy_consent_at, submission.policy_consent_version),
					consentRow('Statuto', submission.statute_consent, submission.statute_consent_at, submission.statute_consent_version),
					consentRow(
						'Condivisione dati',
						submission.data_sharing_consent,
						submission.data_sharing_consent_at,
						submission.data_sharing_consent_text_version
					)
				]
			: []
	);
</script>

<svelte:head><title>{submission?.ref ?? 'Pratica'}</title></svelte:head>

<a class="back" href="/admin/{data.rec}">← Tutte le pratiche</a>

{#if errorMsg}<p class="message error">{errorMsg}</p>{/if}
{#if successMsg}<p class="message success">{successMsg}</p>{/if}

{#if submission}
	<header class="head">
		<div>
			<h1>{submission.ref}</h1>
			<span class="status" data-status={submission.status}>
				{STATUS_LABELS[submission.status] ?? submission.status}
			</span>
		</div>

		<div class="actions">
			{#if can('submissions.review')}
				{#each NEXT[submission.status] ?? [] as action}
					{#if action.target === 'rejected'}
						<button class="danger" onclick={() => (showReject = true)} disabled={busy !== null}>
							{action.label}
						</button>
					{:else}
						<button
							class={action.tone}
							disabled={busy !== null}
							onclick={() => transition(action.target)}
						>
							{busy === action.target ? '…' : action.label}
						</button>
					{/if}
				{/each}
			{/if}
			<a class="secondary button" href={api.pdfUrl(id)}>PDF</a>
		</div>
	</header>

	{#if showReject}
		<form
			class="reject"
			onsubmit={(e) => {
				e.preventDefault();
				void transition('rejected', rejectReason);
			}}
		>
			<label>
				<span>Motivo del rifiuto</span>
				<!-- Required by the API too. The participant is told, and whoever
				     reopens the case months later needs to know why. -->
				<input bind:value={rejectReason} required placeholder="es. POD di un'altra fornitura" />
			</label>
			<button type="submit" class="danger" disabled={busy !== null || !rejectReason.trim()}>
				Conferma rifiuto
			</button>
			<button type="button" class="secondary" onclick={() => (showReject = false)}>Annulla</button>
		</form>
	{/if}

	<div class="columns">
		<div class="col">
			<section class="panel">
				<h2>Richiedente</h2>
				<dl>
					<dt>Nome</dt>
					<dd>{[submission.first_name, submission.last_name].filter(Boolean).join(' ') || '—'}</dd>
					<dt>Email</dt>
					<dd>{submission.email ?? '—'}</dd>
					<dt>Telefono</dt>
					<dd>
						{submission.phone ?? '—'}
						{#if submission.phone_verified}<span class="ok">verificato</span>{/if}
					</dd>
					<dt>Codice fiscale</dt>
					<dd class="mono">{submission.fiscal_code ?? '—'}</dd>
					<dt>POD</dt>
					<dd class="mono">{submission.pod_code ?? '—'}</dd>
					<dt>Comune fornitura</dt>
					<dd>{submission.supply_municipality ?? '—'}</dd>
				</dl>
				{#if can('submissions.reveal')}
					<button class="secondary small" onclick={toggleReveal} disabled={busy !== null}>
						{revealed ? 'Nascondi identificativi' : 'Mostra codice fiscale e POD'}
					</button>
					<p class="hint">Ogni visualizzazione in chiaro viene registrata nel registro.</p>
				{:else}
					<p class="hint">Codice fiscale e POD sono mascherati: serve il permesso di rivelarli.</p>
				{/if}
			</section>

			<section class="panel">
				<h2>Consensi</h2>
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
				<h2>Documenti</h2>
				{#if documents.length === 0}
					<p class="muted">Nessun documento caricato.</p>
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

			{#if can('submissions.write')}
				<section class="panel">
					<h2>Note operatore</h2>
					<textarea bind:value={notes} rows="4"></textarea>
					<button class="secondary small" onclick={saveNotes} disabled={busy !== null}>
						{busy === 'notes' ? 'Salvataggio…' : 'Salva note'}
					</button>
				</section>
			{/if}
		</div>

		<div class="col">
			<section class="panel">
				<h2>
					Abilitazione
					{#if enablement}
						<span class="state" data-state={enablement.state}>{enablement.state}</span>
					{/if}
				</h2>
				<p class="hint">
					Cosa ha prodotto l'approvazione. I passi bloccanti impediscono l'approvazione
					finche' non riescono; gli altri si possono ritentare in seguito.
				</p>

				{#if enablement}
					<ul class="steps">
						{#each enablement.steps as step}
							<li data-status={step.status}>
								<div class="step-head">
									<strong>{step.label}</strong>
									<span class="badge" data-status={step.status}>{step.status}</span>
								</div>
								<div class="step-meta">
									{#if !step.fail_closed}<span class="soft">non bloccante</span>{/if}
									{#if step.attempts > 0}<span>{step.attempts} tentativi</span>{/if}
									{#if step.external_ref}<span class="mono">{step.external_ref}</span>{/if}
								</div>
								{#if step.last_error}
									<p class="step-error">{step.last_error}</p>
								{:else if step.detail}
									<p class="muted small">{step.detail}</p>
								{/if}
								{#if step.status === 'failed' && can('enablement.retry')}
									<button
										class="secondary small"
										disabled={busy !== null}
										onclick={() => retry(step.step)}
									>
										Ritenta questo passo
									</button>
								{/if}
							</li>
						{/each}
					</ul>

					<div class="step-actions">
						{#if can('enablement.retry') && enablement.state === 'failed'}
							<button class="primary small" disabled={busy !== null} onclick={() => retry()}>
								{busy === 'retry' ? 'In corso…' : 'Ritenta tutto'}
							</button>
						{/if}
						{#if can('enablement.revoke') && enablement.state !== 'not_started'}
							<button class="danger small" disabled={busy !== null} onclick={revoke}>
								Revoca abilitazione
							</button>
						{/if}
					</div>
				{:else}
					<p class="muted">Stato non disponibile.</p>
				{/if}
			</section>

			{#if auditEntries.length > 0}
				<section class="panel">
					<h2>Cronologia</h2>
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
	<p class="muted">Caricamento…</p>
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
</style>
