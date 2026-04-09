"""Loaders for markdown-based content (FAQ, legal pages, llms.txt).

FAQ content uses a YAML index (faq.yaml) pointing to individual markdown
answer files.  Legal pages are standalone markdown files converted to HTML.
llms.txt content is a single markdown file served as raw text.
"""

import os

import markdown
import yaml

# Default directories resolved relative to the service root.
_SERVICE_DIR = os.path.dirname(os.path.dirname(__file__))
_CONTENT_DIR = os.path.join(_SERVICE_DIR, "content")
FAQ_DIR = os.path.join(_CONTENT_DIR, "faqs")
LEGAL_DIR = os.path.join(_CONTENT_DIR, "legal")


def load_faqs(faq_dir: str | None = None) -> list[dict]:
    """Load FAQ sections from YAML index and render markdown answers to HTML.

    Args:
        faq_dir: Override directory containing faq.yaml and answer files.
                 Defaults to ``service/content/faqs/``.

    Returns:
        List of section dicts sorted by order, each containing a list of
        question dicts with rendered HTML answers.
    """
    faq_dir = faq_dir or FAQ_DIR

    with open(os.path.join(faq_dir, "faq.yaml"), encoding="utf-8") as f:
        data = yaml.safe_load(f)

    sections: list[dict] = []
    for section in sorted(data["sections"], key=lambda s: s["order"]):
        questions: list[dict] = []
        for q in sorted(section["questions"], key=lambda q: q["order"]):
            md_path = os.path.join(faq_dir, q["answer"])
            with open(md_path, encoding="utf-8") as md_file:
                answer_html = markdown.markdown(md_file.read())
            questions.append({"order": q["order"], "question": q["question"], "answer": answer_html})
        sections.append({"title": section["title"], "id": section["title"].lower().strip().replace(" ", "-"), "order": section["order"], "questions": questions})
    return sections


def load_legal_page(filename: str, legal_dir: str | None = None) -> str:
    """Load a legal page markdown file and convert to HTML.

    Args:
        filename: Markdown filename (e.g. ``privacy.md``).
        legal_dir: Override directory. Defaults to ``service/content/legal/``.

    Returns:
        Rendered HTML string.
    """
    legal_dir = legal_dir or LEGAL_DIR
    md_path = os.path.join(legal_dir, filename)
    with open(md_path, encoding="utf-8") as f:
        return markdown.markdown(f.read())


def load_llms_txt(content_dir: str | None = None) -> str:
    """Load llms.txt markdown content as raw text (no HTML conversion).

    The llms.txt spec (llmstxt.org) defines a markdown file served at /llms.txt
    that gives LLMs a structured overview of a site. Raw markdown is returned
    rather than HTML because LLMs consume it directly.

    Args:
        content_dir: Override directory containing llms.md.
                     Defaults to ``service/content/``.

    Returns:
        Raw markdown string.
    """
    content_dir = content_dir or _CONTENT_DIR
    md_path = os.path.join(content_dir, "llms.md")
    with open(md_path, encoding="utf-8") as f:
        return f.read()
