from __future__ import annotations

import html
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from docx import Document
from lxml import etree

import cloudinary
from cloudinary import uploader as cloudinary_uploader

QUESTION_START_RE = re.compile(r"^\s*(\d+)[\.)]\s*(.*)$")
OPTION_RE = re.compile(r"^\s*(?:\(([A-Da-d])\)|([A-Da-d])(?:[\.)]|\s+))\s*(.*)$")
ANSWER_RE = re.compile(r"^\s*(?:ans|answer)\s*[:.\-\s]*\(?([A-Da-d])\)?\b", re.IGNORECASE)
SOLUTION_RE = re.compile(r"^\s*(?:sol(?:ution)?|explanation)\b[:.\-\s]*(.*)$", re.IGNORECASE)
SPECIAL_TEXT_REPLACEMENTS = {
	# Map Word's private-use triangle glyphs and similar markers to a
	# standard Delta (Δ) which renders reliably across fonts.
	"\uf0d0": "Δ",
	"\uf0c4": "Δ",
	"\ue0b0": "Δ",
	# Some environments render the private-use glyph as a visible glyph
	# character in the string; include the literal in the map as well.
	"": "Δ",
	# Also map the plain white-up-pointing-triangle to Delta
	"\u25b3": "Δ",
}


logger = logging.getLogger(__name__)
_CLOUDINARY_CONFIGURED: bool | None = None
_CLOUDINARY_FOLDER = "docx-images"
_CLOUDINARY_UPLOAD_CACHE: OrderedDict[str, str] = OrderedDict()
_CLOUDINARY_CACHE_MAX_ITEMS = 512
_CLOUDINARY_UPLOAD_TIMEOUT_SECONDS = 20


@dataclass(slots=True)
class ParagraphFragment:
	text: str
	html: str


@dataclass(slots=True)
class MCQOption:
	label: str
	text_parts: list[str] = field(default_factory=list)
	html_parts: list[str] = field(default_factory=list)

	@property
	def text(self) -> str:
		return normalize_text(" ".join(self.text_parts))

	@property
	def html(self) -> str:
		return join_html_parts(self.html_parts)


@dataclass(slots=True)
class MCQQuestion:
	source_file: str
	number: str | None = None
	question_text_parts: list[str] = field(default_factory=list)
	question_html_parts: list[str] = field(default_factory=list)
	options: list[MCQOption] = field(default_factory=list)
	answer: str | None = None
	solution_text_parts: list[str] = field(default_factory=list)
	solution_html_parts: list[str] = field(default_factory=list)

	@property
	def question_text(self) -> str:
		return normalize_text(" ".join(self.question_text_parts))

	@property
	def question_html(self) -> str:
		return join_html_parts(self.question_html_parts)

	@property
	def solution_text(self) -> str:
		return normalize_text(" ".join(self.solution_text_parts))

	@property
	def solution_html(self) -> str:
		return join_html_parts(self.solution_html_parts)


@dataclass(slots=True)
class DocumentReport:
	source_file: str
	questions: list[MCQQuestion]


def normalize_text(text: str) -> str:
	text = replace_special_text_symbols(text)
	cleaned = text.replace("\u00a0", " ").replace("\u200b", "")
	cleaned = re.sub(r"\s+", " ", cleaned).strip()
	return cleaned


def replace_special_text_symbols(text: str) -> str:
	for source, target in SPECIAL_TEXT_REPLACEMENTS.items():
		text = text.replace(source, target)
	return text


def normalize_html(html_text: str) -> str:
	lines = html_text.split("<br/>")
	cleaned_lines: list[str] = []

	for line in lines:
		line = re.sub(r"^(?:&emsp;\s*)+", "", line)
		line = re.sub(r"(?:&emsp;\s*)+$", "", line)
		if line.strip():
			cleaned_lines.append(line)

	return "<br/>".join(cleaned_lines).strip()


def join_html_parts(parts: list[str]) -> str:
	filtered = [normalize_html(part) for part in parts if part.strip()]
	filtered = [part for part in filtered if part]
	return "<br/>".join(filtered)


