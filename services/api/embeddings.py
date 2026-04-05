"""Generate text embeddings via Vertex AI for semantic caching."""
import os, asyncio, logging
from google.cloud import aiplatform

PROJECT_ID = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT"))
LOCATION = os.getenv("GCP_LOCATION", "asia-south1")

logger = logging.getLogger(__name__)

_embed_model = None
_gen_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        aiplatform.init(project=PROJECT_ID, location=LOCATION)
        from vertexai.language_models import TextEmbeddingModel
        _embed_model = TextEmbeddingModel.from_pretrained("text-embedding-004")
    return _embed_model


def _get_gen_model():
    global _gen_model
    if _gen_model is None:
        aiplatform.init(project=PROJECT_ID, location=LOCATION)
        from vertexai.generative_models import GenerativeModel
        _gen_model = GenerativeModel("gemini-2.5-flash")
    return _gen_model


async def normalize_topic(raw_topic: str) -> str:
    """Normalize a user query to a canonical topic phrase using Gemini."""
    model = _get_gen_model()
    prompt = (
        "Extract the core educational topic from this user request. "
        "Return ONLY a concise canonical topic phrase (2-6 words), nothing else.\n"
        "Examples:\n"
        '  "Docker videos" → "Docker"\n'
        '  "what is docker" → "Docker"\n'
        '  "explain photosynthesis to me" → "Photosynthesis"\n'
        '  "how does supply and demand work" → "Supply and Demand"\n'
        '  "Pythagorean theorem" → "Pythagorean Theorem"\n\n'
        f'User request: "{raw_topic}"'
    )
    response = await asyncio.to_thread(
        lambda: model.generate_content(prompt)
    )
    normalized = response.text.strip().strip('"').strip("'")
    logger.info("Topic normalization: %r → %r", raw_topic, normalized)
    return normalized


async def generate_embedding(text: str) -> list[float]:
    """Generate a 768-dim embedding for the given text."""
    model = _get_embed_model()
    embeddings = await asyncio.to_thread(
        lambda: model.get_embeddings([text])
    )
    return embeddings[0].values
