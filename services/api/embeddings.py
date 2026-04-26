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


async def generate_quiz(topic: str, subtopics: list[str]) -> list[dict]:
    """Generate 5 multiple-choice quiz questions about a topic.

    Used to gate path-topic completion: a learner must score >= 80% (4/5)
    before the next path topic unlocks. Cached per video session_id by the
    caller — one Gemini call per unique topic, shared across users.

    Returns a list of dicts: {q, options:[4], correct: int, explain: str}.
    """
    model = _get_gen_model()
    subtopics_block = (
        "\n".join(f"- {s}" for s in subtopics) if subtopics else f"- {topic}"
    )
    prompt = (
        "Generate exactly 5 multiple-choice questions to test comprehension "
        f'of the topic "{topic}", drawing on the following sub-topics covered '
        "in the lesson:\n"
        f"{subtopics_block}\n\n"
        "Each question must have:\n"
        "- A clear, self-contained question (no \"as shown above\" references).\n"
        "- Exactly 4 options.\n"
        "- Exactly one correct option, indicated by its 0-based index.\n"
        "- A brief 1-sentence explanation of why the correct answer is right.\n\n"
        "Return ONLY a JSON array, no markdown, no commentary. Schema:\n"
        '[{"q": "...", "options": ["...", "...", "...", "..."], '
        '"correct": 0, "explain": "..."}, ...]'
    )
    response = await asyncio.to_thread(lambda: model.generate_content(prompt))
    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    try:
        questions = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        questions = json.loads(match.group(0)) if match else []

    cleaned: list[dict] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        q_text = str(q.get("q", "")).strip()
        opts = q.get("options") or []
        correct = q.get("correct")
        explain = str(q.get("explain", "")).strip()
        if (
            q_text
            and isinstance(opts, list)
            and len(opts) == 4
            and isinstance(correct, int)
            and 0 <= correct < 4
        ):
            cleaned.append({
                "q": q_text,
                "options": [str(o).strip() for o in opts],
                "correct": correct,
                "explain": explain,
            })

    logger.info("Quiz for %r: %d valid questions", topic, len(cleaned))
    return cleaned[:5]