def html_to_text(html_fragment: str) -> str:
	# Strip tags and unescape entities to produce a plain-text fallback
	s = re.sub(r"<[^>]+>", "", html_fragment)
	s = html.unescape(s)
	return normalize_text(s)


def extract_option_content(text: str, html_value: str, option_match: re.Match[str]) -> tuple[str, str]:
	body_text = normalize_text(option_match.group(3) or "")

	prefix = text[: option_match.start(3)]
	cleaned_html = html_value

	def _html_to_text(h: str) -> str:
		# Remove tags and unescape HTML entities to produce a readable fallback
		s = re.sub(r"<[^>]+>", "", h)
		s = html.unescape(s)
		return normalize_text(s)

	# If body text is empty (common when the option body is an equation
	# rendered into HTML but not present in paragraph.text), try to extract
	# the body from the HTML fragment instead of returning empty values.
	if not body_text:
		# Attempt to strip the label prefix from the HTML and use the rest
		escaped_prefix = html.escape(prefix)
		if cleaned_html.startswith(escaped_prefix):
			candidate = cleaned_html[len(escaped_prefix) :].lstrip()
			body_text = _html_to_text(candidate)
			cleaned_html = candidate
		else:
			# Try prefix without trailing whitespace
			stripped_prefix = html.escape(prefix.rstrip())
			if cleaned_html.startswith(stripped_prefix):
				candidate = cleaned_html[len(stripped_prefix) :].lstrip()
				body_text = _html_to_text(candidate)
				cleaned_html = candidate
			else:
				# If the HTML contains an equation span or other inline element,
				# try to locate the first inline element and use from there.
				# Fallback: search for the first '<span' or '<img' occurrence.
				for marker in ("<span", "<img", "<math", "<svg"):
					idx = cleaned_html.find(marker)
					if idx != -1:
						candidate = cleaned_html[idx:]
						body_text = _html_to_text(candidate)
						cleaned_html = candidate
						break

	# If we still don't have body_text, fall back to the original capture
	if not body_text:
		body_text = normalize_text(option_match.group(3) or "")

	# Ensure the prefix (label) is always included in both text and HTML output
	# This prevents losing option labels when content is only in HTML (e.g., equations)
	# Always prepend prefix if it exists, even if body_text is empty
	if prefix:
		prefix_clean = prefix.rstrip()
		if body_text:
			body_text = prefix_clean + " " + body_text
		else:
			body_text = prefix_clean
		
		# Preserve the prefix in HTML as well
		escaped_prefix_safe = html.escape(prefix_clean)
		if not cleaned_html.lstrip().startswith(escaped_prefix_safe):
			cleaned_html = html.escape(prefix_clean) + " " + cleaned_html.lstrip()

	return body_text, cleaned_html.lstrip()


def load_doc(file_obj: BinaryIO) -> Document:
	return Document(file_obj)


def extract_paragraph_fragments(doc: Document) -> list[ParagraphFragment]:
	fragments: list[ParagraphFragment] = []

	for paragraph in doc.paragraphs:
		html_parts = render_xml_content(paragraph._p, paragraph.part)

		# Normalize plain text and also ensure any special/private-use
		# glyphs are converted in both plain-text and HTML fragments.
		text = normalize_text(paragraph.text)
		raw_html = "".join(html_parts)
		# Replace special symbols in HTML fragments as well so
		# `question_html` contains the normalized symbol.
		raw_html = replace_special_text_symbols(raw_html)
		html_value = normalize_html(raw_html)

		if text or html_value:
			fragments.append(ParagraphFragment(text=text, html=html_value or html.escape(paragraph.text)))

	return fragments


