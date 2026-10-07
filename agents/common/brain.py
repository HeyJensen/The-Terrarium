"""Where the commerce bots get their words.

TemplateBrain (the default) fills fixed templates. It costs nothing and keeps
the whole pipeline testable, but its copy is placeholder quality: it gives a
human editor a structure, not finished writing.

A Claude-backed brain is the planned upgrade. It needs Nathan's OK on two
things first: a paid Anthropic API key and the `anthropic` Python package.
Until then, asking for it fails loudly instead of silently falling back.
"""


class TemplateBrain:
    name = "template"

    def listing_copy(self, niche: str, product: str, angle: str) -> dict:
        title = f"{angle.title()} {niche.title()} {product.title()}"
        return {
            "title": title[:140],
            "description": (
                f"{angle.capitalize()} {product} for anyone into {niche}.\n\n"
                "[EDIT: two or three lines on who it's for and why they'll love it.]"
            ),
        }

    def design_prompt(self, niche: str, angle: str) -> str:
        return (f"Original typographic t-shirt design about {niche}, {angle} tone, bold readable lettering, "
                "2-3 flat colors, transparent background, no logos, no brand names, no real people")

    def blog_outline(self, topic: str, keywords: list[str]) -> list[str]:
        kw = ", ".join(keywords[:3]) or topic
        return [
            f"Why {topic} is everywhere right now",
            f"What to look for ({kw})",
            "Our top picks [EDIT: real products you checked, with affiliate links]",
            "How to choose the right one for you",
            "FAQ",
        ]

    def social_post(self, platform: str, title: str, hook: str) -> str:
        return f"{hook} {title} [EDIT: platform voice for {platform}]"


class ClaudeBrain:
    name = "claude"

    def __init__(self, *_, **__):
        raise RuntimeError("The Claude brain isn't enabled. It needs Nathan's approval for a paid Anthropic "
                           "API key and the `anthropic` package (see docs/phase2-commerce-bots.md).")


def get_brain(name: str):
    return {"template": TemplateBrain, "claude": ClaudeBrain}[name]()
