"""Optional local foreground segmentation using the lightweight U2NetP model."""

import logging
from functools import lru_cache

LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _session():
    from rembg import new_session
    return new_session("u2netp", providers=["CPUExecutionProvider"])


@lru_cache(maxsize=128)
def mask_png(url: str, image_bytes: bytes) -> bytes | None:
    """Return a PNG alpha mask, or None when optional inference is unavailable."""
    try:
        from rembg import remove
        return remove(image_bytes, session=_session(), only_mask=True, post_process_mask=True)
    except Exception as exc:  # pragma: no cover - depends on optional runtime/model
        LOGGER.warning("Foreground segmentation unavailable: %s", exc)
        return None
