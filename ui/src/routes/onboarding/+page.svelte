<script lang="ts">
	import { t } from '$lib/i18n';
	import { api, type SiteConfig } from '$lib/api/client';
	import FormField from '$lib/components/FormField.svelte';
	import FileUpload from '$lib/components/FileUpload.svelte';
	import ConsentCheckbox from '$lib/components/ConsentCheckbox.svelte';
	import ExtractionReview from '$lib/components/ExtractionReview.svelte';

	let config = $state<SiteConfig | null>(null);
	let configError = $state('');

	const STEP_LABELS: Record<string, string> = {
		consents: 'onboarding.step_data_consents',
		utility: 'onboarding.step_utility',
		personal: 'onboarding.step_personal',
		statute: 'onboarding.step_statute',
		review: 'onboarding.step_review'
	};

	let steps = $derived(
		config
			? config.steps.map((s) => (typeof s === 'string' ? s : s.title))
			: ['consents', 'utility', 'personal', 'statute', 'review']
	);

	let consentVersions = $derived(
		config
			? {
					gdpr: config.consent.gdpr?.version ?? '1.0',
					policy: config.consent.policy?.version ?? '1.0',
					statute: config.consent.statute?.version ?? '1.0'
				}
			: { gdpr: '1.0', policy: '1.0', statute: '1.0' }
	);

	$effect(() => {
		api.getConfig().then((c) => (config = c)).catch((e) => (configError = String(e)));
	});

	let currentStep = $state(0);
	let submitting = $state(false);
	let submitted = $state(false);
	let errorMsg = $state('');

	// Submission — created on consent, used to link everything
	let submissionId: string | null = $state(null);
	let submissionRef: string | null = $state(null);

	// Data collection consents (step 0)
	let gdprConsent = $state(false);
	let policyConsent = $state(false);

	// Bill & extraction (step 1 — optional)
	interface UploadedFile {
		file: File;
		status: 'uploading' | 'done' | 'error';
	}
	let uploadedFiles = $state<UploadedFile[]>([]);
	let extractionData: Record<string, string | null> | null = $state(null);
	let extracting = $state(false);
	let extractionTimer: ReturnType<typeof setTimeout> | null = null;
	let extractionVersion = 0;

	// Personal data (step 2 — prefilled from extraction)
	let firstName = $state('');
	let lastName = $state('');
	let email = $state('');
	let phone = $state('');
	let fiscalCode = $state('');
	let podCode = $state('');

	// Statute consent (step 3)
	let statuteConsent = $state(false);

	// Optional marketing opt-in
	let keepMeUpdated = $state(false);

	// Validation
	let errors = $state<Record<string, string>>({});
	let validated = $state(false);

	const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
	const PHONE_RE = /^\+?[\d\s\-()]{7,}$/;
	const CF_RE = /^[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]$/i;
	const POD_RE = /^IT\d{3}E\d{8,9}$/i;

	function validateStep2(): boolean {
		const e: Record<string, string> = {};
		if (!firstName.trim()) e.first_name = $t('common.required');
		if (!lastName.trim()) e.last_name = $t('common.required');
		if (email && !EMAIL_RE.test(email)) e.email = $t('onboarding.invalid_email');
		if (phone && !PHONE_RE.test(phone)) e.phone = $t('onboarding.invalid_phone');
		if (!email && !phone) {
			e.email = $t('onboarding.email_or_phone');
			e.phone = $t('onboarding.email_or_phone');
		}
		if (!fiscalCode.trim()) e.fiscal_code = $t('common.required');
		else if (!CF_RE.test(fiscalCode.trim())) e.fiscal_code = $t('onboarding.invalid_cf');
		if (!podCode.trim()) e.pod_code = $t('common.required');
		else if (!POD_RE.test(podCode.trim())) e.pod_code = $t('onboarding.invalid_pod');
		errors = e;
		validated = true;
		return Object.keys(e).length === 0;
	}

	let currentStepName = $derived(steps[currentStep] ?? '');
	let uploading = $derived(uploadedFiles.some((f) => f.status === 'uploading'));
	let stepBusy = $derived(currentStepName === 'utility' && (uploading || extracting));

	function cancelProcessing() {
		extractionVersion++;
		extracting = false;
		if (extractionTimer) {
			clearTimeout(extractionTimer);
			extractionTimer = null;
		}
	}

	function canProceed(): boolean {
		if (currentStepName === 'consents') return gdprConsent && policyConsent;
		if (currentStepName === 'utility') return !stepBusy;
		if (currentStepName === 'personal') {
			if (!validated) return !!firstName && !!lastName && (!!email || !!phone) && !!fiscalCode && !!podCode;
			return Object.keys(errors).length === 0;
		}
		if (currentStepName === 'statute') return statuteConsent;
		return true;
	}

	async function advanceStep() {
		errorMsg = '';

		if (currentStepName === 'consents' && !submissionId) {
			submitting = true;
			try {
				const res = await api.createSubmission({
					gdpr_consent: gdprConsent,
					gdpr_consent_version: consentVersions.gdpr,
					policy_consent: policyConsent,
					policy_consent_version: consentVersions.policy,
					statute_consent: false,
					statute_consent_version: consentVersions.statute,
				});
				submissionId = res.id;
				submissionRef = (res as Record<string, unknown>).ref as string;
			} catch (e) {
				errorMsg = e instanceof Error ? e.message : 'Failed to create submission';
				submitting = false;
				return;
			}
			submitting = false;
		}

		if (currentStepName === 'utility') {
			validated = false;
			errors = {};
		}

		if (currentStepName === 'personal') {
			if (!validateStep2()) return;
			if (!submissionId) return;
			try {
				await api.updateSubmission(submissionId, {
					first_name: firstName,
					last_name: lastName,
					email: email || null,
					phone: phone || null,
					fiscal_code: fiscalCode,
					pod_code: podCode,
				});
			} catch (e) {
				errorMsg = e instanceof Error ? e.message : 'Failed to save data';
				return;
			}
		}

		currentStep++;
	}

	async function onFileAdded(file: File) {
		const entry: UploadedFile = { file, status: 'uploading' };
		uploadedFiles = [...uploadedFiles, entry];
		const idx = uploadedFiles.length - 1;

		if (submissionId) {
			try {
				await api.uploadDocument(submissionId, file, 'utility_bill');
				uploadedFiles[idx].status = 'done';
				uploadedFiles = [...uploadedFiles];
			} catch (e) {
				uploadedFiles[idx].status = 'error';
				uploadedFiles = [...uploadedFiles];
			}
		} else {
			uploadedFiles[idx].status = 'done';
			uploadedFiles = [...uploadedFiles];
		}

		scheduleExtraction();
	}

	function scheduleExtraction() {
		if (extractionTimer) clearTimeout(extractionTimer);
		extractionTimer = setTimeout(runExtraction, 800);
	}

	async function runExtraction() {
		const allFiles = uploadedFiles.map((f) => f.file);
		if (allFiles.length === 0) return;

		const thisVersion = ++extractionVersion;
		extracting = true;
		errorMsg = '';
		try {
			const newData = await api.extractBill(allFiles);
			if (thisVersion !== extractionVersion) return;
			extractionData = mergeExtraction(extractionData, newData);
			applyExtraction(extractionData);
		} catch (e) {
			if (thisVersion !== extractionVersion) return;
			errorMsg = e instanceof Error ? e.message : 'Extraction failed';
		} finally {
			if (thisVersion === extractionVersion) extracting = false;
		}
	}

	function mergeExtraction(
		prev: Record<string, string | null> | null,
		next: Record<string, string | null>
	): Record<string, string | null> {
		if (!prev) return next;
		const merged = { ...prev };
		for (const [key, value] of Object.entries(next)) {
			if (!value) continue;
			const existing = merged[key];
			if (!existing || value.length > existing.length) {
				merged[key] = value;
			}
		}
		return merged;
	}

	function applyExtraction(data: Record<string, string | null>) {
		if (data.nome) firstName = data.nome;
		if (data.cognome) lastName = data.cognome;
		if (data.codice_fiscale) fiscalCode = data.codice_fiscale;
		if (data.pod) podCode = data.pod;
	}

	function onExtractionChange(data: Record<string, string | null>) {
		extractionData = data;
		applyExtraction(data);
	}

	async function handleFinalSubmit() {
		if (!submissionId) return;
		submitting = true;
		errorMsg = '';
		try {
			await api.updateSubmission(submissionId, {
				statute_consent: statuteConsent,
				keep_me_updated: keepMeUpdated,
				status: 'submitted',
			});
			submitted = true;
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : 'Submission failed';
		} finally {
			submitting = false;
		}
	}
