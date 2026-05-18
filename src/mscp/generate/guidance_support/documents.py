# mscp/generate/documents.py
"""Guidance document rendering (AsciiDoc, PDF, HTML, Markdown) for mSCP.

Provides `generate_documents`, which renders a baseline through the main
Jinja template and optionally invokes AsciiDoctor to produce PDF and HTML
output.  `render_template` performs the actual Jinja render.  Helper Jinja
filters are also defined here: `group_ulify`, `group_ulify_md`,
`render_references`, `render_rules`, `render_rules_md`,
`replace_include_with_file_content`, `asciidoc_to_markdown`, and
`get_nested`.
"""

# Standard python modules
import base64
import gettext
import html as html_lib
import re
import sys
import tempfile
import time
from collections.abc import Mapping
from itertools import groupby
from pathlib import Path
from typing import Any, Sequence, Dict, List

# Additional python modules
from jinja2 import Environment, FileSystemLoader, Template
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound
from yaspin.core import Yaspin
from yaspin.spinners import Spinners

# Local python modules
from ...classes import Baseline, Macsecurityrule
from ...common_utils import (
    config,
    logger,
    mscp_data,
    open_file,
    run_command,
    NIX_OS,
)


def group_ulify(elements: list[str]) -> str:
    """
    Converts a list of strings into a grouped unordered list (UL) format.

    If the list contains the string "N/A", it returns "- N/A".
    Otherwise, it sorts the list, groups elements by their prefix (before the first parenthesis),
    and returns a string where each group is represented as a bullet point with its elements
    separated by commas.

    Args:
        elements (list[str]): The list of strings to be converted.

    Returns:
        str: A string representing the grouped unordered list.
    """
    if "N/A" in elements:
        return "- N/A"

    elements.sort()
    grouped = [list(i) for _, i in groupby(elements, lambda a: a.split("(")[0])]

    return "\n".join("- " + ", ".join(group) for group in grouped).strip()


def group_ulify_md(elements: list[str]) -> str:
    """Convert a list of strings to a grouped ``<br />``-separated Markdown bullet list.

    Like `group_ulify` but uses HTML ``<br />`` between groups for inline
    Markdown rendering in tables.

    Args:
        elements (list[str]): Strings to group and format.

    Returns:
        str: ``"- N/A"`` if ``"N/A"`` is in *elements*, otherwise a
            ``<br />``-joined grouped bullet string.
    """
    if "N/A" in elements:
        return "- N/A"

    elements.sort()
    grouped = [list(i) for _, i in groupby(elements, lambda a: a.split("(")[0])]

    return "<br />".join("- " + ", ".join(group) for group in grouped).strip()


def group_ulify_html(elements: list[str]) -> str:
    if "N/A" in elements:
        return "<ul><li>N/A</li></ul>"
    elements.sort()
    grouped = [list(i) for _, i in groupby(elements, lambda a: a.split("(")[0])]
    items = "".join(f"<li>{', '.join(group)}</li>" for group in grouped)
    return f"<ul>{items}</ul>"


def render_rules_html(rule_set: list[str]) -> str:
    items = "".join(f"<li>{rule}</li>" for rule in rule_set)
    return f"<ul>{items}</ul>"


def extract_from_title(title: str) -> str:
    """Extract the text inside the first parenthesised group in *title*.

    Args:
        title (str): String that may contain a ``(…)`` group.

    Returns:
        str: The content inside the first ``(…)``, or ``""`` if not found.
    """
    return (
        match.group()
        if (match := re.search(r"(?<=\()(.*?)(?=\s*\))", title, re.IGNORECASE))
        else ""
    )


def render_references(reference_set: Sequence[Dict[str, Any]]) -> str:
    """Convert a sequence of dicts into AsciiDoc table rows (no header, no ``|===``).

    Args:
        reference_set (Sequence[Dict[str, Any]]): Dicts to render; list values
            are joined with ``"\\n- "``.

    Returns:
        str: Newline-separated AsciiDoc cell rows, or ``""`` if *reference_set* is empty.

    Raises:
        TypeError: If any element of *reference_set* is not a dict.
    """

    def _escape_cell(text: Any) -> str:
        s = str(text)
        return s.replace("|", r"\|")

    rows: List[List[str]] = []

    def _walk(path: List[str], value: Any) -> None:
        if isinstance(value, (list, tuple)):
            # Join list elements; str() for non-scalar reference_set
            joined = "\n- ".join(map(str, value))
            rows.append(path + [_escape_cell(joined)])
        else:
            rows.append(path + [_escape_cell(value)])

    # Validate and traverse each input dict
    for d in reference_set:
        if not isinstance(d, dict):
            raise TypeError("All elements of 'reference_set' must be dictionaries.")
        for k in d.keys():
            _walk([str(k)], d[k])

    if not rows:
        return ""  # nothing to emit

    # Determine deepest path and pad each row to keep a rectangular table
    max_cols = max(len(r) for r in rows)
    padded = [r + [""] * (max_cols - len(r)) for r in rows]

    # Assemble rows (each line starts with '| ')
    return "\n".join("!" + "\n!\n- ".join(r) for r in padded)


def render_rules(rule_set: list[str]) -> str:
    """Render a list of rule strings as newline-separated ``"- <rule>"`` lines.

    Args:
        rule_set (list[str]): Rule strings to render.

    Returns:
        str: Newline-joined bullet lines.
    """
    return "\n".join(f"- {rule}" for rule in rule_set)


def render_rules_md(rule_set: list[str]) -> str:
    """Render a list of rule strings as ``<br>``-joined ``"- <rule>"`` lines for Markdown.

    Args:
        rule_set (list[str]): Rule strings to render.

    Returns:
        str: ``<br>``-joined bullet lines.
    """
    return "<br>".join(f"- {rule}" for rule in rule_set)


def replace_include_with_file_content(text: str) -> str:
    """Replace AsciiDoc ``include::`` directives with the content of the referenced file.

    Files are resolved relative to the configured ``includes_dir``.  Missing
    files are logged and replaced with an HTML comment placeholder.

    Args:
        text (str): AsciiDoc source that may contain ``include::<path>[]`` directives.

    Returns:
        str: Source with all ``include::`` directives replaced by file contents.
    """
    includes_dir: Path = Path(config["includes_dir"]).absolute()
    # Regular expression to match `include::` directives and extract filenames
    pattern = re.compile(r"include::(?:.*/)?([^/]+)\[\]")

    # Function to replace matched blocks with file content
    def replace_block(match):
        filename = match.group(1).strip()
        file_path = includes_dir / filename
        try:
            file_content = file_path.read_text()
            return file_content
        except FileNotFoundError:
            logger.error(f"File not found: {file_path}")
            return f"<!-- File not found: {file_path} -->"

    # Replace all `include::` blocks in the text
    return pattern.sub(replace_block, text)


def asciidoc_to_markdown(value: str) -> str:
    """Convert a subset of AsciiDoc syntax to GitHub-flavoured Markdown.

    Handles headers, NOTE/IMPORTANT admonitions, source code blocks,
    tables (``|===``), unordered/ordered lists, block titles, and
    ``link:url[text]`` macros.  Unsupported constructs are passed through
    with links replaced and trailing whitespace stripped.

    Args:
        value (str): AsciiDoc source text.

    Returns:
        str: Markdown-formatted text.
    """
    lines = value.splitlines()
    result = []
    i = 0

    link_pattern = re.compile(r"link:(\S+)\[(.*?)\]")

    def link_replacer(match):
        url, text = match.group(1), match.group(2)
        return f"[{text if text else url}]({url})"

    while i < len(lines):
        line = lines[i].rstrip()

        # Header: == -> ##, === -> ###, etc.
        if re.match(r"^(=+)\s+.+", line):
            level, content = re.match(r"^(=+)\s+(.+)", line).groups()
            result.append(f"{'#' * len(level)} {content}")

        # NOTE block
        elif line.startswith("NOTE:"):
            result.append(
                f"> **NOTE:** {link_pattern.sub(link_replacer, line[5:].strip())}"
            )

        # [IMPORTANT] block
        elif (
            line.strip() == "[IMPORTANT]"
            and i + 1 < len(lines)
            and lines[i + 1].strip() == "===="
        ):
            i += 2
            important_lines = []
            while i < len(lines) and lines[i].strip() != "====":
                important_lines.append(lines[i].strip())
                i += 1
            result.append("> **IMPORTANT:** " + " ".join(important_lines))

        # [source] blocks
        elif line.startswith("[source"):
            language = ""
            # Extract just the language before the first comma
            lang_match = re.match(r"\[source\s*,?\s*([a-zA-Z0-9_+-]+)?", line)
            if lang_match:
                language = lang_match.group(1) or ""

            if i + 1 < len(lines) and lines[i + 1].strip() in ("----", "...."):
                fence = lines[i + 1].strip()
                i += 2
                code_lines = []
                while i < len(lines) and lines[i].strip() != fence:
                    code_lines.append(lines[i])
                    i += 1

                result.append(f"```{language}".strip())
                result.extend(code_lines)
                result.append("```")

        # Code block without [source]
        elif line.strip() in ("----", "...."):
            fence = line.strip()
            i += 1
            code_lines = []
            while i < len(lines) and lines[i].strip() != fence:
                code_lines.append(lines[i])
                i += 1
            result.append("```")
            result.extend(code_lines)
            result.append("```")

        # Table with |===
        elif line.strip() == "|===":
            i += 1
            table_rows = []
            while i < len(lines) and lines[i].strip() != "|===":
                table_line = lines[i].strip()
                if table_line.startswith("|"):
                    cells = [cell.strip() for cell in table_line.lstrip("|").split("|")]
                    table_rows.append(cells)
                i += 1

            if table_rows:
                header = "| " + " | ".join(table_rows[0]) + " |"
                separator = "| " + " | ".join(["---"] * len(table_rows[0])) + " |"
                result.append(header)
                result.append(separator)
                for row in table_rows[1:]:
                    result.append("| " + " | ".join(row) + " |")

        # Skip AsciiDoc block attributes like [cols=...], [width=...], [options=...], etc.
        elif re.match(
            r"^\[(cols|width|options|grid|frame|stripes|halign|valign|%|role|.*)=.*\]$",
            line,
        ):
            pass

        # Handle AsciiDoc block titles like `.Some Title`
        elif re.match(r"^\.(?!\d+\s)(.+)$", line):
            block_title = re.match(r"^\.(.+)$", line).group(1).strip()
            result.append(f"**{block_title}**")

        # Unordered List (* -> -)
        elif line.strip().startswith("* "):
            result.append("- " + line.strip()[2:])

        # Ordered List (. or 1. 2. etc.)
        elif re.match(r"^\.\s+.+", line):
            result.append("1. " + line.strip()[2:])
        elif re.match(r"^\d+\.\s+.+", line):
            result.append(line.strip())

        else:
            result.append(link_pattern.sub(link_replacer, line.strip()))

        i += 1

    return "\n".join(result)