def configure_cloudinary() -> bool:
	global _CLOUDINARY_CONFIGURED
	global _CLOUDINARY_FOLDER
	global _CLOUDINARY_CACHE_MAX_ITEMS
	global _CLOUDINARY_UPLOAD_TIMEOUT_SECONDS

	if _CLOUDINARY_CONFIGURED is not None:
		return _CLOUDINARY_CONFIGURED

	cloudinary_url = os.getenv("CLOUDINARY_URL", "").strip()
	cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
	api_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
	api_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()

	if cloudinary_url:
		cloudinary.config(cloudinary_url=cloudinary_url, secure=True)
		_CLOUDINARY_CONFIGURED = True
	elif cloud_name and api_key and api_secret:
		cloudinary.config(
			cloud_name=cloud_name,
			api_key=api_key,
			api_secret=api_secret,
			secure=True,
		)
		_CLOUDINARY_CONFIGURED = True
	else:
		logger.warning(
			"Cloudinary is not configured. Set CLOUDINARY_URL or CLOUDINARY_CLOUD_NAME/CLOUDINARY_API_KEY/CLOUDINARY_API_SECRET."
		)
		_CLOUDINARY_CONFIGURED = False

	folder = os.getenv("CLOUDINARY_FOLDER", "").strip()
	if folder:
		_CLOUDINARY_FOLDER = folder

	cache_size_value = os.getenv("CLOUDINARY_CACHE_MAX_ITEMS", "").strip()
	if cache_size_value:
		try:
			_CLOUDINARY_CACHE_MAX_ITEMS = max(1, int(cache_size_value))
		except ValueError:
			logger.warning("Invalid CLOUDINARY_CACHE_MAX_ITEMS value '%s'; using default %s", cache_size_value, _CLOUDINARY_CACHE_MAX_ITEMS)

	timeout_value = os.getenv("CLOUDINARY_UPLOAD_TIMEOUT_SECONDS", "").strip()
	if timeout_value:
		try:
			_CLOUDINARY_UPLOAD_TIMEOUT_SECONDS = max(1, int(timeout_value))
		except ValueError:
			logger.warning(
				"Invalid CLOUDINARY_UPLOAD_TIMEOUT_SECONDS value '%s'; using default %s",
				timeout_value,
				_CLOUDINARY_UPLOAD_TIMEOUT_SECONDS,
			)

	return _CLOUDINARY_CONFIGURED


def upload_image_to_cloudinary(content_type: str, image_bytes: bytes) -> str | None:
	if not image_bytes:
		return None

	if not configure_cloudinary():
		raise RuntimeError(
			"Cloudinary is not configured. Set CLOUDINARY_URL or CLOUDINARY_CLOUD_NAME/CLOUDINARY_API_KEY/CLOUDINARY_API_SECRET."
		)

	hash_key = hashlib.sha256(image_bytes).hexdigest()
	cached_url = _CLOUDINARY_UPLOAD_CACHE.get(hash_key)
	if cached_url:
		_CLOUDINARY_UPLOAD_CACHE.move_to_end(hash_key)
		return cached_url

	public_id = f"{_CLOUDINARY_FOLDER}/{uuid.uuid4().hex}"

	try:
		result = cloudinary_uploader.upload(
			image_bytes,
			resource_type="image",
			public_id=public_id,
			overwrite=True,
			unique_filename=False,
			use_filename=False,
			timeout=_CLOUDINARY_UPLOAD_TIMEOUT_SECONDS,
		)
	except Exception as exc:  # noqa: BLE001
		logger.exception("Failed to upload image to Cloudinary")
		raise RuntimeError("Failed to upload embedded image to Cloudinary") from exc

	url = str(result.get("secure_url") or result.get("url") or "")
	if not url:
		raise RuntimeError("Cloudinary upload did not return an image URL")

	_CLOUDINARY_UPLOAD_CACHE[hash_key] = url
	if len(_CLOUDINARY_UPLOAD_CACHE) > _CLOUDINARY_CACHE_MAX_ITEMS:
		_CLOUDINARY_UPLOAD_CACHE.popitem(last=False)
	return url


def render_xml_content(element: etree._Element, part) -> list[str]:
	parts: list[str] = []

	for child in element:
		local_name = etree.QName(child).localname

		if local_name == "t":
			parts.append(html.escape(replace_special_text_symbols(child.text or "")))
		elif local_name == "tab":
			parts.append("&emsp;")
		elif local_name in {"br", "cr"}:
			parts.append("<br/>")
		elif local_name == "drawing":
			parts.append(render_drawing(child, part))
		elif local_name in {"oMath", "oMathPara"}:
			parts.append(render_equation(child))
		else:
			parts.extend(render_xml_content(child, part))

	return parts


