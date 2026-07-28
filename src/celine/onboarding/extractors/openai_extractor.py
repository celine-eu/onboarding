import base64
import io
import json
import tempfile
from pathlib import Path

import pymupdf
from markitdown import MarkItDown
from openai import AsyncOpenAI
from PIL import Image

from celine.onboarding.config.settings import settings

IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

MAX_DIMENSION = 1600
JPEG_QUALITY = 75

EXTRACTION_SYSTEM_PROMPT = (
    "You are an expert at extracting structured data from Italian energy utility bills (bollette). "
    "Analyze all provided pages (images and/or text) and return ONLY a JSON object with these fields:\n\n"
    "{\n"
    '  "nome": "account holder first name",\n'
    '  "cognome": "account holder last name",\n'
    '  "codice_fiscale": "Italian fiscal code (16 alphanumeric characters, e.g. RSSMRA80A01H501U)",\n'
    '  "pod": "POD code (starts with IT, 3 digits, letter E, then 8 digits, e.g. IT221E00450738)",\n'
    '  "indirizzo": "full supply address",\n'
    '  "comune": "municipality of the supply address, name only (e.g. Lavarone)",\n'
    '  "fornitore": "energy provider name",\n'
    '  "numero_contratto": "contract number",\n'
    '  "consumo_annuo": "annual consumption in kWh as integer (e.g. 2700)"\n'
    "}\n\n"
    "Rules:\n"
    "- Return ONLY the JSON, no additional text.\n"
    "- If a field is not found, use null.\n"
    "- The POD code format is: IT + 3 digits + E + 8 digits (14 chars total). "
    "Look for labels like 'Codice POD', 'POD', or 'Punto di Prelievo'.\n"
    "- The codice fiscale is 16 alphanumeric characters. "
    "Look for labels like 'Codice Fiscale', 'C.F.', or 'CF'.\n"
    "- For consumo_annuo, look for labels like 'Consumo annuo', 'kWh/anno', 'Consumo stimato', "
    "'Totale consumo', or similar. Return the yearly figure as an integer in kWh. "
    "If only a partial period is shown, extrapolate to 12 months.\n"
    "- Search ALL provided pages. The POD is often on a different page than the personal details."
)

EXTRACTION_USER_PROMPT = "Extract the data from this utility bill."

ID_CARD_SYSTEM_PROMPT = (
    "You are an expert at extracting structured data from identity documents. "
    "Supported documents: Italian Carta d'Identita, CIE (Carta d'Identita Elettronica), "
    "Passaporto, EU national ID cards, and passports.\n\n"
    "Analyze all provided images (front and back if present) and return ONLY a JSON object "
    "with these fields:\n\n"
    "{\n"
    '  "tipo_documento": "CI" or "CIE" or "PASSAPORTO" or "ID_CARD" or "PASSPORT",\n'
    '  "nome": "first name",\n'
    '  "cognome": "last name",\n'
    '  "codice_fiscale": "Italian fiscal code (16 alphanumeric chars) or null if not present",\n'
    '  "data_nascita": "birth date in DD/MM/YYYY format",\n'
    '  "luogo_nascita": "birth place",\n'
    '  "sesso": "M or F",\n'
    '  "numero_documento": "document number",\n'
    '  "scadenza": "expiry date in DD/MM/YYYY format"\n'
    "}\n\n"
    "Rules:\n"
    "- Return ONLY the JSON, no additional text.\n"
    "- If a field is not found, use null.\n"
    "- The codice fiscale is 16 alphanumeric characters. It may appear on the back of Italian IDs.\n"
    "- For CIE, the document number format is CA followed by 5 digits and 2 letters.\n"
    "- Normalize names to UPPERCASE.\n"
    "- Search ALL provided images. Front and back may contain different fields."
)

ID_CARD_USER_PROMPT = "Extract the data from this identity document."


def _detect_mime(data: bytes) -> str:
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def _compress_image(image_bytes: bytes) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_DIMENSION:
        ratio = MAX_DIMENSION / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), "image/jpeg"


MIN_TEXT_LENGTH = 100
PDF_DPI = 200


def _pdf_to_text(pdf_bytes: bytes) -> str:
    md = MarkItDown()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name
    try:
        result = md.convert(tmp_path)
        return result.text_content
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _pdf_to_images(pdf_bytes: bytes) -> list[tuple[bytes, str]]:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=PDF_DPI)
        img_bytes = pix.tobytes("jpeg")
        compressed, cmime = _compress_image(img_bytes)
        images.append((compressed, cmime))
    doc.close()
    return images


class OpenAIExtractor:
    async def extract(
        self, file_bytes: bytes, declared_mime: str, *,
        system_prompt: str | None = None, user_prompt: str | None = None,
    ) -> tuple[dict, dict]:
        return await self.extract_pages(
            [(file_bytes, declared_mime)],
            system_prompt=system_prompt, user_prompt=user_prompt,
        )

    async def extract_pages(
        self, pages: list[tuple[bytes, str]], *,
        system_prompt: str | None = None, user_prompt: str | None = None,
    ) -> tuple[dict, dict]:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")

        content: list = []
        for i, (data, declared_mime) in enumerate(pages):
            detected = _detect_mime(data)
            mime = detected if detected != "application/octet-stream" else declared_mime

            if mime == "application/pdf":
                text = _pdf_to_text(data)
                if len(text.strip()) >= MIN_TEXT_LENGTH:
                    content.append({
                        "type": "text",
                        "text": f"--- Page {i + 1} (PDF text) ---\n{text}",
                    })
                else:
                    for img_bytes, img_mime in _pdf_to_images(data):
                        b64 = base64.b64encode(img_bytes).decode("utf-8")
                        content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{img_mime};base64,{b64}"},
                        })
            elif mime in IMAGE_MIME_TYPES:
                compressed, cmime = _compress_image(data)
                b64 = base64.b64encode(compressed).decode("utf-8")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{cmime};base64,{b64}"},
                })
            else:
                raise ValueError(f"Unsupported file type: {mime}")

        content.append({"type": "text", "text": user_prompt or EXTRACTION_USER_PROMPT})

        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.extraction_base_url,
        )

        response = await client.chat.completions.create(
            model=settings.extraction_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt or EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            max_completion_tokens=500,
        )

        raw = response.model_dump()
        extracted = json.loads(response.choices[0].message.content or "{}")

        return extracted, raw