def to_anchor_id(text: str) -> str:
    """Convert a heading string to an asciidoctor-style anchor ID."""
    s = text.lower()
    s = re.sub(r"[^a-z0-9\s_-]", "", s)
    s = re.sub(r"[-\s]+", "_", s.strip())
    return f"_{s}"


def apply_rouge_highlighting(html: str) -> str:
    """Highlight all <code data-lang> blocks using Pygments."""
    pattern = re.compile(r'(<code data-lang="([^"]+)">)(.*?)(</code>)', re.DOTALL)
    formatter = HtmlFormatter(nowrap=True)

    def replace_fn(m: re.Match) -> str:
        lang = m.group(2)
        code = html_lib.unescape(m.group(3))
        try:
            lexer = get_lexer_by_name(lang, stripnl=False)
        except ClassNotFound:
            return m.group(0)
        return f'{m.group(1)}{highlight(code, lexer, formatter)}{m.group(4)}'

    return pattern.sub(replace_fn, html)


def asciidoc_to_html(value: str) -> str:
    """Convert AsciiDoc markup to HTML using asciidoctor-compatible CSS class names."""
    if not value:
        return ""

    lines = value.splitlines()
    result = []
    i = 0

    link_pattern = re.compile(r"link:(\S+)\[(.*?)\]")
    bold_pattern = re.compile(r"\*\*(.+?)\*\*")
    italic_pattern = re.compile(r"(?<!\w)_(.+?)_(?!\w)")
    inline_code = re.compile(r"`([^`]+)`")

    def process_inline(text: str) -> str:
        text = link_pattern.sub(lambda m: f'<a href="{m.group(1)}">{m.group(2) or m.group(1)}</a>', text)
        text = bold_pattern.sub(r"<strong>\1</strong>", text)
        text = italic_pattern.sub(r"<em>\1</em>", text)
        text = inline_code.sub(r"<code>\1</code>", text)
        return text

    while i < len(lines):
        line = lines[i].rstrip()

        if re.match(r"^(=+)\s+.+", line):
            level, content = re.match(r"^(=+)\s+(.+)", line).groups()
            hlevel = min(len(level) + 1, 6)
            anchor = to_anchor_id(content)
            result.append(f'<h{hlevel} id="{anchor}">{process_inline(content)}</h{hlevel}>')

        elif line.startswith("NOTE:"):
            result.append(
                '<div class="admonitionblock note"><table><tr>'
                '<td class="icon"><i class="fa icon-note" title="Note"></i></td>'
                f'<td class="content">{process_inline(line[5:].strip())}</td>'
                "</tr></table></div>"
            )

        elif line.startswith("IMPORTANT:"):
            result.append(
                '<div class="admonitionblock important"><table><tr>'
                '<td class="icon"><i class="fa icon-important" title="Important"></i></td>'
                f'<td class="content">{process_inline(line[10:].strip())}</td>'
                "</tr></table></div>"
            )

        elif (
            line.strip() == "[IMPORTANT]"
            and i + 1 < len(lines)
            and lines[i + 1].strip() == "===="
        ):
            i += 2
            important_lines = []
            while i < len(lines) and lines[i].strip() != "====":
                important_lines.append(lines[i].strip())
                i += 1
            result.append(
                '<div class="admonitionblock important"><table><tr>'
                '<td class="icon"><i class="fa icon-important" title="Important"></i></td>'
                f'<td class="content">{process_inline(" ".join(important_lines))}</td>'
                "</tr></table></div>"
            )

        elif line.startswith("[source"):
            lang_match = re.match(r"\[source\s*,?\s*([a-zA-Z0-9_+-]+)?", line)
            lang = lang_match.group(1) or "" if lang_match else ""
            if i + 1 < len(lines) and lines[i + 1].strip() in ("----", "...."):
                fence = lines[i + 1].strip()
                i += 2
                code_lines = []
                while i < len(lines) and lines[i].strip() != fence:
                    code_lines.append(lines[i])
                    i += 1
                data_lang = f' data-lang="{lang}"' if lang else ""
                code_content = "\n".join(code_lines)
                result.append(
                    '<div class="listingblock"><div class="content">'
                    f'<pre class="rouge highlight nowrap"><code{data_lang}>{code_content}</code></pre>'
                    "</div></div>"
                )

        elif line.strip() in ("----", "...."):
            fence = line.strip()
            i += 1
            code_lines = []
            while i < len(lines) and lines[i].strip() != fence:
                code_lines.append(lines[i])
                i += 1
            result.append(
                '<div class="listingblock"><div class="content">'
                f'<pre class="rouge highlight nowrap"><code>{"<br>".join(code_lines)}</code></pre>'
                "</div></div>"
            )

        elif line.strip() == "|===":
            i += 1
            table_rows = []
            while i < len(lines) and lines[i].strip() != "|===":
                table_line = lines[i].strip()
                if table_line.startswith("|"):
                    cells = [cell.strip() for cell in table_line.lstrip("|").split("|")]
                    table_rows.append(cells)
                i += 1
            if table_rows:
                rows_html = []
                for j, row in enumerate(table_rows):
                    tag = "th" if j == 0 else "td"
                    cells_html = "".join(
                        f'<{tag} class="tableblock halign-left valign-top">'
                        f'<div class="content"><div class="paragraph"><p>{process_inline(c)}</p></div></div>'
                        f"</{tag}>"
                        for c in row
                    )
                    rows_html.append(f"<tr>{cells_html}</tr>")
                result.append(
                    '<table class="tableblock frame-all grid-all stretch">'
                    f'{"".join(rows_html)}</table>'
                )

        elif re.match(
            r"^\[(cols|width|options|grid|frame|stripes|halign|valign|%|role|.*)=.*\]$",
            line,
        ):
            pass

        elif re.match(r"^\.(?!\d+\s)(.+)$", line):
            block_title = re.match(r"^\.(.+)$", line).group(1).strip()
            result.append(
                f'<div class="title"><strong>{process_inline(block_title)}</strong></div>'
            )

        elif line.strip().startswith("* "):
            result.append(
                f'<div class="ulist"><ul><li><p>{process_inline(line.strip()[2:])}</p></li></ul></div>'
            )

        elif re.match(r"^\.\s+.+", line):
            result.append(
                f'<div class="olist arabic"><ol class="arabic"><li><p>{process_inline(line.strip()[2:])}</p></li></ol></div>'
            )

        elif re.match(r"^\d+\.\s+.+", line):
            result.append(
                f'<div class="olist arabic"><ol class="arabic"><li><p>{process_inline(line.strip())}</p></li></ol></div>'
            )

        elif not line.strip():
            result.append("")

        else:
            result.append(
                f'<div class="paragraph"><p>{process_inline(line.strip())}</p></div>'
            )

        i += 1

    return "\n".join(result)


def get_nested(
    obj: Mapping[str, Any] | list, keys: list[str | int], default: Any = None
) -> Any:
    """Safely traverse a nested mapping / list using a sequence of keys or indices.

    Args:
        obj (Mapping | list): Root object to traverse.
        keys (list[str | int]): Ordered path of dict keys or list indices.
        default: Value returned when any key/index is missing or the wrong type.

    Returns:
        Any: The value at the nested path, or *default* if unreachable.
    """
    current = obj
    for key in keys:
        if isinstance(current, Mapping) and isinstance(key, str):
            current = current.get(key, default)
        elif isinstance(current, list) and isinstance(key, int):
            if 0 <= key < len(current):
                current = current[key]
            else:
                return default
        else:
            return default
    return current