</script>

{#if submitted}
	<div class="success-card">
		<div class="success-icon">
			<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M20 6 9 17l-5-5"/>
			</svg>
		</div>
		<h2 class="success-title">{$t('onboarding.submit_success')}</h2>
		<p class="success-detail">{$t('onboarding.submit_success_detail')}</p>
		{#if submissionRef}
			<p class="success-ref">Ref: {submissionRef}</p>
		{/if}
		<a href="/" class="btn btn-secondary">{$t('common.back')}</a>
	</div>
{:else}
	<div class="wizard">
		<h2 class="wizard-title">{$t('onboarding.title')}</h2>

		<!-- Step indicator -->
		<div class="steps">
			{#each steps as step, i}
				<div class="step" class:active={i === currentStep} class:done={i < currentStep}>
					<div class="step-bar"></div>
					<span class="step-label">{$t(STEP_LABELS[step] ?? step)}</span>
				</div>
			{/each}
		</div>

		<!-- Step content -->
		<div class="step-content">
			{#if currentStepName === 'consents'}
				<div class="consents">
					<p class="consent-intro">{config?.content?.consent_intro ?? $t('onboarding.consent_intro')}</p>
					<ConsentCheckbox
						label={$t('onboarding.gdpr_consent')}
						bind:checked={gdprConsent}
						required
						documentUrl="/api/consent-documents/gdpr"
						documentLabel={$t('onboarding.view_document')}
					/>
					<ConsentCheckbox
						label={$t('onboarding.policy_consent')}
						bind:checked={policyConsent}
						required
						documentUrl="/api/consent-documents/policy"
						documentLabel={$t('onboarding.view_document')}
					/>
					<ConsentCheckbox
						label={$t('onboarding.keep_me_updated')}
						bind:checked={keepMeUpdated}
					/>
				</div>
			{:else if currentStepName === 'utility'}
				<div class="step-section">
					<p class="step-hint">{$t('onboarding.upload_bill_optional')}</p>
					<FileUpload
						label={$t('onboarding.upload_bill')}
						hint={$t('onboarding.upload_bill_hint')}
						files={uploadedFiles}
						onadd={onFileAdded}
					/>

					{#if extracting}
						<div class="loading-box">
							<div class="spinner"></div>
							<span>{$t('onboarding.extracting')}</span>
						</div>
					{/if}

					{#if extractionData}
						<ExtractionReview data={extractionData} onchange={onExtractionChange} />
					{/if}
				</div>
			{:else if currentStepName === 'personal'}
				<div class="form-grid">
					<FormField label={$t('onboarding.first_name')} name="first_name" bind:value={firstName} required error={errors.first_name ?? ''} />
					<FormField label={$t('onboarding.last_name')} name="last_name" bind:value={lastName} required error={errors.last_name ?? ''} />
					<FormField label={$t('onboarding.email')} name="email" type="email" bind:value={email} error={errors.email ?? ''} placeholder="email@example.com" />
					<FormField label={$t('onboarding.phone')} name="phone" type="tel" bind:value={phone} error={errors.phone ?? ''} placeholder="+39 ..." />
					<FormField label={$t('onboarding.fiscal_code')} name="fiscal_code" bind:value={fiscalCode} required maxlength={16} error={errors.fiscal_code ?? ''} placeholder="RSSMRA80A01H501U" />
					<FormField label={$t('onboarding.pod_code')} name="pod_code" bind:value={podCode} required maxlength={20} error={errors.pod_code ?? ''} placeholder="IT001E12345678" />
				</div>
			{:else if currentStepName === 'statute'}
				<div class="consents">
					<p class="consent-intro">{$t('onboarding.statute_intro')}</p>
					<ConsentCheckbox
						label={$t('onboarding.statute_consent')}
						bind:checked={statuteConsent}
						required
						documentUrl="/api/consent-documents/statute"
						documentLabel={$t('onboarding.view_document')}
					/>
				</div>
			{:else}
				<div class="review">
					<div class="review-section">
						<h3 class="review-heading">{$t('onboarding.step_personal')}</h3>
						<div class="review-row">
							<span class="review-label">{$t('onboarding.first_name')}</span>
							<span class="review-value">{firstName}</span>
						</div>
						<div class="review-row">
							<span class="review-label">{$t('onboarding.last_name')}</span>
							<span class="review-value">{lastName}</span>
						</div>
						{#if email}
							<div class="review-row">
								<span class="review-label">{$t('onboarding.email')}</span>
								<span class="review-value">{email}</span>
							</div>
						{/if}
						{#if phone}
							<div class="review-row">
								<span class="review-label">{$t('onboarding.phone')}</span>
								<span class="review-value">{phone}</span>
							</div>
						{/if}
					</div>
					<div class="review-section">
						<h3 class="review-heading">{$t('onboarding.step_utility')}</h3>
						<div class="review-row">
							<span class="review-label">{$t('onboarding.fiscal_code')}</span>
							<span class="review-value">{fiscalCode}</span>
						</div>
						<div class="review-row">
							<span class="review-label">{$t('onboarding.pod_code')}</span>
							<span class="review-value">{podCode}</span>
						</div>
					</div>
				</div>
			{/if}
		</div>

		{#if errorMsg}
			<div class="error-banner">{errorMsg}</div>
		{/if}

		<!-- Navigation -->
		<div class="wizard-nav">
			<button
				class="btn btn-secondary"
				disabled={currentStep === 0 || !config}
				onclick={() => currentStep--}
			>
				{$t('common.back')}
			</button>

			{#if currentStep < steps.length - 1}
				<div class="nav-right">
					{#if currentStepName === 'utility' && stepBusy}
						<button class="btn btn-danger-ghost" onclick={cancelProcessing}>
							{$t('common.cancel')}
						</button>
					{/if}
					<button
						class="btn btn-primary"
						disabled={!canProceed() || submitting}
						onclick={advanceStep}
					>
						{submitting ? $t('common.loading') : $t('common.next')}
					</button>
				</div>
			{:else}
				<button
					class="btn btn-primary"
					disabled={submitting}
					onclick={handleFinalSubmit}
				>
					{submitting ? $t('common.loading') : $t('common.submit')}
				</button>
			{/if}
		</div>
	</div>
{/if}

<style>
	.wizard {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-lg);
	}

	.wizard-title {
		font-family: var(--celine-font-display);
		font-size: 1.375rem;
		font-weight: 700;
		color: var(--celine-text);
		margin: 0;
	}

	.steps {
		display: flex;
		gap: var(--celine-space-sm);
	}

	.step {
		flex: 1;
	}

	.step-bar {
		height: 4px;
		border-radius: var(--celine-radius-full);
		background: var(--celine-border);
		transition: background var(--celine-transition-base);
	}

	.step.active .step-bar,
	.step.done .step-bar {
		background: var(--celine-primary);
	}

	.step-label {
		display: block;
		margin-top: var(--celine-space-xs);
		font-size: 0.75rem;
		color: var(--celine-text-tertiary);
		transition: color var(--celine-transition-fast);
	}

	.step.active .step-label {
		color: var(--celine-primary);
		font-weight: 600;
	}

	.step.done .step-label {
		color: var(--celine-text-secondary);
	}

	.step-content {
		background: var(--celine-bg-elevated);
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-lg);
		padding: var(--celine-space-xl);
		box-shadow: var(--celine-shadow-sm);
	}

	.consents {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-sm);
	}

	.consent-intro {
		color: var(--celine-text-secondary);
		font-size: 0.9375rem;
		margin: 0 0 var(--celine-space-sm);
	}

	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--celine-space-md);
	}

	@media (max-width: 480px) {
		.form-grid {
			grid-template-columns: 1fr;
		}
	}

	.step-section {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-md);
	}

	.step-hint {
		color: var(--celine-text-secondary);
		font-size: 0.9375rem;
		margin: 0;
	}

	.loading-box {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: var(--celine-space-sm);
		padding: var(--celine-space-lg);
		color: var(--celine-text-secondary);
		background: var(--celine-bg-sunken);
		border-radius: var(--celine-radius-md);
	}

	.spinner {
		width: 1.25rem;
		height: 1.25rem;
		border: 2px solid var(--celine-border);
		border-top-color: var(--celine-primary);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}

	.review {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-lg);
	}

	.review-section {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-xs);
	}

	.review-heading {
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--celine-text-secondary);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin: 0 0 var(--celine-space-xs);
	}

	.review-row {
		display: flex;
		justify-content: space-between;
		padding: var(--celine-space-xs) 0;
		border-bottom: 1px solid var(--celine-border);
	}

	.review-label {
		font-size: 0.875rem;
		color: var(--celine-text-secondary);
	}

	.review-value {
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--celine-text);
	}

	.error-banner {
		background: var(--celine-danger-bg);
		color: var(--celine-danger-text);
		padding: var(--celine-space-sm) var(--celine-space-md);
		border-radius: var(--celine-radius-md);
		font-size: 0.875rem;
	}

	.wizard-nav {
		display: flex;
		justify-content: space-between;
	}

	.nav-right {
		display: flex;
		gap: var(--celine-space-sm);
	}

	.btn {
		padding: 0.5rem 1rem;
		border-radius: var(--celine-radius-md);
		font-size: 0.9375rem;
		font-weight: 500;
		cursor: pointer;
		border: none;
		transition: all var(--celine-transition-fast);
		text-decoration: none;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-primary {
		background: var(--celine-primary);
		color: var(--celine-primary-text);
	}

	.btn-primary:hover:not(:disabled) {
		background: var(--celine-primary-hover);
	}

	.btn-secondary {
		background: transparent;
		color: var(--celine-text-secondary);
		border: 1px solid var(--celine-border);
	}

	.btn-secondary:hover:not(:disabled) {
		background: var(--celine-bg-hover);
		border-color: var(--celine-border-strong);
	}

	.btn-danger-ghost {
		background: transparent;
		color: var(--celine-danger);
		border: 1px solid var(--celine-danger);
	}

	.btn-danger-ghost:hover {
		background: var(--celine-danger-bg);
	}

	.success-card {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--celine-space-md);
		padding: var(--celine-space-2xl);
		background: var(--celine-bg-elevated);
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-lg);
		text-align: center;
	}

	.success-icon {
		background: var(--celine-success-bg);
		color: var(--celine-success);
		border-radius: var(--celine-radius-full);
		padding: var(--celine-space-md);
		display: flex;
	}

	.success-title {
		font-size: 1.25rem;
		font-weight: 700;
		color: var(--celine-text);
		margin: 0;
	}

	.success-detail {
		color: var(--celine-text-secondary);
		margin: 0;
	}

	.success-ref {
		font-family: var(--celine-font-mono);
		font-size: 0.8125rem;
		color: var(--celine-text-tertiary);
		margin: 0;
	}
</style>
