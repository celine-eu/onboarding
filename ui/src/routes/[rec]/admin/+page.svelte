<script lang="ts">
	import { onMount } from 'svelte';
	import { createRecAdminApi, type AdminSubmission, type SiteConfig } from '$lib/api/client';

	type SubmissionStatus = 'draft' | 'submitted' | 'under_review' | 'approved' | 'rejected';

	const STATUS_LABELS: Record<string, string> = {
		draft: 'Bozza',
		submitted: 'Inviata',
		under_review: 'In valutazione',
		approved: 'Approvata',
		rejected: 'Rifiutata'
	};

	let { data } = $props();
	let rec: string = $derived(data.rec);
	let config: SiteConfig = $derived(data.config);

	let token = $state('');
	let submissions = $state<AdminSubmission[]>([]);
	let loading = $state(false);
	let errorMsg = $state('');
	let successMsg = $state('');
	let statusFilter = $state('all');
	let pendingAction = $state<string | null>(null);

	let filteredSubmissions = $derived(
		submissions.filter((submission) => statusFilter === 'all' || submission.status === statusFilter)
	);

	onMount(() => {
		token = localStorage.getItem(tokenKey()) ?? '';
		if (token) void loadSubmissions();
	});

	function tokenKey(): string {
		return `cer-admin-token:${rec}`;
	}

	function adminApi() {
		return createRecAdminApi(rec, token.trim());
	}

	function fullName(submission: AdminSubmission): string {
		return [submission.first_name, submission.last_name].filter(Boolean).join(' ') || '-';
	}

	function statusLabel(status: string): string {
		return STATUS_LABELS[status] ?? status;
	}

	function formatDate(value?: string | null): string {
		if (!value) return '-';
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) return '-';
		return new Intl.DateTimeFormat('it-IT', {
			day: '2-digit',
			month: '2-digit',
			year: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		}).format(date);
	}

	function cleanError(error: unknown): string {
		if (!(error instanceof Error)) return 'Operazione non riuscita';
		return error.message.replace(/\s+/g, ' ').trim();
	}

	function actionsFor(status: string): Array<{ status: SubmissionStatus; label: string; tone: string }> {
		if (status === 'submitted') {
			return [
				{ status: 'under_review', label: 'Prendi in carico', tone: 'secondary' },
				{ status: 'rejected', label: 'Rifiuta', tone: 'danger' }
			];
		}
		if (status === 'under_review') {
			return [
				{ status: 'approved', label: 'Approva', tone: 'primary' },
				{ status: 'rejected', label: 'Rifiuta', tone: 'danger' }
			];
		}
		if (status === 'rejected') {
			return [{ status: 'submitted', label: 'Riapri', tone: 'secondary' }];
		}
		return [];
	}

	async function loadSubmissions() {
		errorMsg = '';
		successMsg = '';
		if (!token.trim()) {
			errorMsg = 'Inserisci il token admin.';
			return;
		}

		loading = true;
		try {
			localStorage.setItem(tokenKey(), token.trim());
			submissions = await adminApi().listSubmissions();
		} catch (error) {
			errorMsg = cleanError(error);
		} finally {
			loading = false;
		}
	}

	async function updateStatus(submission: AdminSubmission, status: SubmissionStatus) {
		errorMsg = '';
		successMsg = '';
		if (!token.trim()) {
			errorMsg = 'Inserisci il token admin.';
			return;
		}

		pendingAction = `${submission.id}:${status}`;
		try {
			const updated = await adminApi().updateSubmissionStatus(submission.id, status);
			submissions = submissions.map((item) => (item.id === updated.id ? updated : item));
			successMsg = `Pratica ${updated.ref} aggiornata: ${statusLabel(updated.status)}.`;
		} catch (error) {
			errorMsg = cleanError(error);
		} finally {
			pendingAction = null;
		}
	}
</script>

<svelte:head>
	<title>{config?.name ?? rec}: admin pratiche</title>
</svelte:head>

