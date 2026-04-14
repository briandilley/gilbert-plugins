"""Name resolver — fuzzy-matches UniFi display names to Gilbert users."""

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Words to strip from UniFi names before matching against user display names.
# These are device-related terms that appear in hostnames and device names.
_DEVICE_TOKENS = frozenset({
    "iphone", "ipad", "macbook", "android", "pixel", "galaxy", "samsung",
    "oneplus", "huawei", "xiaomi", "oppo", "motorola", "nokia", "lg",
    "chromebook", "surface", "thinkpad", "dell", "hp", "lenovo",
    "echo", "alexa", "google", "home", "nest", "sonos", "roku", "firestick",
    "phone", "tablet", "laptop", "watch", "pro", "max", "mini", "plus",
    "air", "se", "wifi", "wireless", "mobile", "device", "unknown", "generic",
})

# Characters to strip when normalizing names
_STRIP_RE = re.compile(r"[^a-z\s]")


@dataclass(frozen=True)
class ResolvedUser:
    """A user match result with confidence."""

    user_id: str
    display_name: str
    confidence: float  # 0.0 to 1.0


class NameResolver:
    """Fuzzy-matches raw names from UniFi to Gilbert user accounts.

    Call ``load_users()`` to populate the resolver with current users,
    then ``resolve()`` to match a raw name.
    """

    def __init__(self, min_confidence: float = 0.3) -> None:
        self._min_confidence = min_confidence
        self._users: list[dict[str, Any]] = []
        # Cache: normalized raw name → ResolvedUser or None
        self._cache: dict[str, ResolvedUser | None] = {}

    async def load_users(self, user_service: Any) -> None:
        """Refresh the user list from the UserService."""
        try:
            self._users = await user_service.list_users()
            self._cache.clear()
            logger.debug("Name resolver loaded %d users", len(self._users))
        except Exception:
            logger.warning("Failed to load users for name resolution", exc_info=True)

    def resolve(self, raw_name: str) -> ResolvedUser | None:
        """Match a raw name to a Gilbert user.

        Returns the best match above min_confidence, or None.
        """
        if not raw_name:
            return None

        cache_key = raw_name.lower().strip()
        if cache_key in self._cache:
            return self._cache[cache_key]

        result = self._find_best_match(raw_name)
        self._cache[cache_key] = result
        return result

    def _find_best_match(self, raw_name: str) -> ResolvedUser | None:
        """Score all users against the raw name and return the best match.

        Matches against multiple user fields:
        - display_name ("Brian Dilley")
        - email local part ("brian.dilley" from "brian.dilley@example.com")
        - full email ("brian.dilley@example.com")
        - user _id ("usr_a3f8b2c1d4e5")
        """
        if not self._users:
            return None

        raw_tokens = _tokenize(raw_name)
        if not raw_tokens:
            return None

        best: ResolvedUser | None = None
        best_score = 0.0

        for user in self._users:
            user_id = user.get("_id", "")
            display_name = user.get("display_name", "")

            # Build list of matchable strings for this user
            candidates: list[str] = []
            if display_name:
                candidates.append(display_name)
            email = user.get("email", "")
            if email:
                candidates.append(email)
                local_part = email.split("@")[0]
                if local_part:
                    # "brian.dilley" → "brian dilley" for token matching
                    candidates.append(local_part.replace(".", " ").replace("_", " "))
            # Don't match against user_id — internal IDs like "usr_569171d4c248"
            # produce garbage token matches against device names.

            # Score against each candidate, keep the best
            for candidate in candidates:
                score = _compute_similarity(raw_tokens, candidate)
                if score > best_score and score >= self._min_confidence:
                    best_score = score
                    best = ResolvedUser(
                        user_id=user_id,
                        display_name=display_name or email or user_id,
                        confidence=score,
                    )

        return best


def _tokenize(raw_name: str) -> list[str]:
    """Normalize and tokenize a raw name, stripping device-related words."""
    # Lowercase, strip punctuation, split on whitespace and hyphens
    cleaned = _STRIP_RE.sub(" ", raw_name.lower().replace("-", " ").replace("'", " "))
    tokens = cleaned.split()
    # Remove device-related tokens
    person_tokens = [t for t in tokens if t not in _DEVICE_TOKENS and len(t) >= 2]
    return person_tokens


def _compute_similarity(raw_tokens: list[str], display_name: str) -> float:
    """Compute a similarity score between tokenized raw name and a display name.

    Returns 0.0 to 1.0. Considers:
    - Exact full match
    - Token overlap (how many raw tokens appear in the display name)
    - Prefix matching (raw token is a prefix of a display name token)
    """
    if not raw_tokens:
        return 0.0

    display_lower = display_name.lower()
    display_tokens = _STRIP_RE.sub(" ", display_lower).split()

    if not display_tokens:
        return 0.0

    # Exact full match (after tokenization)
    raw_joined = " ".join(raw_tokens)
    display_joined = " ".join(display_tokens)
    if raw_joined == display_joined:
        return 1.0

    # Token-level matching
    matched = 0
    for rt in raw_tokens:
        for dt in display_tokens:
            if rt == dt:
                matched += 1
                break
            # Prefix match: "bri" matches "brian", "gregg" matches "gregg"
            # Both tokens must be at least 3 chars to avoid garbage matches
            # like "deering" matching "d" from a hex user_id.
            if len(rt) >= 3 and len(dt) >= 3 and (dt.startswith(rt) or rt.startswith(dt)):
                matched += 0.8
                break

    if matched == 0:
        return 0.0

    # Score based on how many raw tokens matched, weighted by display name coverage
    raw_coverage = matched / len(raw_tokens)
    display_coverage = min(matched, len(display_tokens)) / len(display_tokens)

    # Weighted average favoring display coverage (we want to match real users)
    score = (raw_coverage * 0.4) + (display_coverage * 0.6)

    # Bonus for matching first name (most important token)
    if display_tokens and raw_tokens:
        if raw_tokens[0] == display_tokens[0]:
            score = min(1.0, score + 0.2)
        elif len(raw_tokens[0]) >= 3 and display_tokens[0].startswith(raw_tokens[0]):
            score = min(1.0, score + 0.1)

    return round(score, 3)