def render_drawing(drawing: etree._Element, part) -> str:
	namespaces = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
	blip = drawing.find(".//a:blip", namespaces=namespaces)
	if blip is None:
		return '<span class="embedded-placeholder">[image]</span>'

	embed_id = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
	if not embed_id:
		return '<span class="embedded-placeholder">[image]</span>'

	related_part = part.related_parts.get(embed_id)
	if related_part is None:
		return '<span class="embedded-placeholder">[image]</span>'

	content_type = getattr(related_part, "content_type", "") or ""
	image_bytes = getattr(related_part, "blob", b"") or b""
	if not content_type.startswith("image/") or not image_bytes:
		return '<span class="embedded-placeholder">[image]</span>'

	# Browsers handle these inline reliably; upload to Cloudinary and store URL.
	supported_content_types = {
		"image/png",
		"image/jpeg",
		"image/gif",
		"image/webp",
		"image/svg+xml",
	}
	if content_type not in supported_content_types:
		if content_type in {"image/x-emf", "image/emf", "image/x-wmf", "image/wmf"}:
			converted_html = render_windows_metafile(content_type, image_bytes)
			if converted_html:
				return converted_html
		return '<span class="embedded-placeholder">[image]</span>'

	url = upload_image_to_cloudinary(content_type, image_bytes)

	return f'<img class="embedded-image" alt="Embedded image" src="{html.escape(url, quote=True)}" />'


def render_windows_metafile(content_type: str, image_bytes: bytes) -> str | None:
	if os.name != "nt":
		return None

	suffix = ".emf" if "emf" in content_type else ".wmf"
	# Use a system temporary directory to avoid creating a persistent `tmp` folder
	with tempfile.TemporaryDirectory(prefix="metafile-") as tmpdir:
		temp_dir = Path(tmpdir)
		source_path = temp_dir / f"image{suffix}"
		target_path = temp_dir / "image.png"
		source_path.write_bytes(image_bytes)

		command = [
			"powershell",
			"-NoProfile",
			"-Command",
			(
				"Add-Type -AssemblyName System.Drawing; "
				f"$img = [System.Drawing.Image]::FromFile('{source_path}'); "
				"$bmp = New-Object System.Drawing.Bitmap $img.Width, $img.Height; "
				"$graphics = [System.Drawing.Graphics]::FromImage($bmp); "
				"$graphics.Clear([System.Drawing.Color]::White); "
				"$graphics.DrawImage($img, 0, 0, $img.Width, $img.Height); "
				f"$bmp.Save('{target_path}', [System.Drawing.Imaging.ImageFormat]::Png); "
				"$graphics.Dispose(); $bmp.Dispose(); $img.Dispose();"
			),
		]

		try:
			subprocess.run(command, check=True, capture_output=True, text=True)
		except (OSError, subprocess.CalledProcessError):
			return None

		if not target_path.exists():
			return None

		converted_bytes = target_path.read_bytes()
		url = upload_image_to_cloudinary("image/png", converted_bytes)

		return f'<img class="embedded-image" alt="Embedded image" src="{html.escape(url, quote=True)}" />'


def render_equation(element: etree._Element) -> str:
	latex = convert_omml_to_latex(element)
	if latex:
		safe_latex = html.escape(latex, quote=False)
		return f'<span class="equation">\\({safe_latex}\\)</span>'

	equation_text = collect_math_text(element)
	if not equation_text:
		equation_text = etree.tostring(element, encoding="unicode", with_tail=False)
	return f'<span class="equation">{html.escape(equation_text)}</span>'


def local_name(node: etree._Element) -> str:
	return etree.QName(node).localname


def attr_by_local(node: etree._Element, attr_name: str, default: str = "") -> str:
	for key, value in node.attrib.items():
		if etree.QName(key).localname == attr_name:
			return value
	return default


