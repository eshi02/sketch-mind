"""Generate text embeddings via Vertex AI for semantic caching."""

import asyncio
import json
import logging
import os
import re

from google.cloud import aiplatform

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT"))
LOCATION = os.getenv("GCP_LOCATION", "asia-south1")

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
    response = await asyncio.to_thread(lambda: model.generate_content(prompt))
    normalized = response.text.strip().strip('"').strip("'")
    logger.info("Topic normalization: %r → %r", raw_topic, normalized)
    return normalized


async def generate_embedding(text: str) -> list[float]:
    """Generate a 768-dim embedding for the given text."""
    model = _get_embed_model()
    embeddings = await asyncio.to_thread(lambda: model.get_embeddings([text]))
    return embeddings[0].values


async def generate_path_outline(title: str) -> list[str]:
    """Break a high-level title into an ordered list of learning topics.

    Used by the learning-path feature when the user provides only a title and
    wants the AI to design a syllabus. Each returned item is a self-contained
    sub-topic suitable for a single short video lesson.
    """
    model = _get_gen_model()
    prompt = (
        "Design a structured learning path for the topic below. "
        "Break it into 5-8 ordered sub-topics that build on each other from "
        "foundational to advanced. Each sub-topic must be a self-contained "
        "lesson title (3-8 words) suitable for a 1-2 minute educational video. "
        "Return ONLY a JSON array of strings, no markdown, no commentary.\n\n"
        "Example for \"Calculus\":\n"
        '["Limits and Continuity", "The Derivative", "Rules of Differentiation", '
        '"Applications of Derivatives", "Integration Basics", "The Fundamental '
        'Theorem of Calculus", "Techniques of Integration"]\n\n'
        f'Topic: "{title}"'
    )
    response = await asyncio.to_thread(lambda: model.generate_content(prompt))
    raw = response.text.strip()

    # Strip code fences if the model added them despite the instruction.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    try:
        topics = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        topics = json.loads(match.group(0)) if match else []

    cleaned = [str(t).strip() for t in topics if str(t).strip()]
    logger.info("Path outline for %r: %d topics", title, len(cleaned))
    return cleaned[:20]