def render_template(
    output_file: Path,
    template_name: str,
    baseline: Baseline,
    b64logo: bytes,
    pdf_theme: str,
    html_css: str,
    logo_path: Path,
    os_name: str,
    version_info: dict[str, Any],
    show_all_tags: bool,
    custom: bool,
    template_dir: str,
    themes_dir: str,
    logo_dir: str,
    output_format: str = "adoc",
    language: str = "en",
) -> None:
    """Render a Jinja template against *baseline* data and write to *output_file*.

    Configures a Jinja ``Environment`` with all mSCP filters, installs
    gettext translations for *language*, renders the template, and writes
    the result as text.

    Args:
        output_file (Path): Destination for the rendered output.
        template_name (str): Filename of the template within *template_dir*.
        baseline (Baseline): Baseline data model.
        b64logo (bytes): Base64-encoded logo image bytes.
        pdf_theme (str): AsciiDoctor-PDF theme filename.
        html_css (str): CSS filename for HTML output.
        logo_path (Path): Absolute path to the logo file.
        os_name (str): Operating system name string.
        version_info (dict[str, Any]): OS/compliance version metadata.
        show_all_tags (bool): Whether to render all tags in the document.
        custom (bool): Whether the baseline uses a custom configuration.
        template_dir (str): Path to the Jinja templates directory.
        themes_dir (str): Path to the themes/styles directory.
        logo_dir (str): Path to the images directory.
        output_format (str): ``"adoc"`` (default) or ``"markdown"``.
        language (str): BCP-47 language code for gettext lookup. Defaults to ``"en"``.
    """
    translations = gettext.translation(
        domain="messages",
        localedir=config["locales_dir"],
        languages=[language],
        fallback=True,
    )

    env: Environment = Environment(
        loader=FileSystemLoader(template_dir),
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        extensions=["jinja2.ext.i18n"],
        keep_trailing_newline=True,
    )

    styles_dir: Path = Path(themes_dir).absolute()
    images_dir: Path = Path(logo_dir).absolute()
    acronyms_file: Path = Path(config["includes_dir"], "acronyms.yaml").absolute()

    env.filters["group_ulify"] = group_ulify
    env.filters["include_replace"] = replace_include_with_file_content
    env.filters["render_rules"] = render_rules
    env.filters["render_references"] = render_references
    env.filters["get_nested"] = get_nested
    env.filters["mobileconfig_payloads_to_xml"] = (
        Macsecurityrule.mobileconfig_info_to_xml
    )
    env.filters["to_anchor_id"] = to_anchor_id
    env.filters["asciidoc_to_html"] = asciidoc_to_html
    env.install_gettext_translations(translations)

    if output_format == "markdown":
        env.filters["group_ulify"] = group_ulify_md
        env.filters["render_rules"] = render_rules_md
        env.filters["asciidoc_to_markdown"] = asciidoc_to_markdown

    if output_format == "html":
        env.filters["group_ulify"] = group_ulify_md
        env.filters["render_rules"] = render_rules_md

    if output_format == "pdf":
        env.filters["group_ulify"] = group_ulify_html
        env.filters["render_rules"] = render_rules_html

        def _highlight_code(code: str, lang: str = "bash") -> str:
            try:
                lexer = get_lexer_by_name(lang, stripall=False)
            except ClassNotFound:
                return html_lib.escape(code)
            fmt = HtmlFormatter(nowrap=True, noclasses=True, style="friendly")
            return highlight(code, lexer, fmt).rstrip()

        env.filters["highlight_code"] = _highlight_code

    css_content: str = ""
    pygments_css: str = ""
    if output_format == "html":
        css_file = Path(themes_dir) / html_css
        if css_file.exists():
            css_content = css_file.read_text()
        pygments_css = HtmlFormatter(style="default").get_style_defs(
            "pre.rouge.highlight code"
        )

    template: Template = env.get_template(template_name)

    baseline_dict: dict[str, Any] = baseline.model_dump()
    acronyms_data: dict[str, Any] = open_file(acronyms_file, language)

    html_title, html_subtitle = map(str.strip, baseline.title.split(":", 1))
    document_subtitle2: str = ":document-subtitle2:"

    if "Tailored from" in baseline.title:
        html_subtitle: str = html_subtitle.split("(")[0]
        html_subtitle2: str = extract_from_title(baseline.title)
        document_subtitle2: str = f"{document_subtitle2} {html_subtitle2}"
        baseline_dict["tailored"] = True
    else:
        benchmark = baseline.title.split()[-1]
        benchmarks = mscp_data.get("benchmarks", "")
        benchmark_description = next(
            (d["description"] for d in benchmarks if d.get("keyword") == benchmark),
            benchmark,
        )
        baseline_dict["tailored"] = False
        baseline_dict["benchmark_description"] = benchmark_description

    rendered_output: str = template.render(
        baseline=baseline_dict,
        html_title=html_title,
        html_subtitle=html_subtitle,
        document_subtitle2=document_subtitle2,
        styles_dir=styles_dir,
        images_dir=images_dir,
        logo=logo_path.name,
        pdflogo=b64logo.decode("ascii"),
        pdf_theme=pdf_theme,
        html_css=html_css,
        css_content=css_content,
        pygments_css=pygments_css,
        show_all_tags=show_all_tags,
        os_name=os_name.strip().lower(),
        os_version=str(version_info.get("os_version", None)),
        version=version_info.get("compliance_version", None),
        release_date=version_info.get("date", None),
        custom=custom,
        format=output_format,
        language=language,
        acronyms=acronyms_data.get("acronyms", []),
        terminology=acronyms_data.get("terminology", []),
        NIX_OS=NIX_OS,
    )

    if output_format == "html":
        rendered_output = apply_rouge_highlighting(rendered_output)

    if output_format == "pdf":
        rendered_output = re.sub(r'<div class="paragraph">(.*?)</div>', r'\1', rendered_output, flags=re.DOTALL)
        # Convert admonitionblock table structure → simple styled divs
        rendered_output = re.sub(
            r'<div class="admonitionblock note"><table><tr><td class="icon">.*?</td><td class="content">(.*?)</td></tr></table></div>',
            r'<div class="admonition-wrap-note"><div class="admonition-note"><strong>NOTE:</strong> \1</div></div>',
            rendered_output, flags=re.DOTALL
        )
        rendered_output = re.sub(
            r'<div class="admonitionblock important"><table><tr><td class="icon">.*?</td><td class="content">(.*?)</td></tr></table></div>',
            r'<div class="admonition-wrap-important"><div class="admonition-important"><strong>IMPORTANT:</strong> \1</div></div>',
            rendered_output, flags=re.DOTALL
        )
        rendered_output = apply_rouge_highlighting(rendered_output)

    output_file.write_text(rendered_output)


PDF_CSS = """
body        { font-family: notoserif, serif; font-size: 10.5pt; color: #333333;
              margin: 0; padding: 0; text-align: justify; }
div         { text-align: left; }
h2          { font-family: notoserif, serif; font-size: 22pt; color: #333333; margin-top: 0;
              margin-bottom: 12pt; font-weight: bold; text-align: left; }
h3          { font-family: notoserif, serif; font-size: 18pt; color: #333333; margin-top: 16pt;
              margin-bottom: 8pt; font-weight: bold; text-align: left;
              page-break-inside: avoid; break-inside: avoid; }
h4          { font-family: notoserif, serif; font-size: 10.5pt; color: #333333; margin-top: 10pt;
              margin-bottom: 4pt; font-weight: bold; }
p           { line-height: 1.5; margin: 12pt 0; text-align: justify; }
a           { color: #1f618d; }
ul          { margin: 4pt 0 4pt 18pt; padding: 0; }
ol          { margin: 4pt 0 4pt 18pt; padding: 0; }
li          { margin: 2pt 0; line-height: 1.4; text-align: left; }
strong      { font-weight: bold; }
em          { font-style: italic; }
pre         { font-family: mplus1mn, monospace; font-size: 9pt;
              padding: 6pt 10pt; margin: 8pt 0;
              white-space: pre-wrap; word-wrap: break-word; line-height: 1.4;
              page-break-inside: avoid; break-inside: avoid; }
code        { font-family: mplus1mn, monospace; font-size: 9pt; }
table       { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 10.5pt; }
th          { font-weight: bold; text-align: left; padding: 3pt 6pt;
              border: 1px solid #bbbbbb; vertical-align: top; }
td          { border: 1px solid #cccccc; padding: 3pt 6pt; vertical-align: top; text-align: left; }
td p, th p, p.tableblock { margin: 0; line-height: 1.5; text-align: left; }
img         { max-width: 100%; }
.title      { font-style: italic; font-size: 10pt; margin: 8pt 0 3pt 0; text-align: left; }
.sect1      { page-break-before: always; }
.toc        { margin: 0 0 14pt 0; }
.toc li     { list-style-type: none; font-size: 10.5pt; line-height: 1.6; }
.toc ul li  { list-style-type: none; padding-left: 0; }
.toc a      { color: #333333; text-decoration: none; }
.toc-heading { font-size: 22pt; font-weight: bold; margin-bottom: 14pt; margin-top: 0; }
.admonition-wrap-note      { margin: 10pt 0; }
.admonition-wrap-important { margin: 10pt 0; }
.admonition-note      { padding: 2pt 0 2pt 14pt; font-size: 10.5pt; }
.admonition-important { padding: 2pt 0 2pt 14pt; font-size: 10.5pt; }
.note       { padding: 2pt 0; margin: 10pt 0; font-size: 10.5pt; }
.remediation-title  { font-weight: bold; margin: 12pt 0 6pt 0; font-size: 10.5pt; }
.refs               { margin-top: 16pt; break-inside: avoid; page-break-inside: avoid; }
.ref-label          { font-weight: bold; padding: 3pt 8pt; vertical-align: top; }
.refs th, .refs td  { border: none; }
.refs td ul         { margin: 0; padding-left: 14pt; }
.refs td li         { margin: 0; line-height: 1.5; }
"""