def child_by_local(node: etree._Element, child_name: str) -> etree._Element | None:
	for child in node:
		if local_name(child) == child_name:
			return child
	return None


def latex_group(value: str) -> str:
	if not value:
		return ""
	if len(value) == 1:
		return value
	if value.startswith("{") and value.endswith("}"):
		return value
	return "{" + value + "}"


def convert_omml_to_latex(element: etree._Element) -> str:
	raw_latex = parse_omml_node(element)
	cleaned = normalize_text(replace_math_unicode(raw_latex))
	return cleaned


def parse_omml_node(node: etree._Element) -> str:
	name = local_name(node)

	if name in {"oMath", "oMathPara", "e", "num", "den", "sup", "sub", "deg", "lim", "fName"}:
		return "".join(parse_omml_node(child) for child in node)

	if name == "r":
		parts: list[str] = []
		for child in node:
			child_name = local_name(child)
			if child_name == "t":
				parts.append(child.text or "")
			elif child_name == "sym":
				parts.append(omml_symbol_to_latex(child))
			else:
				parts.append(parse_omml_node(child))
		return "".join(parts)

	if name == "t":
		return node.text or ""

	if name == "f":
		num = parse_omml_node(child_by_local(node, "num")) if child_by_local(node, "num") is not None else ""
		den = parse_omml_node(child_by_local(node, "den")) if child_by_local(node, "den") is not None else ""
		if num and den:
			return f"\\frac{{{num}}}{{{den}}}"
		return num + den

	if name == "sSup":
		base = parse_omml_node(child_by_local(node, "e")) if child_by_local(node, "e") is not None else ""
		sup = parse_omml_node(child_by_local(node, "sup")) if child_by_local(node, "sup") is not None else ""
		if base and sup:
			return f"{latex_group(base)}^{{{sup}}}"
		return base + sup

	if name == "sSub":
		base = parse_omml_node(child_by_local(node, "e")) if child_by_local(node, "e") is not None else ""
		sub = parse_omml_node(child_by_local(node, "sub")) if child_by_local(node, "sub") is not None else ""
		if base and sub:
			return f"{latex_group(base)}_{{{sub}}}"
		return base + sub

	if name == "sSubSup":
		base = parse_omml_node(child_by_local(node, "e")) if child_by_local(node, "e") is not None else ""
		sub = parse_omml_node(child_by_local(node, "sub")) if child_by_local(node, "sub") is not None else ""
		sup = parse_omml_node(child_by_local(node, "sup")) if child_by_local(node, "sup") is not None else ""
		if base:
			suffix = ""
			if sub:
				suffix += f"_{{{sub}}}"
			if sup:
				suffix += f"^{{{sup}}}"
			if suffix:
				return f"{latex_group(base)}{suffix}"
		return base + sub + sup

	if name == "rad":
		degree = parse_omml_node(child_by_local(node, "deg")) if child_by_local(node, "deg") is not None else ""
		radicand = parse_omml_node(child_by_local(node, "e")) if child_by_local(node, "e") is not None else ""
		if degree:
			return f"\\sqrt[{degree}]{{{radicand}}}"
		if radicand:
			return f"\\sqrt{{{radicand}}}"
		return ""

	if name == "nary":
		nary_pr = child_by_local(node, "naryPr")
		op_char = "∑"
		if nary_pr is not None:
			chr_node = child_by_local(nary_pr, "chr")
			if chr_node is not None:
				op_char = attr_by_local(chr_node, "val", "∑") or "∑"

		op = NARY_OPERATOR_MAP.get(op_char, op_char)
		sub = parse_omml_node(child_by_local(node, "sub")) if child_by_local(node, "sub") is not None else ""
		sup = parse_omml_node(child_by_local(node, "sup")) if child_by_local(node, "sup") is not None else ""
		expr = parse_omml_node(child_by_local(node, "e")) if child_by_local(node, "e") is not None else ""

		limits = ""
		if sub:
			limits += f"_{{{sub}}}"
		if sup:
			limits += f"^{{{sup}}}"
		return f"{op}{limits} {latex_group(expr)}"

	if name == "d":
		d_pr = child_by_local(node, "dPr")
		beg_char = "("
		end_char = ")"
		if d_pr is not None:
			beg_node = child_by_local(d_pr, "begChr")
			end_node = child_by_local(d_pr, "endChr")
			if beg_node is not None:
				beg_char = attr_by_local(beg_node, "val", "(") or "("
			if end_node is not None:
				end_char = attr_by_local(end_node, "val", ")") or ")"

		expr = parse_omml_node(child_by_local(node, "e")) if child_by_local(node, "e") is not None else ""
		left = "" if beg_char == "." else beg_char
		right = "" if end_char == "." else end_char
		if expr:
			return f"\\left{left}{expr}\\right{right}"
		return ""

	if name == "func":
		func_name = parse_omml_node(child_by_local(node, "fName")) if child_by_local(node, "fName") is not None else ""
		expr = parse_omml_node(child_by_local(node, "e")) if child_by_local(node, "e") is not None else ""
		if func_name and expr:
			return f"\\{func_name} {latex_group(expr)}"
		return func_name + expr

	if name == "bar":
		expr = parse_omml_node(child_by_local(node, "e")) if child_by_local(node, "e") is not None else ""
		if expr:
			return f"\\overline{{{expr}}}"
		return ""

	if name == "limLow":
		base = parse_omml_node(child_by_local(node, "e")) if child_by_local(node, "e") is not None else ""
		lim = parse_omml_node(child_by_local(node, "lim")) if child_by_local(node, "lim") is not None else ""
		if base and lim:
			return f"{base}_{{{lim}}}"
		return base + lim

	if name == "limUpp":
		base = parse_omml_node(child_by_local(node, "e")) if child_by_local(node, "e") is not None else ""
		lim = parse_omml_node(child_by_local(node, "lim")) if child_by_local(node, "lim") is not None else ""
		if base and lim:
			return f"{base}^{{{lim}}}"
		return base + lim

	return "".join(parse_omml_node(child) for child in node)