<section class="admin-page">
	<div class="admin-header">
		<div>
			<a class="back-link" href="/{rec}">Torna alla REC</a>
			<h1>Admin pratiche</h1>
			<p>{config?.name ?? rec}</p>
		</div>
		<div class="summary">
			<strong>{submissions.length}</strong>
			<span>pratiche caricate</span>
		</div>
	</div>

	<form class="toolbar" onsubmit={(event) => { event.preventDefault(); void loadSubmissions(); }}>
		<label class="token-field">
			<span>Token admin</span>
			<input
				type="password"
				bind:value={token}
				placeholder="dev-admin-token"
				autocomplete="off"
			/>
		</label>

		<label class="filter-field">
			<span>Stato</span>
			<select bind:value={statusFilter}>
				<option value="all">Tutte</option>
				<option value="submitted">Inviate</option>
				<option value="under_review">In valutazione</option>
				<option value="approved">Approvate</option>
				<option value="rejected">Rifiutate</option>
				<option value="draft">Bozze</option>
			</select>
		</label>

		<button type="submit" class="primary-btn" disabled={loading}>
			{loading ? 'Caricamento...' : 'Carica pratiche'}
		</button>
	</form>

	{#if errorMsg}
		<p class="message error">{errorMsg}</p>
	{/if}
	{#if successMsg}
		<p class="message success">{successMsg}</p>
	{/if}

	{#if filteredSubmissions.length === 0}
		<div class="empty-state">
			<p>Nessuna pratica da mostrare.</p>
		</div>
	{:else}
		<div class="table-wrap">
			<table>
				<thead>
					<tr>
						<th>Pratica</th>
						<th>Richiedente</th>
						<th>Contatti</th>
						<th>Stato</th>
						<th>Dataspace</th>
						<th>Aggiornata</th>
						<th>Azioni</th>
					</tr>
				</thead>
				<tbody>
					{#each filteredSubmissions as submission}
						<tr>
							<td>
								<strong>{submission.ref}</strong>
								<span class="muted id-text">{submission.id}</span>
							</td>
							<td>
								<strong>{fullName(submission)}</strong>
								<span class="muted">{submission.fiscal_code ?? '-'}</span>
							</td>
							<td>
								<span>{submission.email ?? '-'}</span>
								<span class="muted">{submission.phone ?? '-'}</span>
							</td>
							<td>
								<span class="status-pill" data-status={submission.status}>
									{statusLabel(submission.status)}
								</span>
							</td>
							<td>
								{#if submission.dataspace_vc_id}
									<span class="vc-ok">VC emessa</span>
									<span class="muted id-text">{submission.dataspace_subject_id}</span>
								{:else if submission.status === 'approved'}
									<span class="vc-missing">VC mancante</span>
								{:else}
									<span class="muted">-</span>
								{/if}
							</td>
							<td>{formatDate(submission.updated_at)}</td>
							<td>
								<div class="actions">
									{#each actionsFor(submission.status) as action}
										<button
											type="button"
											class:primary-action={action.tone === 'primary'}
											class:danger-action={action.tone === 'danger'}
											disabled={pendingAction !== null}
											onclick={() => updateStatus(submission, action.status)}
										>
											{pendingAction === `${submission.id}:${action.status}` ? '...' : action.label}
										</button>
									{/each}
									{#if actionsFor(submission.status).length === 0}
										<span class="muted">-</span>
									{/if}
								</div>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</section>

<style>
	.admin-page {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-lg);
	}

	.admin-header {
		display: flex;
		align-items: flex-end;
		justify-content: space-between;
		gap: var(--celine-space-md);
		padding-top: var(--celine-space-md);
	}

	.back-link {
		color: var(--celine-primary);
		font-size: 0.875rem;
		font-weight: 600;
		text-decoration: none;
	}

	h1 {
		color: var(--celine-text);
		font-family: var(--celine-font-display);
		font-size: 1.75rem;
		line-height: 1.2;
		margin: var(--celine-space-xs) 0 0;
	}

	p {
		margin: 0;
	}

	.admin-header p,
	.muted {
		color: var(--celine-text-secondary);
		font-size: 0.8125rem;
	}

	.summary {
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		min-width: 9rem;
		padding: var(--celine-space-sm) var(--celine-space-md);
		text-align: right;
	}

	.summary strong,
	.summary span {
		display: block;
	}

	.summary strong {
		color: var(--celine-text);
		font-size: 1.5rem;
		line-height: 1.1;
	}

	.summary span {
		color: var(--celine-text-secondary);
		font-size: 0.8125rem;
	}

	.toolbar {
		display: grid;
		grid-template-columns: minmax(16rem, 1fr) minmax(10rem, 13rem) auto;
		gap: var(--celine-space-sm);
		align-items: end;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		padding: var(--celine-space-md);
	}

	label {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-xs);
	}

	label span {
		color: var(--celine-text-secondary);
		font-size: 0.8125rem;
		font-weight: 600;
	}

	input,
	select {
		width: 100%;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		font-family: var(--celine-font-body);
		font-size: 0.9375rem;
		min-height: 2.5rem;
		padding: 0 var(--celine-space-sm);
	}

	input:focus,
	select:focus {
		border-color: var(--celine-primary);
		outline: 2px solid var(--celine-primary-light);
	}

	button {
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		cursor: pointer;
		font-family: var(--celine-font-body);
		font-size: 0.875rem;
		font-weight: 600;
		min-height: 2.25rem;
		padding: 0 var(--celine-space-sm);
		transition: background var(--celine-transition-fast), border-color var(--celine-transition-fast);
	}

	button:hover:not(:disabled) {
		background: var(--celine-bg-hover);
		border-color: var(--celine-border-strong);
	}

	button:disabled {
		cursor: wait;
		opacity: 0.65;
	}

	.primary-btn,
	.primary-action {
		background: var(--celine-primary);
		border-color: var(--celine-primary);
		color: var(--celine-primary-text);
	}

	.primary-btn:hover:not(:disabled),
	.primary-action:hover:not(:disabled) {
		background: var(--celine-primary-hover);
		border-color: var(--celine-primary-hover);
	}

	.danger-action {
		border-color: var(--celine-danger);
		color: var(--celine-danger-text);
	}

	.message {
		border-radius: var(--celine-radius-sm);
		font-size: 0.875rem;
		padding: var(--celine-space-sm) var(--celine-space-md);
	}

	.message.error {
		background: var(--celine-danger-bg);
		color: var(--celine-danger-text);
	}

	.message.success {
		background: var(--celine-success-bg);
		color: var(--celine-success-text);
	}

	.empty-state {
		border: 1px dashed var(--celine-border);
		border-radius: var(--celine-radius-sm);
		color: var(--celine-text-secondary);
		padding: var(--celine-space-xl);
		text-align: center;
	}

	.table-wrap {
		overflow-x: auto;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
	}

	table {
		width: 100%;
		border-collapse: collapse;
		min-width: 860px;
	}

	th,
	td {
		border-bottom: 1px solid var(--celine-border);
		padding: var(--celine-space-sm);
		text-align: left;
		vertical-align: top;
	}

	th {
		background: var(--celine-bg-sunken);
		color: var(--celine-text-secondary);
		font-size: 0.75rem;
		font-weight: 700;
		text-transform: uppercase;
	}

	td {
		color: var(--celine-text);
		font-size: 0.875rem;
	}

	td span,
	td strong {
		display: block;
	}

	tr:last-child td {
		border-bottom: none;
	}

	.id-text {
		max-width: 12rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.status-pill,
	.vc-ok,
	.vc-missing {
		border-radius: var(--celine-radius-full);
		display: inline-block;
		font-size: 0.75rem;
		font-weight: 700;
		line-height: 1;
		padding: 0.35rem 0.55rem;
		white-space: nowrap;
	}

	.status-pill[data-status='submitted'],
	.status-pill[data-status='under_review'] {
		background: var(--celine-warning-bg);
		color: var(--celine-warning-text);
	}

	.status-pill[data-status='approved'],
	.vc-ok {
		background: var(--celine-success-bg);
		color: var(--celine-success-text);
	}

	.status-pill[data-status='rejected'],
	.vc-missing {
		background: var(--celine-danger-bg);
		color: var(--celine-danger-text);
	}

	.status-pill[data-status='draft'] {
		background: var(--celine-bg-sunken);
		color: var(--celine-text-secondary);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--celine-space-xs);
		min-width: 11rem;
	}

	@media (max-width: 720px) {
		.admin-header {
			align-items: stretch;
			flex-direction: column;
		}

		.summary {
			text-align: left;
		}

		.toolbar {
			grid-template-columns: 1fr;
		}

		table {
			min-width: 760px;
		}
	}
</style>