# Pre-compiled patterns used by PDF detection functions (avoid recompilation per call)
_RE_RULE_NUM = re.compile(r"^\d+\.\d+\.")
_RE_H3_FIND  = re.compile(r'<h3[^>]*id="([^"]+)"')

# Module-level font bytes cache: populated once, reused across multiple PDF generations
_FONT_BYTES_CACHE: dict[str, bytes] = {}


def _story_render_no_links(story: Any, rectfn: Any) -> Any:
    """Render a Story to a Document without inserting PDF GoTo links.
    Used for intermediate detection passes — link insertion accounts for ~80% of
    write_with_links time on documents with many anchors, so skipping it makes
    each non-final pass ~8x faster.
    """
    import io as _io
    import pymupdf as _pymupdf
    stream = _io.BytesIO()
    writer = _pymupdf.DocumentWriter(stream)
    story.write(writer, rectfn)
    writer.close()
    stream.seek(0)
    return _pymupdf.open(stream=stream.read(), filetype="pdf")

def _detect_split_refs_in_pdf(
    doc: Any,
    body_rect: Any,
    html_content: str,
    page_blocks: list | None = None,
) -> set[str]:
    """
    Detect rules whose <table class="refs"> is split across a page boundary.
    Returns rule_ids needing page-break-before on the refs table.
    """
    if page_blocks is None:
        page_blocks = [pg.get_text("dict")["blocks"] for pg in doc]

    REF_LABELS = {
        "ID", "800-53r5", "800-171r3", "CCE", "SFR", "DISA STIG(s)",
        "CIS Benchmark", "CIS Controls V8", "indigo", "CMMC", "TAGS",
    }
    # Refs table label column spans from body left to ~body.x0 + 80
    COL_X0 = body_rect.x0 - 10
    COL_X1 = body_rect.x0 + 90
    SPLIT_BOT = body_rect.y1 - 100  # last ref row within 100pt of bottom
    SPLIT_TOP = body_rect.y0 + 60   # first continuation row within 60pt of top
    H3_MIN = 17.0

    splits: set[str] = set()

    for pno in range(len(doc) - 1):
        # Find the last ref-label block on this page.
        # PyMuPDF merges <th> and <td> into one block spanning the full row width,
        # so we check only the left edge (bx0) and match by first word.
        last_ref_blk = None
        last_ref_label = None
        for b in page_blocks[pno]:
            if "lines" not in b:
                continue
            bx0, by0, bx1, by1 = b["bbox"]
            if bx0 < COL_X0 or bx0 > COL_X1:
                continue
            spans = [s for ln in b["lines"] for s in ln.get("spans", [])]
            txt = " ".join(s.get("text", "") for s in spans).strip()
            first_word = txt.split()[0] if txt else ""
            if first_word in REF_LABELS:
                if last_ref_blk is None or by1 > last_ref_blk["bbox"][3]:
                    last_ref_blk = b
                    last_ref_label = first_word

        if last_ref_blk is None or last_ref_blk["bbox"][3] < SPLIT_BOT:
            continue

        # Find the first ref-label block on the next page
        first_ref_next = None
        for b in page_blocks[pno + 1]:
            if "lines" not in b:
                continue
            bx0, by0, bx1, by1 = b["bbox"]
            if bx0 < COL_X0 or bx0 > COL_X1:
                continue
            spans = [s for ln in b["lines"] for s in ln.get("spans", [])]
            txt = " ".join(s.get("text", "") for s in spans).strip()
            first_word = txt.split()[0] if txt else ""
            if first_word in REF_LABELS:
                first_ref_next = (first_word, by0)
                break

        if first_ref_next is None or first_ref_next[1] > SPLIT_TOP:
            continue

        # Filter: if the first ref label on the next page is "ID" it's a NEW table, not a split
        if first_ref_next[0] == "ID":
            continue

        # Only fix the most egregious splits: those where just the "ID" row is on the
        # previous page (every other row overflowed).  Less severe splits create too much
        # cascading layout churn when page-breaks are injected.
        if last_ref_label != "ID":
            continue

        # Confirmed split — find the preceding rule h3
        ref_y0 = last_ref_blk["bbox"][1]
        rule_h3_txt: str | None = None
        for search_pno in range(pno, max(-1, pno - 10), -1):
            best_h3_txt: str | None = None
            best_h3_y0 = -1.0
            for pb in page_blocks[search_pno]:
                if "lines" not in pb:
                    continue
                pb_y0 = pb["bbox"][1]
                if search_pno == pno and pb_y0 >= ref_y0:
                    continue
                pb_spans = [s for ln in pb["lines"] for s in ln.get("spans", [])]
                pb_txt = " ".join(s.get("text", "") for s in pb_spans).strip()
                if (any(s.get("size", 0) >= H3_MIN for s in pb_spans)
                        and _RE_RULE_NUM.match(pb_txt)
                        and pb_y0 > best_h3_y0):
                    best_h3_txt = pb_txt
                    best_h3_y0 = pb_y0
            if best_h3_txt:
                rule_h3_txt = best_h3_txt
                break

        if not rule_h3_txt:
            continue

        escaped = re.escape(rule_h3_txt[:30])
        hm = re.search(r'<h3[^>]*id="([^"]+)"[^>]*>[^<]*?' + escaped, html_content)
        if hm:
            splits.add(hm.group(1))

    return splits


def _inject_refs_page_break(html: str, rule_id: str) -> str:
    """
    Wrap <table class="refs">...</table> in a div with page-break-before: always.
    PyMuPDF doesn't respect page-break-before on table elements, but it does on divs.
    """
    m = re.search(r'<h3[^>]*id="' + re.escape(rule_id) + r'"', html)
    if not m:
        return html

    rule_start = m.start()
    table_m = re.search(r'<table class="refs"', html[rule_start:])
    if not table_m:
        return html

    table_start = rule_start + table_m.start()

    # Skip if already injected
    if 'page-break-before: always' in html[max(0, table_start - 80):table_start]:
        return html

    # Find the matching </table>
    table_end_m = re.search(r'</table>', html[table_start:])
    if not table_end_m:
        return html

    table_end = table_start + table_end_m.end()

    wrapped = (
        '<div style="page-break-before: always">'
        + html[table_start:table_end]
        + '</div>'
    )
    return html[:table_start] + wrapped + html[table_end:]


def _remove_refs_page_break(html: str, rule_id: str) -> str:
    """Unwrap the page-break-before: always div from a rule's refs table."""
    m = re.search(r'<h3[^>]*id="' + re.escape(rule_id) + r'"', html)
    if not m:
        return html

    rule_start = m.start()
    wrapper_m = re.search(
        r'<div style="page-break-before: always">(<table class="refs".*?</table>)</div>',
        html[rule_start:],
        re.DOTALL,
    )
    if not wrapper_m:
        return html

    abs_start = rule_start + wrapper_m.start()
    abs_end = rule_start + wrapper_m.end()
    return html[:abs_start] + wrapper_m.group(1) + html[abs_end:]