NARY_OPERATOR_MAP = {
	"∑": "\\sum",
	"∏": "\\prod",
	"∐": "\\coprod",
	"∫": "\\int",
	"∬": "\\iint",
	"∭": "\\iiint",
	"⋂": "\\bigcap",
	"⋃": "\\bigcup",
	"⋁": "\\bigvee",
	"⋀": "\\bigwedge",
}


SYMBOL_MAP = {
	"≤": "\\le",
	"≥": "\\ge",
	"≠": "\\ne",
	"≈": "\\approx",
	"∞": "\\infty",
	"→": "\\to",
	"×": "\\times",
	"÷": "\\div",
	"±": "\\pm",
	"∂": "\\partial",
	"√": "\\sqrt{}",
	"π": "\\pi",
	"θ": "\\theta",
	"α": "\\alpha",
	"β": "\\beta",
	"γ": "\\gamma",
	"Δ": "\\Delta",
	"δ": "\\delta",
	"λ": "\\lambda",
	"μ": "\\mu",
	"σ": "\\sigma",
	"Σ": "\\Sigma",
	"Ω": "\\Omega",
	"ω": "\\omega",
}


def replace_math_unicode(value: str) -> str:
	for symbol, latex in SYMBOL_MAP.items():
		value = value.replace(symbol, f"{latex} ")
	return value


def omml_symbol_to_latex(node: etree._Element) -> str:
	value = attr_by_local(node, "val", "")
	if not value:
		return ""

	try:
		decoded = chr(int(value, 16))
	except ValueError:
		decoded = value

	return SYMBOL_MAP.get(decoded, decoded)


def collect_math_text(element: etree._Element) -> str:
	parts: list[str] = []

	for node in element.iter():
		if etree.QName(node).localname == "t" and node.text:
			parts.append(node.text)

	return normalize_text("".join(parts))


