"""Generate text embeddings via Vertex AI for semantic caching."""
import os
from google.cloud import aiplatform

PROJECT_ID = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT"))
LOCATION = os.getenv("GCP_LOCATION", "asia-south1")

_model = None


def _get_model():
    global _model
    if _model is None:
        aiplatform.init(project=PROJECT_ID, location=LOCATION)
        from vertexai.language_models import TextEmbeddingModel
        _model = TextEmbeddingModel.from_pretrained("text-embedding-004")
    return _model


async def generate_embedding(text: str) -> list[float]:
    """Generate a 768-dim embedding for the given text."""
    import asyncio
    model = _get_model()
    embeddings = await asyncio.to_thread(
        lambda: model.get_embeddings([text])
    )
    return embeddings[0].values
