from __future__ import annotations


def _emotion(
    label: str,
    family: str,
    tier: int,
    accent: str,
    accent2: str,
    background: str,
    text: str,
    *,
    strong_against: str | None = None,
    weak_to: str | None = None,
) -> dict[str, object]:
    theme: dict[str, object] = {
        "label": label,
        "family": family,
        "tier": tier,
        "accent": accent,
        "accent2": accent2,
        "background": background,
        "text": text,
    }
    if strong_against:
        theme["strongAgainst"] = strong_against
    if weak_to:
        theme["weakTo"] = weak_to
    return theme


# Anchored to the dominant endpoints in the game-derived faceset_states tiles.
OMORI_MOOD_THEMES: dict[str, dict[str, object]] = {
    "neutral": _emotion("NEUTRAL", "neutral", 0, "#ffffff", "#777777", "#000000", "#f4f4f4"),
    "happy": _emotion("HAPPY", "happy", 1, "#fbff00", "#ff5bf0", "#120012", "#fff7c2", strong_against="angry", weak_to="sad"),
    "ecstatic": _emotion("ECSTATIC", "happy", 2, "#f7ff00", "#ff20e2", "#180018", "#fff7b0", strong_against="angry", weak_to="sad"),
    "manic": _emotion("MANIC", "happy", 3, "#feff00", "#ff0229", "#1a0005", "#fff9a8", strong_against="angry", weak_to="sad"),
    "sad": _emotion("SAD", "sad", 1, "#0031d1", "#00e5d5", "#000b1a", "#c8f7ff", strong_against="happy", weak_to="angry"),
    "depressed": _emotion("DEPRESSED", "sad", 2, "#0019ab", "#00cea6", "#00091a", "#bdf4ef", strong_against="happy", weak_to="angry"),
    "miserable": _emotion("MISERABLE", "sad", 3, "#000098", "#009e7d", "#000616", "#b8e8e1", strong_against="happy", weak_to="angry"),
    "angry": _emotion("ANGRY", "angry", 1, "#ff2611", "#ffe008", "#160200", "#ffd6c7", strong_against="sad", weak_to="happy"),
    "enraged": _emotion("ENRAGED", "angry", 2, "#ed1400", "#ffd405", "#180000", "#ffc8bd", strong_against="sad", weak_to="happy"),
    "furious": _emotion("FURIOUS", "angry", 3, "#f60000", "#670000", "#0b0000", "#ffb8b8", strong_against="sad", weak_to="happy"),
    "afraid": _emotion("AFRAID", "fear", 1, "#494949", "#161616", "#020202", "#f2f2f2"),
    "stressed_out": _emotion("STRESSED OUT", "fear", 1, "#333333", "#0b0b0b", "#000000", "#f2f2f2"),
    "toast": _emotion("TOAST", "state", 0, "#d9a65f", "#fff0bd", "#080603", "#ffe9bd"),
    "injured": _emotion("INJURED", "state", 0, "#ff3838", "#ff9b7a", "#100000", "#ffd6d0"),
    "victory": _emotion("VICTORY", "state", 0, "#f9e84f", "#4cc6ff", "#090908", "#fff7bc"),
    "defeated": _emotion("DEFEATED", "state", 0, "#c8c8c8", "#ffffff", "#050505", "#eeeeee"),
    "succumb": _emotion("DID NOT SUCCUMB", "state", 0, "#f4f4f4", "#303030", "#000000", "#f4f4f4"),
}


OMORI_MOOD_BACKGROUNDS: dict[str, str] = {
    mood: f"backgrounds/{mood}.png"
    for mood in (
        "neutral", "happy", "ecstatic", "manic", "sad", "depressed",
        "miserable", "angry", "enraged", "furious", "afraid", "stressed_out",
    )
}