def parse_questions_from_fragments(fragments: list[ParagraphFragment], source_file: str) -> list[MCQQuestion]:
	questions: list[MCQQuestion] = []
	current_question: MCQQuestion | None = None
	current_option: MCQOption | None = None
	in_solution = False

	for fragment in fragments:
		text = fragment.text
		html_value = fragment.html

		if not text and not html_value:
			continue

		question_start = QUESTION_START_RE.match(text)
		if question_start:
			if current_question is not None:
				questions.append(current_question)

			current_question = MCQQuestion(source_file=source_file, number=question_start.group(1))
			current_question.question_text_parts.append(text)
			current_question.question_html_parts.append(html_value)
			current_option = None
			in_solution = False
			continue

		if current_question is None:
			continue

		answer_match = ANSWER_RE.match(text)
		if answer_match:
			current_question.answer = answer_match.group(1)
			current_option = None
			continue

		solution_match = SOLUTION_RE.match(text)
		if solution_match:
			in_solution = True
			current_option = None
			current_question.solution_text_parts.append(text)
			current_question.solution_html_parts.append(html_value)
			continue

		if in_solution:
			current_question.solution_text_parts.append(text)
			current_question.solution_html_parts.append(html_value)
			continue

		# Try to match option labels in the visible text first; if empty or no match,
		# attempt to match against a plain-text extraction of the HTML fragment
		option_match = OPTION_RE.match(text)
		used_text_for_match = text
		if option_match is None and html_value:
			candidate = html_to_text(html_value)
			if candidate:
				option_match = OPTION_RE.match(candidate)
				used_text_for_match = candidate
		if option_match:
			option_label = option_match.group(1) or option_match.group(2)
			# Pass the text used for matching (either paragraph.text or extracted HTML text)
			option_text, option_html = extract_option_content(used_text_for_match, html_value, option_match)
			current_option = MCQOption(label=option_label)
			if option_text:
				current_option.text_parts.append(option_text)
				current_option.html_parts.append(option_html)
			current_question.options.append(current_option)
			in_solution = False
			continue

		if current_option is not None:
			current_option.text_parts.append(text)
			current_option.html_parts.append(html_value)
		else:
			current_question.question_text_parts.append(text)
			current_question.question_html_parts.append(html_value)

	if current_question is not None:
		questions.append(current_question)

	return questions


def parse_document(file_obj: BinaryIO, source_file: str) -> DocumentReport:
	document = load_doc(file_obj)
	fragments = extract_paragraph_fragments(document)
	questions = parse_questions_from_fragments(fragments, source_file)
	return DocumentReport(source_file=source_file, questions=questions)


def question_to_dict(question: MCQQuestion) -> dict[str, str | list[dict[str, str]] | None]:
	return {
		"source_file": question.source_file,
		"number": question.number,
		"question_text": question.question_text,
		"question_html": question.question_html,
		"options": [
			{
				"label": option.label,
				"text": option.text,
				"html": option.html,
			}
			for option in question.options
		],
		"answer": question.answer,
		"solution_text": question.solution_text,
		"solution_html": question.solution_html,
	}


def report_to_dict(report: DocumentReport) -> dict[str, str | int | list[dict[str, str | list[dict[str, str]] | None]]]:
	return {
		"source_file": report.source_file,
		"total_questions": len(report.questions),
		"questions": [question_to_dict(question) for question in report.questions],
	}


def parse_file_to_dict(
	file_obj: BinaryIO,
	source_file: str,
) -> dict[str, str | int | list[dict[str, str | list[dict[str, str]] | None]]]:
	report = parse_document(file_obj, source_file)
	return report_to_dict(report)


def parse_path_to_dict(path: Path | str) -> dict[str, str | int | list[dict[str, str | list[dict[str, str]] | None]]]:
	path_obj = Path(path)
	with path_obj.open("rb") as file_obj:
		report = parse_document(file_obj, path_obj.name)
	return report_to_dict(report)

def parse_bytes_to_dict(
	file_bytes: bytes,
	source_file: str,
) -> dict[str, str | int | list[dict[str, str | list[dict[str, str]] | None]]]:
	with BytesIO(file_bytes) as file_obj:
		report = parse_document(file_obj, source_file)
	return report_to_dict(report)
