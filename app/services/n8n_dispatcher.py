
import logging
from collections.abc import Callable

from app.config import Settings
from app.services.n8n_client import N8NClient
from app.services.n8n_deduplication import N8NDeduplication
from app.services.n8n_router import should_route_to_n8n
from app.services.tenant_resolver import TenantResolver

logger = logging.getLogger(__name__)


class N8NDispatcher:
    """Route authorized messages to n8n without affecting other users."""

    def __init__(
        self,
        settings: Settings,
        n8n_client: N8NClient,
        send_reply: Callable,
        deduplication: N8NDeduplication | None = None,
    ) -> None:
        self.settings = settings
        self.n8n_client = n8n_client
        self.send_reply = send_reply
        self.deduplication = deduplication
        self.tenant_resolver = TenantResolver(settings)

    async def dispatch(
        self,
        message_id: str,
        wa_id: str,
        text: str,
        phone_number_id: str | None = None,
    ) -> bool:
        """Return True when the message belongs to the n8n integration."""
        
        if phone_number_id is not None:
            tenant = self.tenant_resolver.resolve(phone_number_id)

            if tenant is None:
                logger.warning(
                    "WhatsApp message blocked: unknown channel, "
                    "phone_number_id=%s, message_id=%s",
                    phone_number_id,
                    message_id,
                )
                return True

            logger.info(
                "WhatsApp channel resolved: tenant_id=%s, "
                "phone_number_id=%s, message_id=%s",
                tenant.tenant_id,
                tenant.phone_number_id,
                message_id,
            )

        if not should_route_to_n8n(wa_id, self.settings):
            return False

        if not should_route_to_n8n(wa_id, self.settings):
            return False

        if not message_id or not message_id.strip():
            logger.error(
                "n8n message rejected: missing message ID"
            )
            return True

        if self.deduplication is None:
            logger.error(
                "n8n dispatch blocked: Redis deduplication is required"
            )
            return True

        try:
            token = await self.deduplication.acquire(message_id)

        except Exception:
            logger.exception(
                "Failed to acquire message: %s",
                message_id,
            )
            return True

        if token is None:
            logger.info(
                "Skipping duplicate message: %s",
                message_id,
            )
            return True

        try:
            reply = await self.n8n_client.generate_reply(
                message_id=message_id,
                wa_id=wa_id,
                text=text,
                phone_number_id=phone_number_id,
            )

        except Exception as exc:
            logger.exception(
                "Failed to generate reply: %s",
                message_id,
            )

            try:
                released = await self.deduplication.release_processing(
                    message_id,
                    token,
                )

                if not released:
                    logger.warning(
                        "Could not release processing claim: %s",
                        message_id,
                    )

            except Exception:
                logger.exception(
                    "Failed to release processing claim: %s",
                    message_id,
                )

            # Propagate the failure to the ARQ worker.
            raise RuntimeError(
                f"n8n processing failed for message {message_id}"
            ) from exc

        try:
            started = await self.deduplication.begin_send(
                message_id,
                token,
            )

        except Exception:
            logger.exception(
                "Failed to persist send attempt: %s",
                message_id,
            )
            return True

        if not started:
            logger.error(
                "Send attempt could not be claimed: %s",
                message_id,
            )
            return True

        try:
            meta_response = await self.send_reply(wa_id, reply)

            # Verify that Meta accepted the outgoing message.
            if not isinstance(meta_response, dict):
                raise ValueError(
                    "Invalid Meta response: expected a JSON object"
                )

            messages = meta_response.get("messages")

            if not isinstance(messages, list) or not messages:
                raise ValueError(
                    "Invalid Meta response: missing messages"
                )

            first_message = messages[0]

            if not isinstance(first_message, dict):
                raise ValueError(
                    "Invalid Meta response: invalid message entry"
                )

            meta_message_id = first_message.get("id")

            if (
                not isinstance(meta_message_id, str)
                or not meta_message_id.strip()
            ):
                raise ValueError(
                    "Invalid Meta response: missing message ID"
                )

            logger.info(
                "Meta accepted WhatsApp message: "
                "message_id=%s, meta_message_id=%s",
                message_id,
                meta_message_id,
            )

        except Exception:
            logger.exception(
                "WhatsApp delivery outcome uncertain: %s",
                message_id,
            )

            try:
                marked = await self.deduplication.mark_unknown(
                    message_id,
                    token,
                )

                if not marked:
                    logger.error(
                        "Failed to transition message to unknown: %s",
                        message_id,
                    )

            except Exception:
                logger.exception(
                    "Failed to persist unknown state: %s",
                    message_id,
                )

            return True

        try:
            completed = await self.deduplication.complete(
                message_id,
                token,
            )

            if not completed:
                logger.error(
                    "Failed to transition message to completed: %s",
                    message_id,
                )

        except Exception:
            logger.exception(
                "Failed to persist completed state: %s",
                message_id,
            )

        logger.info(
            "n8n dispatch finished: %s",
            message_id,
        )

        return True
