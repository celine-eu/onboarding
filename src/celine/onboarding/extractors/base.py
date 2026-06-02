from typing import Protocol


class Extractor(Protocol):
    async def extract(self, image_bytes: bytes, mime_type: str) -> tuple[dict, dict]:
        """Extract structured data from a document image.

        Returns (extracted_data, raw_response).
        """
        ...
