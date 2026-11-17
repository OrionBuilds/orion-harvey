"""The assistant persona: loading its knowledge and composing its messages.

Greeting and sign-off are composed here, in code, never by the model, so they
cannot drift, be forgotten, or be turned into a claim that someone else wrote
the message. The model writes only the body.
"""

from pathlib import Path

from harvey.paths import PROJECT_ROOT

PERSONA_DIR = PROJECT_ROOT / "skills" / "persona"

# What the Writer may draw on, in the order it reads it. The rules that must
# win (boundaries) come last so they are freshest in the prompt.
WRITER_FILES = ("identity", "icp", "offers_pricing", "stories", "voice", "boundaries")


# What the Conversationalist may draw on. Boundaries come last so they are freshest.
CONVERSATION_FILES = ("identity", "icp", "offers_pricing", "stories", "opinions", "voice", "faq",
                      "logistics", "boundaries")


def load_conversation_prompt(directory: Path | None = None) -> str:
    """The reply prompt the owner approved; empty when the file is missing."""
    try:
        return ((directory or PERSONA_DIR) / "conversation_prompt.md").read_text()
    except OSError:
        return ""


def is_assistant(config) -> bool:
    return getattr(getattr(config, "persona", None), "mode", "self") == "assistant"


def load_persona_context(files: tuple[str, ...] = WRITER_FILES,
                         directory: Path | None = None) -> tuple[str, list[str]]:
    """The persona files as one block, plus the names of any that are missing.

    A missing file is never papered over: callers must refuse to write, because
    a message drafted without the boundaries file is a message drafted without
    the rules."""
    directory = directory or PERSONA_DIR
    parts, missing = [], []
    for name in files:
        path = directory / f"{name}.md"
        try:
            parts.append(f"### persona/{name}.md\n{path.read_text().strip()}")
        except OSError:
            missing.append(name)
    return "\n\n".join(parts), missing


def compose_email(body: str, *, first_name: str, step: int, last_step: int, persona) -> str:
    """Greeting + body + sign-off.

    first_name may be a merge variable ("{{first_name}}") for template emails;
    it is rendered at staging. The closer ("Thanks,") goes on middle steps only:
    a first touch and a breakup end with just the signature.
    """
    parts: list[str] = []
    greeting = (persona.greeting or "").replace("{{first_name}}", first_name).strip()
    if greeting:
        parts.append(greeting)
    parts.append(body.strip())
    signature = (persona.signature or "").strip()
    if signature:
        middle = 1 < step < last_step
        closer = (persona.closer or "").strip() if middle else ""
        parts.append(f"{closer}\n{signature}" if closer else signature)
    return "\n\n".join(parts)