def _find_spurious_refs_injections(
    doc: Any,
    body_rect: Any,
    html_content: str,
    page_blocks: list | None = None,
) -> set[str]:
    """
    After layout convergence, find rule_ids whose refs page-break injection turned
    out to be unnecessary: the refs table is at the top of a page but would fit in
    the space available at the bottom of the preceding page.

    This fixes cases where refs were injected in the initial (unstable) layout but
    the injection became spurious once cascading h3/pre fixes repositioned the rule.
    """
    if page_blocks is None:
        page_blocks = [pg.get_text("dict")["blocks"] for pg in doc]

    REF_LABELS = {
        "ID", "800-53r5", "800-171r3", "CCE", "SFR", "DISA STIG(s)",
        "CIS Benchmark", "CIS Controls V8", "indigo", "CMMC", "TAGS",
    }
    COL_X0 = body_rect.x0 - 10
    COL_X1 = body_rect.x0 + 90
    H3_MIN = 17.0

    # Build the set of rule_ids that currently have a refs injection in the HTML
    injected_ids: set[str] = set()
    search_str = '<div style="page-break-before: always"><table class="refs"'
    pos = 0
    while True:
        idx = html_content.find(search_str, pos)
        if idx == -1:
            break
        h3_matches = list(_RE_H3_FIND.finditer(html_content[:idx]))
        if h3_matches:
            injected_ids.add(h3_matches[-1].group(1))
        pos = idx + len(search_str)

    if not injected_ids:
        return set()

    to_remove: set[str] = set()

    for pno in range(1, len(doc)):
        first_ref_label: str | None = None
        first_ref_y0: float | None = None
        last_ref_y1: float = 0.0
        first_h3_y0: float | None = None
        id_row_text: str = ""
        prev_ref_y1: float = 0.0

        # Sort blocks top-to-bottom so we can detect the end of a contiguous table.
        all_blocks = [b for b in page_blocks[pno] if "lines" in b]
        all_blocks.sort(key=lambda b: b["bbox"][1])

        for b in all_blocks:
            bx0, by0, bx1, by1 = b["bbox"]
            spans = [s for ln in b["lines"] for s in ln.get("spans", [])]
            txt = " ".join(s.get("text", "") for s in spans).strip()

            # Detect rule headings (h3)
            if any(s.get("size", 0) >= H3_MIN for s in spans) and _RE_RULE_NUM.match(txt):
                if first_h3_y0 is None:
                    first_h3_y0 = by0
                # An h3 after the first ref row ends the current table.
                if first_ref_y0 is not None:
                    break

            # Detect ref-label blocks (left column)
            if COL_X0 <= bx0 <= COL_X1:
                first_word = txt.split()[0] if txt else ""
                if first_word in REF_LABELS:
                    if first_ref_y0 is None:
                        first_ref_y0 = by0
                        first_ref_label = first_word
                        id_row_text = txt
                        prev_ref_y1 = by1
                        last_ref_y1 = by1
                    elif by0 - prev_ref_y1 < 30:
                        # Contiguous with the previous row — still same table.
                        last_ref_y1 = by1
                        prev_ref_y1 = by1
                    else:
                        # Large gap → this is a different rule's table; stop.
                        break

        # Only pages whose first content is an "ID" ref row (entire refs table pushed here)
        if first_ref_label != "ID" or first_ref_y0 is None:
            continue
        # If an h3 appears before the refs, this is a normal rule start — skip
        if first_h3_y0 is not None and first_h3_y0 < first_ref_y0:
            continue

        # Extract rule_id from the merged "ID <rule_id> [severity]" block
        parts = id_row_text.split()
        if len(parts) < 2:
            continue
        candidate = parts[1]
        if candidate not in injected_ids:
            continue

        refs_height = last_ref_y1 - first_ref_y0

        # Measure available space at the bottom of the preceding page
        last_y1 = body_rect.y0
        for b in page_blocks[pno - 1]:
            if "lines" not in b:
                continue
            by1 = b["bbox"][3]
            if body_rect.y0 <= by1 <= body_rect.y1:
                last_y1 = max(last_y1, by1)

        available = body_rect.y1 - last_y1

        # 40pt accounts for the pre-table margin-top (16pt) + row spacing not
        # captured in the refs_height measurement from the injected layout.
        if refs_height + 40 <= available:
            to_remove.add(candidate)

    return to_remove



