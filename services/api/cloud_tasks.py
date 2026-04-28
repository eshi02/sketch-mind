"""Cloud Tasks enqueue helper for subtopic processing.

Three modes resolved at startup from env vars:

- Production: SUBTOPIC_QUEUE set, CLOUD_TASKS_EMULATOR_HOST unset
  -> google-cloud-tasks SDK targets real Cloud Tasks, OIDC auth attached.
- Local dev: SUBTOPIC_QUEUE set, CLOUD_TASKS_EMULATOR_HOST set
  -> SDK transparently targets the emulator (the env var is honored by the SDK
  itself, no code branching needed). OIDC headers omitted.
- Direct fallback: SUBTOPIC_QUEUE unset
  -> Plain httpx POST to the agents service. Bypasses the queue entirely; only
  for single-service debugging outside docker-compose.
"""
from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
LOCATION = os.getenv("CLOUD_TASKS_LOCATION", "")
QUEUE = os.getenv("SUBTOPIC_QUEUE", "")
OIDC_SA = os.getenv("OIDC_SERVICE_ACCOUNT_EMAIL", "")
EMULATOR_HOST = os.getenv("CLOUD_TASKS_EMULATOR_HOST", "")

AGENTS_URL = os.getenv("AGENTS_SERVICE_URL", "")


def _queue_enabled() -> bool:
    return bool(QUEUE and PROJECT_ID and LOCATION and AGENTS_URL)


_client = None


def _get_client():
    """Lazy-init the Cloud Tasks client. Imported lazily so the package is
    optional in test environments that go through the direct-HTTP fallback.

    Unlike Pub/Sub / Firestore, the Cloud Tasks SDK does NOT honor an
    `*_EMULATOR_HOST` env var on its own. When EMULATOR_HOST is set we have to
    build the client with an explicit insecure gRPC channel pointing at the
    emulator; otherwise the SDK tries to authenticate against the real GCP
    Tasks API and fails with `Permission denied on resource project ...`.
    """
    global _client
    if _client is None:
        from google.cloud import tasks_v2  # type: ignore
        if EMULATOR_HOST:
            import grpc
            from google.cloud.tasks_v2.services.cloud_tasks.transports import (
                CloudTasksGrpcTransport,
            )
            channel = grpc.insecure_channel(EMULATOR_HOST)
            transport = CloudTasksGrpcTransport(channel=channel)
            _client = tasks_v2.CloudTasksClient(transport=transport)
            logger.info("[cloud_tasks] using emulator at %s", EMULATOR_HOST)
        else:
            _client = tasks_v2.CloudTasksClient()
    return _client


async def enqueue_subtopic_task(
    video_id: str, subtopic_data: dict, index: int,
) -> None:
    """Enqueue one subtopic for processing. Returns immediately after enqueue."""
    payload = {
        "video_id": video_id,
        "subtopic_data": subtopic_data,
        "index": index,
    }

    if not _queue_enabled():
        logger.info(
            "[cloud_tasks] queue disabled, falling back to direct HTTP "
            "(video_id=%s, index=%d)", video_id, index,
        )
        await _direct_post(payload)
        return

    from google.cloud import tasks_v2  # type: ignore

    client = _get_client()
    parent = client.queue_path(PROJECT_ID, LOCATION, QUEUE)
    target = f"{AGENTS_URL}/process-subtopic-task"

    http_request = {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": target,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload).encode(),
    }

    if OIDC_SA and not EMULATOR_HOST:
        http_request["oidc_token"] = {
            "service_account_email": OIDC_SA,
            "audience": AGENTS_URL,
        }

    task = {"http_request": http_request}

    logger.info(
        "[cloud_tasks] enqueue video_id=%s index=%d -> %s%s",
        video_id, index, target,
        " (emulator)" if EMULATOR_HOST else "",
    )

    try:
        client.create_task(request={"parent": parent, "task": task})
    except Exception as exc:
        logger.exception(
            "[cloud_tasks] enqueue failed video_id=%s index=%d: %s",
            video_id, index, exc,
        )
        raise


async def _direct_post(payload: dict) -> None:
    """Fallback path: skip queue, POST straight to agents. Fire-and-forget so
    the API doesn't block on the 3-9 minute pipeline."""
    import asyncio

    async def _run():
        headers = {"Content-Type": "application/json"}
        if AGENTS_URL and "run.app" in AGENTS_URL:
            import google.auth.transport.requests
            import google.oauth2.id_token
            auth_req = google.auth.transport.requests.Request()
            tok = google.oauth2.id_token.fetch_id_token(auth_req, AGENTS_URL)
            headers["Authorization"] = f"Bearer {tok}"
        timeouts = httpx.Timeout(connect=30, read=1800, write=30, pool=60)
        async with httpx.AsyncClient(timeout=timeouts, headers=headers) as client:
            try:
                resp = await client.post(
                    f"{AGENTS_URL}/process-subtopic-task", json=payload,
                )
                if resp.status_code >= 500:
                    logger.error(
                        "[cloud_tasks] direct fallback got %d for video_id=%s index=%d",
                        resp.status_code, payload["video_id"], payload["index"],
                    )
            except Exception as exc:
                logger.exception(
                    "[cloud_tasks] direct fallback failed: %s", exc,
                )

    asyncio.create_task(_run())
