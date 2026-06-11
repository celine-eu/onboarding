<script lang="ts">
	import { t } from '$lib/i18n';

	interface Props {
		data: Record<string, string | null>;
		onchange: (data: Record<string, string | null>) => void;
	}

	let { data, onchange }: Props = $props();

	let editing = $state(false);
	let fields = $state<Record<string, string | null>>({});

	$effect(() => {
		fields = { ...data };
	});

	const FIELD_LABEL_KEYS: Record<string, string> = {
		nome: 'onboarding.field_nome',
		cognome: 'onboarding.field_cognome',
		codice_fiscale: 'onboarding.field_codice_fiscale',
		pod: 'onboarding.field_pod',
		indirizzo: 'onboarding.field_indirizzo',
		fornitore: 'onboarding.field_fornitore',
		numero_contratto: 'onboarding.field_numero_contratto',
		consumo_annuo: 'onboarding.field_consumo_annuo',
		tipo_documento: 'onboarding.field_tipo_documento',
		data_nascita: 'onboarding.field_data_nascita',
		luogo_nascita: 'onboarding.field_luogo_nascita',
		sesso: 'onboarding.field_sesso',
		numero_documento: 'onboarding.field_numero_documento',
		scadenza: 'onboarding.field_scadenza',
	};

	function fieldLabel(key: string): string {
		const i18nKey = FIELD_LABEL_KEYS[key];
		return i18nKey ? $t(i18nKey) : key;
	}

	function onFieldChange() {
		onchange(fields);
	}
</script>

<div class="extraction">
	<div class="extraction-header">
		<h3 class="extraction-title">{$t('onboarding.extraction_title')}</h3>
		<button class="edit-toggle" onclick={() => editing = !editing}>
			{editing ? $t('common.save') : $t('onboarding.extraction_edit')}
		</button>
	</div>

	<div class="extraction-fields">
		{#each Object.entries(fields) as [key, value]}
			<div class="extraction-field">
				<span class="extraction-label">{fieldLabel(key)}</span>
				{#if editing}
					<input
						class="extraction-input"
						value={value ?? ''}
						oninput={(e) => { fields[key] = (e.target as HTMLInputElement).value || null; onFieldChange(); }}
					/>
				{:else}
					<span class="extraction-value" class:empty={!value}>
						{value ?? '-'}
					</span>
				{/if}
			</div>
		{/each}
	</div>
</div>

<style>
	.extraction {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-sm);
		background: var(--celine-success-bg);
		border-radius: var(--celine-radius-md);
		padding: var(--celine-space-md);
	}

	.extraction-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.extraction-title {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--celine-success-text);
		margin: 0;
	}

	.edit-toggle {
		background: none;
		border: none;
		color: var(--celine-primary);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		padding: 0.25rem 0.5rem;
		border-radius: var(--celine-radius-sm);
		transition: background var(--celine-transition-fast);
	}

	.edit-toggle:hover {
		background: var(--celine-bg-hover);
	}

	.extraction-fields {
		display: grid;
		gap: 1px;
	}

	.extraction-field {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: var(--celine-space-xs) 0;
	}

	.extraction-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--celine-text-secondary);
	}

	.extraction-value {
		font-size: 0.875rem;
		color: var(--celine-text);
		font-weight: 500;
	}

	.extraction-value.empty {
		color: var(--celine-text-tertiary);
	}

	.extraction-input {
		padding: 0.25rem 0.5rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		font-size: 0.8125rem;
		font-family: var(--celine-font-body);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		width: 55%;
		text-align: right;
	}

	.extraction-input:focus {
		outline: 2px solid var(--celine-primary);
		outline-offset: 1px;
	}
</style>