def generate_pdf_with_pymupdf(
    pdf_file: Path,
    b64logo: bytes,
    baseline: "Baseline",
    pdf_theme: str,
    html_css: str,
    logo_path: Path,
    os_name: str,
    version_info: dict[str, Any],
    show_all_tags: bool,
    custom: bool,
    language: str,
    spinner: Yaspin,
) -> None:
    import pymupdf

    spinner.spinner = Spinners.dots
    spinner.text = "Generating PythonPDF"

    template_dir: str = config["documents_templates_dir"]
    themes_dir: str = config["themes_dir"]
    logo_dir: str = config["images_dir"]
    if custom:
        template_dir = config["custom"]["documents_templates_dir"]
        themes_dir = config["custom"]["themes_dir"]
        logo_dir = config["custom"]["images_dir"]

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as tmp:
        tmp_path = Path(tmp.name)

    render_template(
        tmp_path,
        "main.jinja",
        baseline,
        b64logo,
        pdf_theme,
        html_css,
        logo_path,
        os_name,
        version_info,
        show_all_tags,
        custom,
        template_dir,
        themes_dir,
        logo_dir,
        output_format="pdf",
        language=language,
    )

    html_content = tmp_path.read_text()
    tmp_path.unlink(missing_ok=True)

    # Cover page text derived from baseline
    html_title, html_subtitle = map(str.strip, baseline.title.split(":", 1))
    compliance_version: str = str(version_info.get("compliance_version", ""))
    release_date: str = str(version_info.get("date", ""))
    footer_doc_title: str = f"{html_title}: {html_subtitle}"

    logo_bytes: bytes = base64.b64decode(b64logo)

    arch = pymupdf.Archive()
    arch.add((logo_bytes, "banner.png"))

    # Embed NotoSerif + mplus1mn from asciidoctor-pdf gem so text metrics match original exactly
    font_face_css: str = ""
    if _FONT_BYTES_CACHE:
        _font_meta = {
            "notoserif-regular.ttf":    ("notoserif", "normal", "normal"),
            "notoserif-bold.ttf":       ("notoserif", "bold",   "normal"),
            "notoserif-italic.ttf":     ("notoserif", "normal", "italic"),
            "notoserif-bolditalic.ttf": ("notoserif", "bold",   "italic"),
            "mplus1mn-regular.ttf":     ("mplus1mn",  "normal", "normal"),
            "mplus1mn-bold.ttf":        ("mplus1mn",  "bold",   "normal"),
        }
        for arch_name, font_data in _FONT_BYTES_CACHE.items():
            arch.add((font_data, arch_name))
            family, weight, style = _font_meta[arch_name]
            font_face_css += (
                f'@font-face {{ font-family: "{family}"; font-weight: {weight}; '
                f'font-style: {style}; src: url("{arch_name}"); }}\n'
            )
    else:
        _project_root = Path(__file__).parent.parent.parent.parent.parent
        for _gem_fonts in _project_root.glob("mscp_gems/ruby/*/gems/asciidoctor-pdf-*/data/fonts"):
            _font_map = {
                "notoserif-regular.ttf":    ("notoserif-regular-subset.ttf",   "notoserif", "normal", "normal"),
                "notoserif-bold.ttf":       ("notoserif-bold-subset.ttf",       "notoserif", "bold",   "normal"),
                "notoserif-italic.ttf":     ("notoserif-italic-subset.ttf",     "notoserif", "normal", "italic"),
                "notoserif-bolditalic.ttf": ("notoserif-bold_italic-subset.ttf","notoserif", "bold",   "italic"),
                "mplus1mn-regular.ttf":     ("mplus1mn-regular-subset.ttf",     "mplus1mn",  "normal", "normal"),
                "mplus1mn-bold.ttf":        ("mplus1mn-bold-subset.ttf",        "mplus1mn",  "bold",   "normal"),
            }
            for arch_name, (gem_file, family, weight, style) in _font_map.items():
                fp = _gem_fonts / gem_file
                if fp.exists():
                    font_data = fp.read_bytes()
                    _FONT_BYTES_CACHE[arch_name] = font_data
                    arch.add((font_data, arch_name))
                    font_face_css += (
                        f'@font-face {{ font-family: "{family}"; font-weight: {weight}; '
                        f'font-style: {style}; src: url("{arch_name}"); }}\n'
                    )
            if font_face_css:
                break
    if not font_face_css:
        logger.warning("asciidoctor-pdf gem fonts not found; PDF text flow may differ from original")

    # A4 paper matching original (595.28 × 841.89 pt), margins matching original measurements
    MEDIABOX = pymupdf.paper_rect("a4")
    W = MEDIABOX.width    # 595.28
    H = MEDIABOX.height   # 841.89
    MX, MY = 48, 36       # left/top margins (measured from original)
    MY_BOT = 6            # bottom margin between body text and footer separator (measured from original)
    FOOTER_H = 43         # footer zone height from separator to page bottom (measured from original)

    # Body area: top margin to (separator - bottom margin)
    BODY = pymupdf.Rect(MX, MY, W - MX, H - MY_BOT - FOOTER_H)

    # Highlighting uses inline styles (noclasses=True) so no external Pygments CSS needed
    full_css: str = font_face_css + PDF_CSS

    def rectfn(rect_num: int, filled: "pymupdf.Rect"):
        return MEDIABOX, BODY, pymupdf.Identity

    # Render loop: fix refs-table splits only.  Rules (h3) and code blocks (pre) are
    # allowed to break naturally across pages — section-level breaks (h2) are handled
    # by CSS (.sect1 { page-break-before: always }).  Only refs tables are fixed
    # iteratively because a split refs grid looks genuinely bad.
    _html_dirty = True
    # Intermediate render passes use _story_render_no_links() — link insertion
    # accounts for ~80% of write_with_links time, so skipping it on detection-only
    # passes gives an ~8x speedup per intermediate render.
    for _render_pass in range(8):
        story = pymupdf.Story(html=html_content, user_css=full_css, archive=arch)
        doc = _story_render_no_links(story, rectfn)
        _html_dirty = False
        _page_blocks = [pg.get_text("dict")["blocks"] for pg in doc]
        _split_refs = _detect_split_refs_in_pdf(doc, BODY, html_content, _page_blocks)
        if not _split_refs:
            break
        for _rid in _split_refs:
            html_content = _inject_refs_page_break(html_content, _rid)
        _html_dirty = True

    # Final render with links — either because the loop ended dirty, or because
    # we need to upgrade the last no-links render to a full linked document.
    # Capture heading positions for the PDF outline (sidebar bookmarks).
    _heading_positions: list = []
    def _capture_headings(pos: Any) -> None:
        if pos.heading > 0 and pos.open_close == 1 and pos.text:
            _heading_positions.append(pos)

    story = pymupdf.Story(html=html_content, user_css=full_css, archive=arch)
    doc = story.write_with_links(rectfn, positionfn=_capture_headings)

    # Cleanup: remove any refs injections that became spurious after layout settled.
    for _cleanup_pass in range(6):
        _page_blocks = [pg.get_text("dict")["blocks"] for pg in doc]
        _spurious_refs = _find_spurious_refs_injections(doc, BODY, html_content, _page_blocks)
        if not _spurious_refs:
            break
        for _rid in _spurious_refs:
            html_content = _remove_refs_page_break(html_content, _rid)
        _heading_positions.clear()
        story = pymupdf.Story(html=html_content, user_css=full_css, archive=arch)
        doc = story.write_with_links(rectfn, positionfn=_capture_headings)

    # ── Post-process: draw code-block fills and refs-table grid ──────────────
    # CSS background-color / border on block elements cause PyMuPDF Story ghost
    # artifacts on all subsequent pages.  All visual styling is applied here via
    # page.draw_* after story rendering.  Colors/widths match asciidoctor-pdf defaults:
    #   code block:  fill=#f5f5f5 stroke=#cccccc  radius=4  width=0.75
    #   refs table:  stroke=#dddddd  width=0.5
    _CODE_FILL    = (0.9608, 0.9608, 0.9608)   # #f5f5f5
    _CODE_STROKE  = (0.8,    0.8,    0.8   )   # #cccccc
    _REF_STROKE   = (0.8667, 0.8667, 0.8667)   # #dddddd
    _CODE_R_PT = 4  # border-radius in points
    _CODE_W  = 0.75
    _REF_W   = 0.5
    _OD = MX + 80   # outer refs vertical divider x (label col | value col)
    _KAPPA = 0.5523  # cubic bezier kappa for circular arc approximation

    # Ref-row label spans in the left column (one per row after "ID")
    _INNER_STD = frozenset({
        "800-53r5", "800-171r3", "CCE", "SFR", "DISA STIG(s)",
        "CIS Benchmark", "CIS Controls V8", "indigo", "CMMC", "TAGS",
    })

    def _rounded_rect(page, rect, color, fill, width, round_top, round_bot, overlay=False):
        """Draw a rect with selectively rounded top and/or bottom corners.
        Uses cubic bezier paths so each edge can be independently rounded or square.
        overlay=False prepends to page stream (drawn behind Story text).
        """
        if rect.height < 2 or rect.width < 2:
            return
        r = min(_CODE_R_PT, rect.width / 8, rect.height / 4)
        if round_top and round_bot:
            rad = min(r / min(rect.height, rect.width), 0.49)
            page.draw_rect(rect, color=color, fill=fill, width=width, radius=rad, overlay=overlay)
            return
        if not round_top and not round_bot:
            page.draw_rect(rect, color=color, fill=fill, width=width, overlay=overlay)
            return
        x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
        k = r * _KAPPA
        # draw_cont uses PDF y-up coordinates; convert from PyMuPDF y-down
        H_pg = page.rect.height
        sh = page.new_shape()
        if round_top:   # rounded top, square bottom
            sh.draw_cont += "%g %g m " % (x0 + r, H_pg - y0)
            sh.draw_cont += "%g %g l " % (x1 - r, H_pg - y0)
            sh.draw_cont += "%g %g %g %g %g %g c " % (x1-r+k, H_pg-y0,   x1, H_pg-y0-k,   x1, H_pg-y0-r)
            sh.draw_cont += "%g %g l " % (x1, H_pg - y1)
            sh.draw_cont += "%g %g l " % (x0, H_pg - y1)
            sh.draw_cont += "%g %g l " % (x0, H_pg - y0 - r)
            sh.draw_cont += "%g %g %g %g %g %g c " % (x0, H_pg-y0-r+k,   x0+k, H_pg-y0,   x0+r, H_pg-y0)
        else:           # square top, rounded bottom
            sh.draw_cont += "%g %g m " % (x0, H_pg - y0)
            sh.draw_cont += "%g %g l " % (x1, H_pg - y0)
            sh.draw_cont += "%g %g l " % (x1, H_pg - y1 + r)
            sh.draw_cont += "%g %g %g %g %g %g c " % (x1, H_pg-y1+r-k,   x1-r+k, H_pg-y1,   x1-r, H_pg-y1)
            sh.draw_cont += "%g %g l " % (x0 + r, H_pg - y1)
            sh.draw_cont += "%g %g %g %g %g %g c " % (x0+r-k, H_pg-y1,   x0, H_pg-y1+k,   x0, H_pg-y1+r)
            sh.draw_cont += "%g %g l " % (x0, H_pg - y0)
        sh.finish(color=color, fill=fill, width=width, closePath=True)
        sh.commit(overlay=overlay)

    for _page in doc:
        _blks = sorted(_page.get_text("dict")["blocks"], key=lambda b: b["bbox"][1])

        # ── A. Code-block fills (overlay=False → drawn behind text) ───────────
        # Group consecutive mplus1mn blocks; draw a single rounded filled rect
        # per group to replicate the asciidoctor-pdf code-block styling.
        _cg: list | None = None   # [y0, y1] of current group

        for _b in _blks:
            _is_mono = False
            if "lines" in _b:
                _bspans = [s for _l in _b["lines"] for s in _l.get("spans", [])]
                if _bspans:
                    _is_mono = all("mplus" in s.get("font", "").lower() for s in _bspans)
            _bx0, _by0, _bx1, _by1 = _b["bbox"]

            if _is_mono and (_bx1 - _bx0) > 5:
                if _cg is None:
                    _cg = [_by0, _by1]
                elif _by0 - _cg[1] < 14:
                    _cg[1] = _by1
                else:
                    _cr_y0 = max(_cg[0] - 6, BODY.y0)
                    _cr_y1 = min(_cg[1] + 6, BODY.y1)
                    _rounded_rect(_page, pymupdf.Rect(BODY.x0 + 6, _cr_y0, BODY.x1 - 6, _cr_y1),
                                  color=_CODE_STROKE, fill=_CODE_FILL, width=_CODE_W,
                                  round_top=_cr_y0 > BODY.y0 + 1, round_bot=_cr_y1 < BODY.y1 - 1)
                    _cg = [_by0, _by1]
            else:
                if _cg is not None:
                    _cr_y0 = max(_cg[0] - 6, BODY.y0)
                    _cr_y1 = min(_cg[1] + 6, BODY.y1)
                    _rounded_rect(_page, pymupdf.Rect(BODY.x0 + 6, _cr_y0, BODY.x1 - 6, _cr_y1),
                                  color=_CODE_STROKE, fill=_CODE_FILL, width=_CODE_W,
                                  round_top=_cr_y0 > BODY.y0 + 1, round_bot=_cr_y1 < BODY.y1 - 1)
                    _cg = None
        if _cg is not None:
            _cr_y0 = max(_cg[0] - 6, BODY.y0)
            _cr_y1 = min(_cg[1] + 6, BODY.y1)
            _rounded_rect(_page, pymupdf.Rect(BODY.x0 + 6, _cr_y0, BODY.x1 - 6, _cr_y1),
                          color=_CODE_STROKE, fill=_CODE_FILL, width=_CODE_W,
                          round_top=_cr_y0 > BODY.y0 + 1, round_bot=_cr_y1 < BODY.y1 - 1)

        # ── C. Refs-table grid (overlay=True → thin lines on top) ─────────────
        # A page may contain multiple rules, each with its own refs table.
        # Collect them all then draw; reset state at each rule heading.
        # Flat refs table: rows are ID, 800-53r5, CCE, etc. — no nested tables.
        # Each th.ref-label span in the left column (x < MX+40, Bold) signals a new row.
        _refs_tables = []
        _rt_y0 = None
        _rt_y1 = None
        _rt_row_ys = []   # y0 of each row after the first (for horizontal dividers)

        # Cross-page continuation: if the page starts with a ref-label Bold span
        # at the left margin (y < 80) that is NOT "ID", the table continued from
        # the previous page — seed _rt_y0 so row detection proceeds normally.
        for _cb in _blks:
            if "lines" not in _cb:
                continue
            _cb_spans = [s for _l in _cb["lines"] for s in _l.get("spans", [])]
            if (_cb["bbox"][1] < 80 and any(
                    s.get("text", "").strip() in _INNER_STD
                    and s["bbox"][0] < MX + 40
                    and "Bold" in s.get("font", "")
                    for s in _cb_spans)):
                _rt_y0 = _cb["bbox"][1] - 4
            break

        for _b in _blks:
            if "lines" not in _b:
                continue
            _bx0, _by0, _bx1, _by1 = _b["bbox"]
            _bspans = [s for _l in _b["lines"] for s in _l.get("spans", [])]

            # A new rule heading (h3/h2) commits and resets state
            if any(s.get("size", 0) > 14 for s in _bspans):
                if _rt_y0 is not None:
                    _refs_tables.append((_rt_y0, _rt_y1, list(_rt_row_ys)))
                _rt_y0 = None; _rt_y1 = None; _rt_row_ys = []
                continue

            if _rt_y0 is None:
                # Look for "ID" label to start a refs table
                for _s in _bspans:
                    if (_s.get("text", "").strip() == "ID"
                            and _s["bbox"][0] < MX + 40
                            and "Bold" in _s.get("font", "")):
                        _rt_y0 = _by0
                        _rt_y1 = _by1
                        break
                continue

            _rt_y1 = _by1

            # Each new ref-label Bold span in the left column = new row divider
            for _s in _bspans:
                if (_s.get("text", "").strip() in _INNER_STD
                        and _s["bbox"][0] < MX + 40
                        and "Bold" in _s.get("font", "")):
                    if not _rt_row_ys or abs(_by0 - _rt_row_ys[-1]) > 3:
                        _rt_row_ys.append(_by0)
                    break

        if _rt_y0 is not None:
            _refs_tables.append((_rt_y0, _rt_y1, list(_rt_row_ys)))

        _LABEL_FILL = (0.910, 0.925, 0.957)   # #e8ecf4 — left label column

        for (_ry0, _ry1, _row_ys) in _refs_tables:
            if _ry1 is None:
                continue
            _T = _ry0 - 2
            _B = _ry1 + 2

            # Left label column background
            _page.draw_rect(
                pymupdf.Rect(BODY.x0, _T, _OD, _B),
                color=None, fill=_LABEL_FILL, width=0, overlay=False,
            )
            # Outer bounding box
            _page.draw_rect(
                pymupdf.Rect(BODY.x0, _T, BODY.x1, _B),
                color=_REF_STROKE, fill=None, width=_REF_W,
            )
            # Vertical divider (label col | value col)
            _page.draw_line(
                pymupdf.Point(_OD, _T), pymupdf.Point(_OD, _B),
                color=_REF_STROKE, width=_REF_W,
            )
            # Horizontal row dividers
            for _ry in _row_ys:
                _dy = _ry - 2
                _page.draw_line(
                    pymupdf.Point(BODY.x0, _dy), pymupdf.Point(BODY.x1, _dy),
                    color=_REF_STROKE, width=_REF_W,
                )

        # ── D. Admonition markings (NOTE: / IMPORTANT:) ────────────────────────
        # CSS backgrounds on block elements cause Story ghost artifacts, so we
        # draw fills and border lines here as post-processing.
        _NOTE_COLOR      = (0.357, 0.608, 0.835)   # #5b9bd5
        _NOTE_FILL       = (0.922, 0.953, 0.984)   # #ebf3fb
        _IMPORT_COLOR    = (0.851, 0.188, 0.145)   # #d93025
        _IMPORT_FILL     = (0.992, 0.941, 0.941)   # #fdf0f0
        _ADMON_LINE_W    = 3.0
        _ADMON_LX        = BODY.x0                 # flush with left edge of body/fill

        _admon_y0: float | None = None
        _admon_y1: float | None = None
        _admon_border = None
        _admon_fill   = None

        for _b in _blks:
            if "lines" not in _b:
                continue
            _bspans = [s for _l in _b["lines"] for s in _l.get("spans", [])]
            _by0 = _b["bbox"][1]
            _by1 = _b["bbox"][3]

            _is_heading    = any(s.get("size", 0) > 14 for s in _bspans)
            _new_note      = any(s.get("text","").strip() == "NOTE:"
                                 and "Bold" in s.get("font","") for s in _bspans)
            _new_important = any(s.get("text","").strip() == "IMPORTANT:"
                                 and "Bold" in s.get("font","") for s in _bspans)

            # Flush current admonition on heading or new admonition start
            if (_is_heading or _new_note or _new_important) and _admon_y0 is not None:
                _ar = pymupdf.Rect(BODY.x0, _admon_y0 - 3, BODY.x1, _admon_y1 + 3)
                _page.draw_rect(_ar, color=None, fill=_admon_fill, width=0, overlay=False)
                _page.draw_line(
                    pymupdf.Point(_ADMON_LX, _admon_y0 - 3),
                    pymupdf.Point(_ADMON_LX, _admon_y1 + 3),
                    color=_admon_border, width=_ADMON_LINE_W,
                )
                _admon_y0 = None; _admon_y1 = None
                _admon_border = None; _admon_fill = None

            if _is_heading:
                continue

            if _new_note:
                _admon_y0 = _by0; _admon_y1 = _by1
                _admon_border = _NOTE_COLOR; _admon_fill = _NOTE_FILL
            elif _new_important:
                _admon_y0 = _by0; _admon_y1 = _by1
                _admon_border = _IMPORT_COLOR; _admon_fill = _IMPORT_FILL
            elif _admon_border is not None:
                if _by0 - _admon_y1 < 6:
                    _admon_y1 = _by1
                else:
                    # Gap too large — flush and stop tracking
                    _ar = pymupdf.Rect(BODY.x0, _admon_y0 - 3, BODY.x1, _admon_y1 + 3)
                    _page.draw_rect(_ar, color=None, fill=_admon_fill, width=0, overlay=False)
                    _page.draw_line(
                        pymupdf.Point(_ADMON_LX, _admon_y0 - 3),
                        pymupdf.Point(_ADMON_LX, _admon_y1 + 3),
                        color=_admon_border, width=_ADMON_LINE_W,
                    )
                    _admon_y0 = None; _admon_y1 = None
                    _admon_border = None; _admon_fill = None

        # Final flush for admonition reaching page bottom
        if _admon_y0 is not None:
            _ar = pymupdf.Rect(BODY.x0, _admon_y0 - 3, BODY.x1, _admon_y1 + 3)
            _page.draw_rect(_ar, color=None, fill=_admon_fill, width=0, overlay=False)
            _page.draw_line(
                pymupdf.Point(_ADMON_LX, _admon_y0 - 3),
                pymupdf.Point(_ADMON_LX, _admon_y1 + 3),
                color=_admon_border, width=_ADMON_LINE_W,
            )

    # ── Find foreword page so body numbering starts at 1 ──────────────────────
    # The foreword heading "1. Foreword" appears near the top of its page (y < 50)
    # while the TOC entry also says "1. Foreword" but is deeper on the page.
    foreword_pno: int = 0
    for pno in range(len(doc)):
        for hit in doc[pno].search_for("Foreword"):
            if hit.y0 < 50:   # heading at top margin = foreword content page
                foreword_pno = pno
                break
        if foreword_pno:
            break

    # ── Add dot leaders + page numbers to TOC pages ──────────────────────────
    # PyMuPDF Story creates word-level link rects — group by dest page + line.
    # insert_textbox silently fails on Story-rendered pages; use insert_text instead.
    _right_edge = W - MX
    _dot_pair = ". "
    _dot_pair_w = pymupdf.get_text_length(_dot_pair, fontname="helv", fontsize=10)

    for _pno in range(foreword_pno):   # all pages before the foreword = TOC pages
        _page = doc[_pno]
        _all_links = [lk for lk in _page.get_links() if lk.get("kind") == pymupdf.LINK_GOTO]
        if not _all_links:
            continue

        # Group word-level rects by (dest_page, rounded y0) → merge into one row rect
        _rows: dict[tuple[int, int], list[pymupdf.Rect]] = {}
        for _lk in _all_links:
            _dest = _lk["page"]
            _display = _dest - foreword_pno + 1
            if _display < 1:
                continue
            _r = pymupdf.Rect(_lk["from"])
            _key = (_display, round(_r.y0))
            _rows.setdefault(_key, []).append(_r)

        # For entries that wrap (same dest, multiple y lines), use the last line only
        _by_dest: dict[int, tuple[int, list[pymupdf.Rect]]] = {}
        for (_display, _y_int), _rects in _rows.items():
            if _display not in _by_dest or _y_int > _by_dest[_display][0]:
                _by_dest[_display] = (_y_int, _rects)

        for _display, (_y_int, _rects) in _by_dest.items():
            _text_x1 = max(r.x1 for r in _rects)
            _baseline = _rects[0].y1 - 1.5   # text baseline

            # Right-aligned page number
            _num_str = str(_display)
            _num_w = pymupdf.get_text_length(_num_str, fontname="helv", fontsize=10)
            _num_x = _right_edge - _num_w
            _page.insert_text(
                pymupdf.Point(_num_x, _baseline),
                _num_str, fontsize=10, fontname="helv", color=(0.2, 0.2, 0.2),
            )

            # Dot leaders filling space between text end and page number
            _dot_start = _text_x1 + 4
            _dot_end = _num_x - 4
            _avail = _dot_end - _dot_start
            if _avail > _dot_pair_w:
                _n_pairs = int(_avail / _dot_pair_w)
                _dots = _dot_pair * _n_pairs
                _page.insert_text(
                    pymupdf.Point(_dot_start, _baseline),
                    _dots, fontsize=10, fontname="helv", color=(0.5, 0.5, 0.5),
                )

    # ── Add separator line + footer to every page ─────────────────────────────
    # sep_y measured from original: H - FOOTER_H = 841.89 - 43 = 798.89
    sep_y = H - FOOTER_H               # y of separator line (measured: 798.69)
    ftr_y = sep_y + 5                  # y of footer text start (first baseline ~815, matching original 814.4)

    version_full = f"{compliance_version} ({release_date})" if release_date else compliance_version

    # Pre-compute footer text metrics once (avoids per-page HTML layout from insert_htmlbox)
    _FTR_FS  = 9
    _FTR_CLR = (0.2, 0.2, 0.2)   # #333333
    _ftr_line1_y = ftr_y + _FTR_FS + 1        # baseline of line 1
    _ftr_line2_y = _ftr_line1_y + round(_FTR_FS * 1.35) + 1  # baseline of line 2

    _t1   = footer_doc_title
    _t1_w = pymupdf.get_text_length(_t1, fontname="tiro", fontsize=_FTR_FS)
    _t1_x = MX + (W - 2 * MX - _t1_w) / 2

    _ftr_reg = "macOS Security Compliance Project - "
    _ftr_ita = version_full
    _ftr_rw  = pymupdf.get_text_length(_ftr_reg, fontname="tiro", fontsize=_FTR_FS)
    _ftr_iw  = pymupdf.get_text_length(_ftr_ita, fontname="tiit", fontsize=_FTR_FS)
    _t2_x    = MX + (W - 2 * MX - (_ftr_rw + _ftr_iw)) / 2

    for pno in range(len(doc)):
        page = doc[pno]
        display_num: int = pno - foreword_pno + 1  # foreword page = 1
        show_num: bool = display_num >= 1
        is_recto: bool = (display_num % 2 == 1)

        # Separator line — color matches original (0.867, 0.867, 0.867)
        page.draw_line(
            pymupdf.Point(MX, sep_y),
            pymupdf.Point(W - MX, sep_y),
            color=(0.867, 0.867, 0.867),
            width=0.5,
        )

        # Centered footer text — two lines, plain serif + italic serif
        page.insert_text(pymupdf.Point(_t1_x, _ftr_line1_y), _t1,
                         fontname="tiro", fontsize=_FTR_FS, color=_FTR_CLR)
        page.insert_text(pymupdf.Point(_t2_x, _ftr_line2_y), _ftr_reg,
                         fontname="tiro", fontsize=_FTR_FS, color=_FTR_CLR)
        page.insert_text(pymupdf.Point(_t2_x + _ftr_rw, _ftr_line2_y), _ftr_ita,
                         fontname="tiit", fontsize=_FTR_FS, color=_FTR_CLR)

        # Page number — right-aligned bold serif
        if show_num:
            _ns  = str(display_num)
            _nsw = pymupdf.get_text_length(_ns, fontname="tibo", fontsize=_FTR_FS)
            page.insert_text(pymupdf.Point(W - MX - _nsw, _ftr_line1_y), _ns,
                             fontname="tibo", fontsize=_FTR_FS, color=_FTR_CLR)

    # ── Insert cover page at index 0 (PyMuPDF auto-updates GoTo link targets) ──
    cover = doc.new_page(pno=0, width=W, height=H)

    # Logo — exact dimensions from original: x=[48, 547], y=[112, 192]
    cover.insert_image(
        pymupdf.Rect(48, 112, 547, 192),
        stream=logo_bytes,
        keep_proportion=True,
    )

    # Title "macOS 26.0" — NotoSerif 27pt #999999 right-aligned
    cover.insert_textbox(
        pymupdf.Rect(MX, 430, W - MX, 478),
        html_title,
        fontname="tibo",
        fontsize=27,
        color=(0.6, 0.6, 0.6),
        align=2,
    )

    # Subtitle — NotoSerif-BoldItalic 18pt #333333 right-aligned
    cover.insert_textbox(
        pymupdf.Rect(MX, 478, W - MX, 512),
        html_subtitle,
        fontname="tibi",
        fontsize=18,
        color=(0.2, 0.2, 0.2),
        align=2,
    )

    # Version line — NotoSerif 13pt #181818 right-aligned
    cover.insert_textbox(
        pymupdf.Rect(MX, 518, W - MX, 542),
        f"{compliance_version} ({release_date})",
        fontname="tiro",
        fontsize=13,
        color=(0.094, 0.094, 0.094),
        align=2,
    )

    # Build PDF outline (sidebar bookmarks in Preview/Acrobat).
    # Cover page was inserted at pno=0, shifting all story pages by +1.
    # h2 → level 1, h3 → level 2 — matches the reference asciidoctor-pdf structure.
    _toc = [[1, footer_doc_title, 1]]  # document title → cover page
    for _pos in _heading_positions:
        _level = _pos.heading - 1  # h2→1, h3→2
        if _level < 1:
            continue
        _page = _pos.page_num + 1  # +1 for inserted cover page (1-based)
        _dest = {"kind": 1, "page": _page - 1, "to": pymupdf.Point(_pos.rect[0], _pos.rect[1]), "zoom": 0}
        _toc.append([_level, _pos.text.strip(), _page, _dest])
    doc.set_toc(_toc)

    doc.save(str(pdf_file), deflate=True, garbage=4, use_objstms=True, deflate_fonts=True, deflate_images=True)
    total_pages = doc.page_count
    doc.close()

    logger.info(f"PyMuPDF PDF written: {pdf_file} ({total_pages} pages)")


