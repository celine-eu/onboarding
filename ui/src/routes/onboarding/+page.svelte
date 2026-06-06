<script lang="ts">
	import { t, locale } from '$lib/i18n';
	import { api, setSessionToken, getSessionToken, type SiteConfig } from '$lib/api/client';
	import FormField from '$lib/components/FormField.svelte';
	import FileUpload from '$lib/components/FileUpload.svelte';
	import ConsentCheckbox from '$lib/components/ConsentCheckbox.svelte';
	import ExtractionReview from '$lib/components/ExtractionReview.svelte';
	import Markdown from '$lib/components/Markdown.svelte';

	let config = $state<SiteConfig | null>(null);
	let configError = $state('');

	const STEP_LABELS: Record<string, string> = {
		consents: 'onboarding.step_data_consents',
		utility: 'onboarding.step_utility',
		personal: 'onboarding.step_personal',
		energy: 'onboarding.step_energy',
		eligibility: 'onboarding.step_eligibility',
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
	let extractionPending = $state(false);
	let extractionTimer: ReturnType<typeof setTimeout> | null = null;
	let extractionVersion = 0;

	// ID card upload & extraction (in personal step)
	let idUploadedFiles = $state<UploadedFile[]>([]);
	let idExtractionData: Record<string, string | null> | null = $state(null);
	let idExtracting = $state(false);
	let idExtractionPending = $state(false);
	let idExtractionTimer: ReturnType<typeof setTimeout> | null = null;
	let idExtractionVersion = 0;

	// Personal data (step 2 — prefilled from extraction)
	let firstName = $state('');
	let lastName = $state('');
	let email = $state('');
	let phone = $state('');
	let fiscalCode = $state('');
	let podCode = $state('');

	// Eligibility
	let eligibilityAddress = $state('');
	let eligibilityChecking = $state(false);
	let eligibilityResult = $state<{
		eligible: boolean;
		municipality?: string;
		postal_code?: string;
		state?: string;
		matched_rule?: string;
		matched_value?: string;
		reason?: string;
	} | null>(null);

	// Statute
	let statuteConsent = $state(false);

	// Optional marketing opt-in
	let keepMeUpdated = $state(false);

	// Dynamic extra fields from manifest
	let extraData = $state<Record<string, unknown>>({});

	function fieldLabel(field: { label: string; [k: string]: unknown }): string {
		const localized = field[`label:${$locale}`];
		return typeof localized === 'string' ? localized : field.label;
	}

	function extraFieldsForStep(step: string) {
		if (!config) return [];
		return config.fields.extra.filter((f) => f.step === step);
	}

	function isFieldVisible(field: { show_if?: { key: string; value: unknown } }): boolean {
		if (!field.show_if) return true;
		return extraData[field.show_if.key] === field.show_if.value;
	}

	function setExtraField(key: string, value: unknown) {
		const updated = { ...extraData, [key]: value };
		if (!config) { extraData = updated; return; }
		for (const f of config.fields.extra) {
			if (f.show_if?.key === key && value !== f.show_if.value) {
				delete updated[f.key];
			}
		}
		extraData = updated;
	}

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
	let idUploading = $derived(idUploadedFiles.some((f) => f.status === 'uploading'));
	let stepBusy = $derived(
		(currentStepName === 'utility' && (uploading || extractionPending || extracting)) ||
		(currentStepName === 'personal' && (idUploading || idExtractionPending || idExtracting))
	);

	function cancelProcessing() {
		extractionVersion++;
		extracting = false;
		extractionPending = false;
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
		if (currentStepName === 'eligibility') return eligibilityResult?.eligible === true;
		if (currentStepName === 'statute') return statuteConsent;

		const stepFields = extraFieldsForStep(currentStepName);
		if (stepFields.length > 0) {
			for (const field of stepFields) {
				if (field.required && isFieldVisible(field)) {
					const val = extraData[field.key];
					if (val === undefined || val === null || val === '') return false;
				}
			}
		}

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
				if (res.session_token) setSessionToken(res.session_token);
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
					extracted_data: extractionData,
					id_extracted_data: idExtractionData,
					extra_data: extraData,
				});
			} catch (e) {
				errorMsg = e instanceof Error ? e.message : 'Failed to save data';
				return;
			}
		}

		const stepExtraFields = extraFieldsForStep(currentStepName);
		if (stepExtraFields.length > 0 && currentStepName !== 'personal' && submissionId) {
			try {
				await api.updateSubmission(submissionId, { extra_data: extraData });
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
		extractionPending = true;
		if (extractionTimer) clearTimeout(extractionTimer);
		extractionTimer = setTimeout(runExtraction, 800);
	}

	async function runExtraction() {
		extractionPending = false;
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
		if (data.indirizzo && !eligibilityAddress) eligibilityAddress = data.indirizzo;
	}

	async function checkEligibility() {
		if (!eligibilityAddress.trim()) return;
		eligibilityChecking = true;
		eligibilityResult = null;
		errorMsg = '';
		try {
			eligibilityResult = await api.checkEligibility({ address: eligibilityAddress });
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : 'Eligibility check failed';
		} finally {
			eligibilityChecking = false;
		}
	}

	function onExtractionChange(data: Record<string, string | null>) {
		extractionData = data;
		applyExtraction(data);
	}

	// ID card upload & extraction handlers
	async function onIdFileAdded(file: File) {
		const entry: UploadedFile = { file, status: 'uploading' };
		idUploadedFiles = [...idUploadedFiles, entry];
		const idx = idUploadedFiles.length - 1;

		if (submissionId) {
			try {
				await api.uploadDocument(submissionId, file, 'id_card');
				idUploadedFiles[idx].status = 'done';
				idUploadedFiles = [...idUploadedFiles];
			} catch {
				idUploadedFiles[idx].status = 'error';
				idUploadedFiles = [...idUploadedFiles];
			}
		} else {
			idUploadedFiles[idx].status = 'done';
			idUploadedFiles = [...idUploadedFiles];
		}

		scheduleIdExtraction();
	}

	function scheduleIdExtraction() {
		idExtractionPending = true;
		if (idExtractionTimer) clearTimeout(idExtractionTimer);
		idExtractionTimer = setTimeout(runIdExtraction, 800);
	}

	async function runIdExtraction() {
		idExtractionPending = false;
		const allFiles = idUploadedFiles.map((f) => f.file);
		if (allFiles.length === 0) return;

		const thisVersion = ++idExtractionVersion;
		idExtracting = true;
		errorMsg = '';
		try {
			const newData = await api.extractIdCard(allFiles);
			if (thisVersion !== idExtractionVersion) return;
			idExtractionData = mergeExtraction(idExtractionData, newData);
			applyIdExtraction(idExtractionData);
		} catch (e) {
			if (thisVersion !== idExtractionVersion) return;
			errorMsg = e instanceof Error ? e.message : 'ID extraction failed';
		} finally {
			if (thisVersion === idExtractionVersion) idExtracting = false;
		}
	}

	function applyIdExtraction(data: Record<string, string | null>) {
		if (data.nome && !firstName) firstName = data.nome;
		if (data.cognome && !lastName) lastName = data.cognome;
		if (data.codice_fiscale && !fiscalCode) fiscalCode = data.codice_fiscale;
	}

	function onIdExtractionChange(data: Record<string, string | null>) {
		idExtractionData = data;
		applyIdExtraction(data);
	}

	// Cross-validation: bill vs ID card
	interface MismatchField {
		field: string;
		bill: string;
		id: string;
	}
	let mismatches = $derived.by(() => {
		if (!extractionData || !idExtractionData) return [];
		const result: MismatchField[] = [];

		const billName = (extractionData.nome ?? '').toUpperCase().trim();
		const idName = (idExtractionData.nome ?? '').toUpperCase().trim();
		if (billName && idName && billName !== idName) {
			result.push({ field: 'first_name', bill: billName, id: idName });
		}

		const billSurname = (extractionData.cognome ?? '').toUpperCase().trim();
		const idSurname = (idExtractionData.cognome ?? '').toUpperCase().trim();
		if (billSurname && idSurname && billSurname !== idSurname) {
			result.push({ field: 'last_name', bill: billSurname, id: idSurname });
		}

		const billCf = (extractionData.codice_fiscale ?? '').toUpperCase().replace(/\s/g, '');
		const idCf = (idExtractionData.codice_fiscale ?? '').toUpperCase().replace(/\s/g, '');
		if (billCf && idCf && billCf !== idCf) {
			result.push({ field: 'fiscal_code', bill: billCf, id: idCf });
		}

		return result;
	});

	async function handleFinalSubmit() {
		if (!submissionId) return;
		submitting = true;
		errorMsg = '';
		try {
			await api.updateSubmission(submissionId, {
				statute_consent: statuteConsent,
				keep_me_updated: keepMeUpdated,
				extra_data: extraData,
				status: 'submitted',
			});
			submitted = true;
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : 'Submission failed';
		} finally {
			submitting = false;
		}
	}

	async function downloadPdf() {
		if (!submissionId) return;
		const headers: Record<string, string> = {};
		const token = getSessionToken();
		if (token) headers['X-Session-Token'] = token;
		const res = await fetch(`/api/submissions/${submissionId}/pdf`, { headers });
		if (!res.ok) return;
		const blob = await res.blob();
		const url = URL.createObjectURL(blob);
		window.open(url, '_blank');
	}
</script>

{#if submitted}
	<div class="success-card">
		<div class="success-icon">
			<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M20 6 9 17l-5-5"/>
			</svg>
		</div>
		{#if config?.content?.success}
			<Markdown content={config.content.success} />
		{:else}
			<h2 class="success-title">{$t('onboarding.submit_success')}</h2>
		{/if}
		{#if submissionRef}
			<p class="success-ref">Ref: {submissionRef}</p>
		{/if}
		<div class="success-actions">
			{#if submissionId}
				<button class="btn btn-primary" onclick={downloadPdf}>
					{$t('onboarding.download_pdf')}
				</button>
			{/if}
			<a href="/" class="btn btn-secondary">{$t('common.back')}</a>
		</div>
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
					{#if config?.content?.consent_intro}
						<Markdown content={config.content.consent_intro} />
					{:else}
						<p class="consent-intro">{$t('onboarding.consent_intro')}</p>
					{/if}
					<ConsentCheckbox
						label={$t('onboarding.gdpr_consent')}
						bind:checked={gdprConsent}
						required
						documentUrl={config?.consent?.gdpr?.url ?? '/api/consent-documents/gdpr'}
						documentLabel={$t('onboarding.view_document')}
					/>
					<ConsentCheckbox
						label={$t('onboarding.policy_consent')}
						bind:checked={policyConsent}
						required
						documentUrl={config?.consent?.policy?.url ?? '/api/consent-documents/policy'}
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
				<div class="step-section id-upload-section">
					<FileUpload
						label={$t('onboarding.upload_id')}
						hint={$t('onboarding.upload_id_hint')}
						files={idUploadedFiles}
						onadd={onIdFileAdded}
					/>

					{#if idExtracting}
						<div class="loading-box">
							<div class="spinner"></div>
							<span>{$t('onboarding.id_extracting')}</span>
						</div>
					{/if}

					{#if idExtractionData}
						<ExtractionReview data={idExtractionData} onchange={onIdExtractionChange} />
					{/if}
				</div>

				{#if mismatches.length > 0}
					<div class="mismatch-banner">
						<strong>{$t('onboarding.mismatch_title')}</strong>
						<ul class="mismatch-list">
							{#each mismatches as m}
								<li>
									<strong>{$t(`onboarding.${m.field}`)}</strong>:
									{$t('onboarding.mismatch_bill')} <em>{m.bill}</em> /
									{$t('onboarding.mismatch_id')} <em>{m.id}</em>
								</li>
							{/each}
						</ul>
					</div>
				{/if}

				<div class="form-grid">
					<FormField label={$t('onboarding.first_name')} name="first_name" bind:value={firstName} required error={errors.first_name ?? ''} />
					<FormField label={$t('onboarding.last_name')} name="last_name" bind:value={lastName} required error={errors.last_name ?? ''} />
					<FormField label={$t('onboarding.email')} name="email" type="email" bind:value={email} error={errors.email ?? ''} placeholder="email@example.com" />
					<FormField label={$t('onboarding.phone')} name="phone" type="tel" bind:value={phone} error={errors.phone ?? ''} placeholder="+39 ..." />
					<FormField label={$t('onboarding.fiscal_code')} name="fiscal_code" bind:value={fiscalCode} required maxlength={16} error={errors.fiscal_code ?? ''} placeholder="RSSMRA80A01H501U" />
					<FormField label={$t('onboarding.pod_code')} name="pod_code" bind:value={podCode} required maxlength={20} error={errors.pod_code ?? ''} placeholder="IT001E12345678" />
				</div>
				{#if extraFieldsForStep('personal').length > 0}
					<div class="extra-fields">
						{#each extraFieldsForStep('personal') as field (field.key)}
							{#if isFieldVisible(field)}
								{#if field.type === 'boolean'}
									<label class="toggle-field">
										<input type="checkbox" checked={!!extraData[field.key]} onchange={(e) => { setExtraField(field.key, e.currentTarget.checked); }} />
										<span>{fieldLabel(field)}{#if field.required}<span class="field-required">*</span>{/if}</span>
									</label>
								{:else if field.type === 'select' && field.options}
									<div class="field">
										<label class="field-label" for={field.key}>{fieldLabel(field)}{#if field.required}<span class="field-required">*</span>{/if}</label>
										<select class="field-input" id={field.key} value={extraData[field.key] ?? ''} onchange={(e) => { setExtraField(field.key, e.currentTarget.value); }}>
											<option value="">—</option>
											{#each field.options as opt}
												<option value={opt.value}>{opt.label}</option>
											{/each}
										</select>
									</div>
								{:else}
									<div class="field">
									<label class="field-label" for={field.key}>{fieldLabel(field)}{#if field.required}<span class="field-required">*</span>{/if}</label>
									<input
										class="field-input"
										id={field.key}
										name={field.key}
										type={field.type === 'number' ? 'number' : 'text'}
										value={extraData[field.key] ?? ''}
										placeholder={field.placeholder ?? ''}
										oninput={(e) => { setExtraField(field.key, field.type === 'number' ? (e.currentTarget.value ? Number(e.currentTarget.value) : null) : e.currentTarget.value); }}
									/>
								</div>
								{/if}
							{/if}
						{/each}
					</div>
				{/if}
			{:else if currentStepName === 'eligibility'}
				<div class="step-section">
					<p class="step-hint">{$t('onboarding.eligibility_intro')}</p>
					<div class="eligibility-row">
						<FormField
							label={$t('onboarding.eligibility_address')}
							name="eligibility_address"
							bind:value={eligibilityAddress}
							required
							placeholder="Via Roma 1, Borgo Valsugana"
						/>
						<button
							class="btn btn-primary eligibility-btn"
							disabled={!eligibilityAddress.trim() || eligibilityChecking}
							onclick={checkEligibility}
						>
							{eligibilityChecking ? $t('onboarding.eligibility_checking') : $t('onboarding.eligibility_check')}
						</button>
					</div>

					{#if eligibilityResult}
						{#if eligibilityResult.eligible}
							<div class="eligibility-ok">
								<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
									<path d="M20 6 9 17l-5-5"/>
								</svg>
								<span>{$t('onboarding.eligibility_ok')}</span>
								<span class="eligibility-detail">
									{[eligibilityResult.municipality, eligibilityResult.postal_code, eligibilityResult.state].filter(Boolean).join(' - ')}
								</span>
							</div>
						{:else}
							<div class="eligibility-fail">
								<span>{$t('onboarding.eligibility_fail')}</span>
								<span class="eligibility-detail">
									{[eligibilityResult.municipality, eligibilityResult.postal_code].filter(Boolean).join(' - ')}
								</span>
								{#if eligibilityResult.reason}
									<span class="eligibility-reason">{eligibilityResult.reason}</span>
								{/if}
							</div>
						{/if}
					{/if}
				</div>
			{:else if extraFieldsForStep(currentStepName).length > 0}
				<div class="extra-fields-step">
					{#each extraFieldsForStep(currentStepName) as field (field.key)}
						{#if isFieldVisible(field)}
							{#if field.type === 'boolean'}
								<label class="toggle-field">
									<input type="checkbox" checked={!!extraData[field.key]} onchange={(e) => { setExtraField(field.key, e.currentTarget.checked); }} />
									<span>{fieldLabel(field)}{#if field.required}<span class="field-required">*</span>{/if}</span>
								</label>
							{:else if field.type === 'select' && field.options}
								<div class="field">
									<label class="field-label" for={field.key}>{fieldLabel(field)}{#if field.required}<span class="field-required">*</span>{/if}</label>
									<select class="field-input" id={field.key} value={extraData[field.key] ?? ''} onchange={(e) => { setExtraField(field.key, e.currentTarget.value); }}>
										<option value="">—</option>
										{#each field.options as opt}
											<option value={opt.value}>{opt.label}</option>
										{/each}
									</select>
								</div>
							{:else}
								<div class="extra-field-row">
									<div class="field">
									<label class="field-label" for={field.key}>{fieldLabel(field)}{#if field.required}<span class="field-required">*</span>{/if}</label>
									<input
										class="field-input"
										id={field.key}
										name={field.key}
										type={field.type === 'number' ? 'number' : 'text'}
										value={extraData[field.key] ?? ''}
										placeholder={field.placeholder ?? ''}
										oninput={(e) => { setExtraField(field.key, field.type === 'number' ? (e.currentTarget.value ? Number(e.currentTarget.value) : null) : e.currentTarget.value); }}
									/>
								</div>
									{#if field.suffix}
										<span class="field-suffix">{field.suffix}</span>
									{/if}
								</div>
							{/if}
						{/if}
					{/each}
				</div>
			{:else if currentStepName === 'statute'}
				<div class="consents">
					<p class="consent-intro">{$t('onboarding.statute_intro')}</p>
					<ConsentCheckbox
						label={$t('onboarding.statute_consent')}
						bind:checked={statuteConsent}
						required
						documentUrl={config?.consent?.statute?.url ?? '/api/consent-documents/statute'}
						documentLabel={$t('onboarding.view_document')}
					/>
				</div>
			{:else}
				<div class="review">
					{#if mismatches.length > 0}
						<div class="mismatch-banner">
							<strong>{$t('onboarding.mismatch_title')}</strong>
							<ul class="mismatch-list">
								{#each mismatches as m}
									<li>
										<strong>{$t(`onboarding.${m.field}`)}</strong>:
										{$t('onboarding.mismatch_bill')} <em>{m.bill}</em> /
										{$t('onboarding.mismatch_id')} <em>{m.id}</em>
									</li>
								{/each}
							</ul>
						</div>
					{/if}
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
					{#if config && Object.keys(extraData).length > 0}
						{@const extraSteps = [...new Set(config.fields.extra.map((f) => f.step))]}
						{#each extraSteps as step}
							{@const stepFields = config.fields.extra.filter((f) => f.step === step && extraData[f.key] !== undefined && extraData[f.key] !== null && extraData[f.key] !== '')}
							{#if stepFields.length > 0}
								<div class="review-section">
									<h3 class="review-heading">{$t(STEP_LABELS[step] ?? step)}</h3>
									{#each stepFields as field}
										<div class="review-row">
											<span class="review-label">{fieldLabel(field)}</span>
											<span class="review-value">
												{#if typeof extraData[field.key] === 'boolean'}
													{extraData[field.key] ? $t('onboarding.yes') : $t('onboarding.no')}
												{:else if field.type === 'select' && field.options}
													{field.options.find((o) => o.value === extraData[field.key])?.label ?? extraData[field.key]}
												{:else}
													{extraData[field.key]}{#if field.suffix} {field.suffix}{/if}
												{/if}
											</span>
										</div>
									{/each}
								</div>
							{/if}
						{/each}
					{/if}
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

	.eligibility-row {
		display: flex;
		gap: var(--celine-space-sm);
		align-items: flex-end;
	}

	.eligibility-row :global(.field) {
		flex: 1;
	}

	.eligibility-btn {
		flex-shrink: 0;
		height: 2.625rem;
	}

	.eligibility-ok {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--celine-space-sm);
		padding: var(--celine-space-md);
		background: var(--celine-success-bg);
		color: var(--celine-success-text);
		border-radius: var(--celine-radius-md);
		font-weight: 500;
		font-size: 0.9375rem;
	}

	.eligibility-fail {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-xs);
		padding: var(--celine-space-md);
		background: var(--celine-danger-bg);
		color: var(--celine-danger-text);
		border-radius: var(--celine-radius-md);
		font-size: 0.9375rem;
	}

	.eligibility-detail {
		font-size: 0.8125rem;
		font-weight: 400;
		width: 100%;
	}

	.eligibility-reason {
		font-size: 0.8125rem;
		opacity: 0.8;
	}

	.extra-fields,
	.extra-fields-step {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-md);
		margin-top: var(--celine-space-md);
	}

	.extra-fields-step {
		margin-top: 0;
	}

	.toggle-field {
		display: flex;
		align-items: center;
		gap: var(--celine-space-sm);
		cursor: pointer;
		font-size: 0.9375rem;
		color: var(--celine-text);
		padding: var(--celine-space-sm) 0;
	}

	.toggle-field input[type='checkbox'] {
		width: 1.125rem;
		height: 1.125rem;
		accent-color: var(--celine-primary);
		cursor: pointer;
		flex-shrink: 0;
	}

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

	select.field-input {
		cursor: pointer;
	}

	.extra-field-row {
		display: flex;
		align-items: flex-end;
		gap: var(--celine-space-sm);
	}

	.extra-field-row .field {
		flex: 1;
	}

	.field-suffix {
		font-size: 0.875rem;
		color: var(--celine-text-secondary);
		padding-bottom: 0.75rem;
		white-space: nowrap;
	}

	.error-banner {
		background: var(--celine-danger-bg);
		color: var(--celine-danger-text);
		padding: var(--celine-space-sm) var(--celine-space-md);
		border-radius: var(--celine-radius-md);
		font-size: 0.875rem;
	}

	.mismatch-banner {
		background: #fef3c7;
		color: #92400e;
		border: 1px solid #f59e0b;
		padding: var(--celine-space-sm) var(--celine-space-md);
		border-radius: var(--celine-radius-md);
		font-size: 0.875rem;
		margin-bottom: var(--celine-space-md);
	}

	.mismatch-list {
		margin: var(--celine-space-xs) 0 0;
		padding-left: 1.2em;
	}

	.mismatch-list li {
		margin-bottom: 0.25em;
	}

	.mismatch-list em {
		font-style: normal;
		font-weight: 600;
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

	.success-actions {
		display: flex;
		gap: var(--celine-space-sm);
	}

	.success-ref {
		font-family: var(--celine-font-mono);
		font-size: 0.8125rem;
		color: var(--celine-text-tertiary);
		margin: 0;
	}
</style>
