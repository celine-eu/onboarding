<script lang="ts">
	interface Props {
		label: string;
		name: string;
		type?: string;
		value?: string;
		required?: boolean;
		placeholder?: string;
		error?: string;
		maxlength?: number;
	}

	let { label, name, type = 'text', value = $bindable(''), required = false, placeholder = '', error = '', maxlength }: Props = $props();
</script>

<div class="field">
	<label class="field-label" for={name}>
		{label}
		{#if required}<span class="field-required">*</span>{/if}
	</label>
	<input
		class="field-input"
		class:has-error={!!error}
		id={name}
		{name}
		{type}
		{placeholder}
		{required}
		{maxlength}
		bind:value
	/>
	{#if error}
		<p class="field-error">{error}</p>
	{/if}
</div>

<style>
	.field {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-xs);
	}

	.field-label {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--celine-text);
	}

	.field-required {
		color: var(--celine-danger);
	}

	.field-input {
		padding: 0.625rem 0.875rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-md);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		font-family: var(--celine-font-body);
		font-size: 0.9375rem;
		transition: border-color var(--celine-transition-fast);
	}

	.field-input:focus {
		outline: 2px solid var(--celine-primary);
		outline-offset: 2px;
		border-color: var(--celine-primary);
	}

	.field-input.has-error {
		border-color: var(--celine-danger);
	}

	.field-error {
		font-size: 0.8125rem;
		color: var(--celine-danger-text);
		margin: 0;
	}
</style>
