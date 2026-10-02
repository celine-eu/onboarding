// The email an operator may send an applicant who declared they are already a
// member but is not in the community's register (REQ-0027). Written in the
// applicant's language, which is not necessarily the console's, so the texts
// live here rather than in the operator's i18n namespace. The operator's own
// mail client sends it, from the community's mailbox: this service sends nothing.

type Texts = { subject: string; body: string };

const TEXTS: Record<string, Texts> = {
	it: {
		subject: 'La tua richiesta di adesione a {{community}} ({{ref}})',
		body: [
			'Gentile {{name}},',
			'',
			'grazie per la tua richiesta di adesione a {{community}} (riferimento {{ref}}).',
			'',
			'Hai indicato di essere già socio/a, ma non ti abbiamo trovato nel nostro registro dei soci, quindi non possiamo completare la richiesta così com\'è.',
			'',
			'Se non sei ancora socio/a, puoi presentare una nuova richiesta senza selezionare «Sono già socio/a»: ti chiederemo il codice POD e l\'indirizzo di fornitura.',
			'Se invece sei già socio/a, rispondi a questa email indicando il nome dell\'intestatario della fornitura o il codice POD, così possiamo verificare.',
			'',
			'Cordiali saluti,',
			'{{community}}'
		].join('\n')
	},
	en: {
		subject: 'Your application to join {{community}} ({{ref}})',
		body: [
			'Dear {{name}},',
			'',
			'thank you for your application to join {{community}} (reference {{ref}}).',
			'',
			'You said you are already a member, but we could not find you in our member register, so we cannot complete the application as it is.',
			'',
			'If you are not a member yet, you can apply again without ticking "I am already a member": we will then ask for your POD code and supply address.',
			'If you are a member, please reply to this email with the name the supply is registered to or your POD code, so that we can check.',
			'',
			'Kind regards,',
			'{{community}}'
		].join('\n')
	},
	es: {
		subject: 'Tu solicitud de adhesión a {{community}} ({{ref}})',
		body: [
			'Estimado/a {{name}}:',
			'',
			'gracias por tu solicitud de adhesión a {{community}} (referencia {{ref}}).',
			'',
			'Indicaste que ya eres socio/a, pero no te hemos encontrado en nuestro registro de socios, así que no podemos completar la solicitud tal como está.',
			'',
			'Si todavía no eres socio/a, puedes presentar una nueva solicitud sin marcar «Ya soy socio/a»: entonces te pediremos el código POD y la dirección de suministro.',
			'Si ya eres socio/a, responde a este correo indicando el titular del suministro o tu código POD, para que podamos comprobarlo.',
			'',
			'Un saludo,',
			'{{community}}'
		].join('\n')
	}
};

function fill(text: string, values: Record<string, string>): string {
	return text.replace(/\{\{(\w+)\}\}/g, (_, key: string) => values[key] ?? '');
}

/** A `mailto:` link, or null when the applicant left no email. */
export function rejectionMailto(args: {
	email: string | null | undefined;
	locale: string | null | undefined;
	fallbackLocale: string;
	community: string;
	name: string;
	ref: string;
}): string | null {
	if (!args.email) return null;
	const texts = TEXTS[args.locale ?? ''] ?? TEXTS[args.fallbackLocale] ?? TEXTS.it;
	const values = { community: args.community, name: args.name, ref: args.ref };
	const subject = encodeURIComponent(fill(texts.subject, values));
	const body = encodeURIComponent(fill(texts.body, values));
	return `mailto:${encodeURIComponent(args.email)}?subject=${subject}&body=${body}`;
}
