<script lang="ts">
	import { t } from '$lib/i18n';

	interface UploadedFile {
		file: File;
		status: 'uploading' | 'done' | 'error';
	}

	interface Props {
		label: string;
		hint?: string;
		accept?: string;
		files?: UploadedFile[];
		onadd: (file: File) => void;
	}

	let { label, hint = '', accept = 'image/*,.pdf', files = [], onadd }: Props = $props();

	function onInput(e: Event) {
		const input = e.target as HTMLInputElement;
		if (!input.files) return;
		for (const file of Array.from(input.files)) {
			onadd(file);
		}
		input.value = '';
	}
</script>

<div class="upload-area">
	{#if files.length > 0}
		<div class="file-list">
			{#each files as f, i}
				<div class="file-row">
					<span class="file-name">{f.file.name}</span>
					{#if f.status === 'uploading'}
						<span class="file-status uploading">
							<span class="spinner-sm"></span>
						</span>
					{:else if f.status === 'done'}
						<span class="file-status done">
							<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
								<path d="M20 6 9 17l-5-5"/>
							</svg>
						</span>
					{:else}
						<span class="file-status error">!</span>
					{/if}
				</div>
			{/each}
		</div>
	{/if}

	<label class="add-btn">
		<input type="file" {accept} class="hidden-input" onchange={onInput} capture="environment" />
		{#if files.length === 0}
			<div class="upload-prompt">
				<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
					<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
					<polyline points="17 8 12 3 7 8"/>
					<line x1="12" y1="3" x2="12" y2="15"/>
				</svg>
				<span class="upload-label">{label}</span>
				{#if hint}
					<span class="upload-hint">{hint}</span>
				{/if}
			</div>
		{:else}
			<div class="add-more">
				<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
					<line x1="12" y1="5" x2="12" y2="19"/>
					<line x1="5" y1="12" x2="19" y2="12"/>
				</svg>
				<span>{$t('onboarding.add_page')}</span>
			</div>
		{/if}
	</label>
</div>

<style>
	.upload-area {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-sm);
	}

	.file-list {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.file-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: var(--celine-space-sm) var(--celine-space-md);
		background: var(--celine-bg-sunken);
		border-radius: var(--celine-radius-sm);
		font-size: 0.875rem;
	}

	.file-name {
		color: var(--celine-text);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.file-status {
		flex-shrink: 0;
		display: flex;
		align-items: center;
	}

	.file-status.done {
		color: var(--celine-success);
	}

	.file-status.error {
		color: var(--celine-danger);
		font-weight: 700;
	}

	.spinner-sm {
		width: 1rem;
		height: 1rem;
		border: 2px solid var(--celine-border);
		border-top-color: var(--celine-primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}

	.add-btn {
		cursor: pointer;
	}

	.hidden-input {
		display: none;
	}

	.upload-prompt {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--celine-space-sm);
		padding: var(--celine-space-xl);
		border: 2px dashed var(--celine-border);
		border-radius: var(--celine-radius-lg);
		text-align: center;
		color: var(--celine-text-secondary);
		transition: all var(--celine-transition-fast);
		background: var(--celine-bg-elevated);
	}

	.upload-prompt:hover {
		border-color: var(--celine-primary);
		background: var(--celine-primary-light);
	}

	.upload-label {
		font-weight: 500;
		color: var(--celine-text);
	}

	.upload-hint {
		font-size: 0.8125rem;
		color: var(--celine-text-tertiary);
	}

	.add-more {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: var(--celine-space-xs);
		padding: var(--celine-space-sm) var(--celine-space-md);
		border: 1px dashed var(--celine-border);
		border-radius: var(--celine-radius-md);
		color: var(--celine-primary);
		font-size: 0.875rem;
		font-weight: 500;
		transition: all var(--celine-transition-fast);
	}

	.add-more:hover {
		border-color: var(--celine-primary);
		background: var(--celine-primary-light);
	}
</style>