def generate_documents(
    spinner: Yaspin,
    output_file: Path,
    baseline: Baseline,
    b64logo: bytes,
    pdf_theme: str,
    html_css: str,
    logo_path: Path,
    os_name: str,
    version_info: dict[str, Any],
    show_all_tags: bool = False,
    custom: bool = False,
    output_format: str = "adoc",
    language: str = "en",
) -> None:
    """Render guidance documents and, for AsciiDoc output, invoke AsciiDoctor.

    Selects standard or custom template/theme directories, calls
    `render_template`, then (when *output_format* is ``"adoc"``) runs
    ``bundle exec asciidoctor`` and ``bundle exec asciidoctor-pdf`` to
    produce HTML and PDF output.

    Args:
        spinner (Yaspin): Spinner for progress feedback.
        output_file (Path): Destination ``.adoc`` or ``.md`` file.
        baseline (Baseline): Baseline data model.
        b64logo (bytes): Base64-encoded logo image bytes.
        pdf_theme (str): AsciiDoctor-PDF theme filename.
        html_css (str): CSS filename for HTML output.
        logo_path (Path): Absolute path to the logo file.
        os_name (str): Operating system name string.
        version_info (dict[str, Any]): OS/compliance version metadata.
        show_all_tags (bool): Whether to render all tags. Defaults to ``False``.
        custom (bool): Whether to use the custom template directory. Defaults to ``False``.
        output_format (str): ``"adoc"`` (default) or ``"markdown"``.
        language (str): BCP-47 language code. Defaults to ``"en"``.
    """
    template_dir: str = config["documents_templates_dir"]
    themes_dir: str = config["themes_dir"]
    logo_dir: str = config["images_dir"]

    if custom:
        template_dir = config["custom"]["documents_templates_dir"]
        themes_dir = config["custom"]["themes_dir"]
        logo_dir = config["custom"]["images_dir"]

    render_template(
        output_file,
        "main.jinja",
        baseline,
        b64logo,
        pdf_theme,
        html_css,
        logo_path,
        os_name,
        version_info,
        show_all_tags,
        custom,
        template_dir,
        themes_dir,
        logo_dir,
        output_format,
        language,
    )

    if output_format == "adoc":
        spinner.spinner = Spinners.dots
        spinner.text = "Checking for asciidoctor components"
        time.sleep(1)
        asciidoctor_pdf_path, asciidoctor_pdf_err = run_command(
            "bundle show asciidoctor-pdf"
        )

        if asciidoctor_pdf_err:
            spinner.text = "Installing missing asciidoctor components"
            time.sleep(1)
            output, error = run_command(
                "bundle install --gemfile Gemfile --path mscp_gems --binstubs"
            )
            if error:
                logger.error(f"Bundle install failed: {error}")
                sys.exit()
        spinner.text = "Generating PDF file from adoc"
        output, error = run_command(f"bundle exec asciidoctor-pdf {output_file}")
        if error:
            logger.error(f"Error converting to ADOC: {error}")
            sys.exit()
