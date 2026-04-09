"""Tests for the FAQ, legal page, and llms.txt content loaders."""

import os
import tempfile
import textwrap

import pytest


@pytest.fixture()
def faq_dir(tmp_path):
    """Create a minimal FAQ content directory with YAML index and markdown answers."""
    faq_yaml = tmp_path / "faq.yaml"
    faq_yaml.write_text(
        textwrap.dedent("""\
        sections:
          - title: "Getting Started"
            order: 1
            questions:
              - order: 1
                question: "What is a tenant?"
                answer: "q1.md"
              - order: 2
                question: "How do I connect?"
                answer: "q2.md"
          - title: "Billing"
            order: 2
            questions:
              - order: 1
                question: "How much does it cost?"
                answer: "q3.md"
    """)
    )
    (tmp_path / "q1.md").write_text("A tenant is a **Xero organisation**.")
    (tmp_path / "q2.md").write_text("Click **Login** in the navigation bar.")
    (tmp_path / "q3.md").write_text("Each PDF page costs 1 token.")
    return tmp_path


@pytest.fixture()
def legal_dir(tmp_path):
    """Create a minimal legal content directory with a markdown file."""
    (tmp_path / "privacy.md").write_text("# Privacy Policy\n\nWe collect **minimal** data.")
    return tmp_path


class TestLoadFaqs:
    """Tests for load_faqs()."""

    def test_returns_sections_sorted_by_order(self, faq_dir):
        from utils.content import load_faqs

        sections = load_faqs(faq_dir=str(faq_dir))

        assert len(sections) == 2
        assert sections[0]["title"] == "Getting Started"
        assert sections[1]["title"] == "Billing"

    def test_section_has_kebab_case_id(self, faq_dir):
        from utils.content import load_faqs

        sections = load_faqs(faq_dir=str(faq_dir))

        assert sections[0]["id"] == "getting-started"
        assert sections[1]["id"] == "billing"

    def test_questions_sorted_by_order(self, faq_dir):
        from utils.content import load_faqs

        sections = load_faqs(faq_dir=str(faq_dir))
        questions = sections[0]["questions"]

        assert len(questions) == 2
        assert questions[0]["question"] == "What is a tenant?"
        assert questions[1]["question"] == "How do I connect?"

    def test_markdown_converted_to_html(self, faq_dir):
        from utils.content import load_faqs

        sections = load_faqs(faq_dir=str(faq_dir))
        answer_html = sections[0]["questions"][0]["answer"]

        assert "<strong>Xero organisation</strong>" in answer_html

    def test_missing_yaml_raises(self, tmp_path):
        from utils.content import load_faqs

        with pytest.raises(FileNotFoundError):
            load_faqs(faq_dir=str(tmp_path))


class TestLoadLegalPage:
    """Tests for load_legal_page()."""

    def test_converts_markdown_to_html(self, legal_dir):
        from utils.content import load_legal_page

        html = load_legal_page("privacy.md", legal_dir=str(legal_dir))

        assert "<h1>Privacy Policy</h1>" in html
        assert "<strong>minimal</strong>" in html

    def test_missing_file_raises(self, legal_dir):
        from utils.content import load_legal_page

        with pytest.raises(FileNotFoundError):
            load_legal_page("nonexistent.md", legal_dir=str(legal_dir))


class TestLoadLlmsTxt:
    """Tests for load_llms_txt()."""

    def test_returns_raw_markdown(self, tmp_path):
        """Returns file content as-is, no HTML conversion."""
        from utils.content import load_llms_txt

        md_file = tmp_path / "llms.md"
        md_file.write_text("# Statement Processor\n\n> A summary.\n")

        result = load_llms_txt(content_dir=str(tmp_path))

        assert result == "# Statement Processor\n\n> A summary.\n"

    def test_missing_file_raises(self, tmp_path):
        """Raises FileNotFoundError when llms.md is missing."""
        from utils.content import load_llms_txt

        with pytest.raises(FileNotFoundError):
            load_llms_txt(content_dir=str(tmp_path))
