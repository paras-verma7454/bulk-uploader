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
from math import cos, pi, sin
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageChops, ImageDraw, ImageFont
from docx import Document
from lxml import etree

import cloudinary
from cloudinary import uploader as cloudinary_uploader

QUESTION_START_RE = re.compile(r"^\s*(?:q\s*)?(\d+)[\.)]\s*(.*)$", re.IGNORECASE)
OPTION_RE = re.compile(r"^\s*(?:\(([A-Da-d])\)|([A-Da-d])(?:[\.)]|\s+))\s*(.*)$")
ANSWER_RE = re.compile(r"^\s*(?:ans|answer)\s*[:.\-\s]*\(?([A-Da-d])\)?\b", re.IGNORECASE)
SOLUTION_RE = re.compile(r"^\s*(?:sol(?:ution)?|explanation)\b[:.\-\s]*(.*)$", re.IGNORECASE)
COMPOUND_MARKER_RE = re.compile(r"^\s*#\s*(?:start|end)\s+compound(?:\s+hindi)?\s*#\s*$", re.IGNORECASE)
SPECIAL_TEXT_REPLACEMENTS = {
	
	"\uf0d0": "Δ",
	"\uf0c4": "Δ",
	"\ue0b0": "Δ",
	"": "Δ",
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
	compound_text: str = ""
	compound_html: str = ""
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

	s = re.sub(r"<[^>]+>", "", html_fragment)
	s = html.unescape(s)
	return normalize_text(s)


def extract_option_content(text: str, html_value: str, option_match: re.Match[str]) -> tuple[str, str]:
	body_text = normalize_text(option_match.group(3) or "")

	prefix = text[: option_match.start(3)]
	cleaned_html = html_value

	def _html_to_text(h: str) -> str:
		
		s = re.sub(r"<[^>]+>", "", h)
		s = html.unescape(s)
		return normalize_text(s)

	if not body_text:
		
		escaped_prefix = html.escape(prefix)
		if cleaned_html.startswith(escaped_prefix):
			candidate = cleaned_html[len(escaped_prefix) :].lstrip()
			body_text = _html_to_text(candidate)
			cleaned_html = candidate
		else:
			
			stripped_prefix = html.escape(prefix.rstrip())
			if cleaned_html.startswith(stripped_prefix):
				candidate = cleaned_html[len(stripped_prefix) :].lstrip()
				body_text = _html_to_text(candidate)
				cleaned_html = candidate
			else:
			
				for marker in ("<span", "<img", "<math", "<svg"):
					idx = cleaned_html.find(marker)
					if idx != -1:
						candidate = cleaned_html[idx:]
						body_text = _html_to_text(candidate)
						cleaned_html = candidate
						break

	
	if not body_text:
		body_text = normalize_text(option_match.group(3) or "")

	
	if prefix:
		prefix_clean = prefix.rstrip()
		if body_text:
			body_text = prefix_clean + " " + body_text
		else:
			body_text = prefix_clean
		
		
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

		
		text = normalize_text(paragraph.text)
		raw_html = "".join(html_parts)
	
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
	except Exception as exc: 
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
		elif local_name in {"object", "pict"}:
			ole_html = render_ole_preview(child, part)
			if ole_html:
				parts.append(ole_html)
		elif local_name in {"oMath", "oMathPara"}:
			parts.append(render_equation(child))
		else:
			parts.extend(render_xml_content(child, part))

	return parts


def render_drawing(drawing: etree._Element, part) -> str:
	chart_html = render_chart_drawing(drawing, part)
	if chart_html:
		return chart_html

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

	
	supported_content_types = {
		"image/png",
		"image/jpeg",
		"image/gif",
		"image/webp",
		"image/svg+xml",
	}
	if content_type not in supported_content_types:
		if content_type in {"image/x-emf", "image/emf", "image/x-wmf", "image/wmf"}:
			converted_html = render_metafile_with_inkscape(content_type, image_bytes)
			if converted_html:
				return converted_html
		return '<span class="embedded-placeholder">[image]</span>'

	url = upload_image_to_cloudinary(content_type, image_bytes)

	return f'<img class="embedded-image" alt="Embedded image" src="{html.escape(url, quote=True)}" />'


def render_chart_drawing(drawing: etree._Element, part) -> str | None:
	namespaces = {
		"c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
		"r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
	}
	chart = drawing.find(".//c:chart", namespaces=namespaces)
	if chart is None:
		return None

	rel_id = chart.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
	if not rel_id:
		return '<span class="embedded-placeholder">[chart]</span>'

	chart_part = part.related_parts.get(rel_id)
	if chart_part is None:
		return '<span class="embedded-placeholder">[chart]</span>'

	chart_bytes = getattr(chart_part, "blob", b"") or b""
	if not chart_bytes:
		return '<span class="embedded-placeholder">[chart]</span>'

	try:
		chart_root = etree.fromstring(chart_bytes)
		rendered_bytes = render_pie_chart_png(chart_root)
	except Exception:
		logger.exception("Failed to render chart drawing")
		return '<span class="embedded-placeholder">[chart]</span>'

	if not rendered_bytes:
		return '<span class="embedded-placeholder">[chart]</span>'

	url = upload_image_to_cloudinary("image/png", rendered_bytes)
	return f'<img class="embedded-image" alt="Embedded chart" src="{html.escape(url, quote=True)}" />'


def render_pie_chart_png(chart_root: etree._Element) -> bytes | None:
	namespaces = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart"}
	pie_chart = chart_root.find(".//c:pieChart", namespaces=namespaces)
	if pie_chart is None:
		return None

	series = pie_chart.find(".//c:ser", namespaces=namespaces)
	if series is None:
		return None

	labels = chart_cache_values(series, ".//c:cat//c:strCache", namespaces)
	if not labels:
		labels = chart_cache_values(series, ".//c:cat//c:numCache", namespaces)
	values_text = chart_cache_values(series, ".//c:val//c:numCache", namespaces)
	if not labels or not values_text:
		return None

	values: list[float] = []
	cleaned_labels: list[str] = []
	for label, value_text in zip(labels, values_text):
		try:
			value = float(value_text)
		except ValueError:
			continue
		if value <= 0:
			continue
		cleaned_labels.append(label)
		values.append(value)

	if not cleaned_labels or not values:
		return None

	return draw_pie_chart_png(cleaned_labels, values)


def chart_cache_values(parent: etree._Element, xpath: str, namespaces: dict[str, str]) -> list[str]:
	cache = parent.find(xpath, namespaces=namespaces)
	if cache is None:
		return []

	points: list[tuple[int, str]] = []
	for point in cache.findall(".//c:pt", namespaces=namespaces):
		idx_text = point.get("idx", "0")
		value_node = point.find("c:v", namespaces=namespaces)
		if value_node is None or value_node.text is None:
			continue
		try:
			idx = int(idx_text)
		except ValueError:
			idx = 0
		points.append((idx, normalize_text(value_node.text)))

	return [value for _, value in sorted(points)]


def draw_pie_chart_png(labels: list[str], values: list[float]) -> bytes:
	width = 720
	height = 520
	image = Image.new("RGB", (width, height), "white")
	draw = ImageDraw.Draw(image)
	font = ImageFont.load_default()
	total = sum(values)

	colors = [
		(68, 114, 196),
		(237, 125, 49),
		(165, 165, 165),
		(255, 192, 0),
		(91, 155, 213),
		(112, 173, 71),
	]
	box = (95, 40, 485, 430)
	start_angle = -90.0
	mid_angles: list[float] = []

	for index, value in enumerate(values):
		sweep = 360.0 * value / total
		end_angle = start_angle + sweep
		draw.pieslice(box, start=start_angle, end=end_angle, fill=colors[index % len(colors)], outline="white", width=2)
		mid_angles.append(start_angle + sweep / 2.0)
		start_angle = end_angle

	center_x = (box[0] + box[2]) / 2.0
	center_y = (box[1] + box[3]) / 2.0
	radius = (box[2] - box[0]) / 2.0
	for label, value, angle in zip(labels, values, mid_angles):
		radians = angle * pi / 180.0
		x = center_x + cos(radians) * radius * 0.62
		y = center_y + sin(radians) * radius * 0.62
		text = f"{label}\n{value:g}"
		bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=3)
		text_width = bbox[2] - bbox[0]
		text_height = bbox[3] - bbox[1]
		draw.multiline_text((x - text_width / 2, y - text_height / 2), text, fill="black", font=font, spacing=3, align="center")

	legend_x = 525
	legend_y = 145
	for index, label in enumerate(labels):
		y = legend_y + index * 32
		draw.rectangle((legend_x, y, legend_x + 18, y + 18), fill=colors[index % len(colors)])
		draw.text((legend_x + 28, y + 2), label, fill="black", font=font)

	output = BytesIO()
	image.save(output, format="PNG")
	return output.getvalue()


def render_ole_preview(element: etree._Element, part) -> str | None:
	image_part = find_ole_preview_part(element, part)
	if image_part is None:
		return None

	content_type = getattr(image_part, "content_type", "") or ""
	image_bytes = getattr(image_part, "blob", b"") or b""
	if not content_type.startswith("image/") or not image_bytes:
		return '<span class="embedded-placeholder">[image]</span>'

	supported_content_types = {
		"image/png",
		"image/jpeg",
		"image/gif",
		"image/webp",
		"image/svg+xml",
	}
	if content_type not in supported_content_types:
		if content_type in {"image/x-emf", "image/emf", "image/x-wmf", "image/wmf"}:
			converted_html = render_metafile_with_inkscape(content_type, image_bytes)
			if converted_html:
				return converted_html
		return '<span class="embedded-placeholder">[image]</span>'

	url = upload_image_to_cloudinary(content_type, image_bytes)
	return f'<img class="embedded-image" alt="Embedded image" src="{html.escape(url, quote=True)}" />'


def find_ole_preview_part(element: etree._Element, part):
	namespaces = {
		"v": "urn:schemas-microsoft-com:vml",
		"r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
	}
	imagedata_nodes = element.xpath(".//v:imagedata", namespaces=namespaces)
	if not imagedata_nodes:
		return None

	for node in imagedata_nodes:
		rel_id = node.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
		if not rel_id:
			rel_id = attr_by_local(node, "id")
		if not rel_id:
			rel_id = attr_by_local(node, "embed")
		if not rel_id:
			continue
		related_part = part.related_parts.get(rel_id)
		if related_part is not None:
			return related_part
	return None


def render_metafile_with_inkscape(content_type: str, image_bytes: bytes) -> str | None:
	suffix = ".emf" if "emf" in content_type else ".wmf"
	
	with tempfile.TemporaryDirectory(prefix="metafile-") as tmpdir:
		temp_dir = Path(tmpdir)
		source_path = temp_dir / f"image{suffix}"
		target_path = temp_dir / "image.png"
		source_path.write_bytes(image_bytes)

		command = [
			"inkscape",
			str(source_path),
			"--export-area-drawing",
			"-o",
			str(target_path),
		]

		try:
			subprocess.run(command, check=True, capture_output=True, text=True)
		except (OSError, subprocess.CalledProcessError):
			return None

		if not target_path.exists():
			return None

		converted_bytes = target_path.read_bytes()
		trimmed_bytes = trim_png_bytes(converted_bytes, source_path=target_path)
		if trimmed_bytes:
			converted_bytes = trimmed_bytes
		url = upload_image_to_cloudinary("image/png", converted_bytes)

		return f'<img class="embedded-image" alt="Embedded image" src="{html.escape(url, quote=True)}" />'


def trim_png_bytes(png_bytes: bytes, source_path: Path) -> bytes | None:
	trimmed_bytes = None
	try:
		trimmed_bytes = trim_png_with_imagemagick(source_path)
	except (OSError, subprocess.CalledProcessError):
		logger.exception("ImageMagick trim failed; falling back to Pillow")

	if trimmed_bytes:
		return trimmed_bytes

	try:
		return trim_png_with_pillow(png_bytes)
	except Exception:
		logger.exception("Pillow trim failed; using untrimmed image")
		return None


def trim_png_with_imagemagick(source_path: Path) -> bytes | None:
	magick_cmd = shutil.which("magick") or shutil.which("convert")
	if not magick_cmd:
		return None

	target_path = source_path.with_name("image-trimmed.png")
	command = [
		magick_cmd,
		str(source_path),
		"-fuzz",
		"1%",
		"-trim",
		"+repage",
		str(target_path),
	]
	subprocess.run(command, check=True, capture_output=True, text=True)

	if not target_path.exists():
		return None

	return target_path.read_bytes()


def trim_png_with_pillow(png_bytes: bytes) -> bytes | None:
	image = Image.open(BytesIO(png_bytes))
	if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
		background = Image.new("RGBA", image.size, (255, 255, 255, 255))
		image = Image.alpha_composite(background, image.convert("RGBA")).convert("RGB")
	else:
		image = image.convert("RGB")

	white = Image.new("RGB", image.size, (255, 255, 255))
	diff = ImageChops.difference(image, white).convert("L")
	threshold = int(255 * 0.01)
	mask = diff.point(lambda p: 255 if p > threshold else 0)
	bbox = mask.getbbox()
	if not bbox or bbox == (0, 0, image.width, image.height):
		return None

	cropped = image.crop(bbox)
	output = BytesIO()
	cropped.save(output, format="PNG")
	return output.getvalue()


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


def is_compound_marker(text: str) -> bool:
	return bool(COMPOUND_MARKER_RE.match(text))


def extract_question_content(text: str, html_value: str, question_match: re.Match[str]) -> tuple[str, str]:
	body_text = normalize_text(question_match.group(2) or "")
	if not body_text:
		return normalize_text(text), html_value

	prefix = text[: question_match.start(2)]
	cleaned_html = html_value
	escaped_prefix = html.escape(prefix)
	if cleaned_html.startswith(escaped_prefix):
		cleaned_html = cleaned_html[len(escaped_prefix) :].lstrip()
	else:
		stripped_prefix = html.escape(prefix.rstrip())
		if cleaned_html.startswith(stripped_prefix):
			cleaned_html = cleaned_html[len(stripped_prefix) :].lstrip()

	return body_text, cleaned_html or html.escape(body_text)


def parse_questions_from_fragments(fragments: list[ParagraphFragment], source_file: str) -> list[MCQQuestion]:
	questions: list[MCQQuestion] = []
	current_question: MCQQuestion | None = None
	current_option: MCQOption | None = None
	compound_text_parts: list[str] = []
	compound_html_parts: list[str] = []
	in_solution = False

	for fragment in fragments:
		text = fragment.text
		html_value = fragment.html

		if not text and not html_value:
			continue

		if is_compound_marker(text):
			if current_question is not None:
				questions.append(current_question)
			marker_text = normalize_text(text).lower()
			if "end compound" in marker_text:
				compound_text_parts = []
				compound_html_parts = []
			elif "start compound" in marker_text:
				compound_text_parts = []
				compound_html_parts = []
			current_option = None
			current_question = None
			in_solution = False
			continue

		question_start = QUESTION_START_RE.match(text)
		if question_start:
			if current_question is not None:
				questions.append(current_question)

			question_text, question_html = extract_question_content(text, html_value, question_start)
			current_question = MCQQuestion(
				source_file=source_file,
				number=question_start.group(1),
				compound_text=normalize_text(" ".join(compound_text_parts)),
				compound_html=join_html_parts(compound_html_parts),
			)
			current_question.question_text_parts.append(question_text)
			current_question.question_html_parts.append(question_html)
			current_option = None
			in_solution = False
			continue

		if current_question is None:
			if text:
				compound_text_parts.append(text)
			if html_value:
				compound_html_parts.append(html_value)
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

		
		option_match = OPTION_RE.match(text)
		used_text_for_match = text
		if option_match is None and html_value:
			candidate = html_to_text(html_value)
			if candidate:
				option_match = OPTION_RE.match(candidate)
				used_text_for_match = candidate
		if option_match:
			option_label = option_match.group(1) or option_match.group(2)
			
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


def compound_to_dict(
	compound_text: str,
	compound_html: str,
	questions: list[MCQQuestion],
) -> dict[str, str | int | list[dict[str, str | list[dict[str, str]] | None]]]:
	return {
		"compound_text": compound_text,
		"compound_html": compound_html,
		"total_questions": len(questions),
		"questions": [question_to_dict(question) for question in questions],
	}


def group_compound_questions(questions: list[MCQQuestion]) -> list[dict[str, str | int | list[dict[str, str | list[dict[str, str]] | None]]]]:
	compounds: list[dict[str, str | int | list[dict[str, str | list[dict[str, str]] | None]]]] = []
	current_key: tuple[str, str] | None = None
	current_questions: list[MCQQuestion] = []

	for question in questions:
		if not question.compound_text and not question.compound_html:
			continue

		key = (question.compound_text, question.compound_html)
		if current_key is not None and key != current_key:
			compounds.append(compound_to_dict(current_key[0], current_key[1], current_questions))
			current_questions = []

		current_key = key
		current_questions.append(question)

	if current_key is not None:
		compounds.append(compound_to_dict(current_key[0], current_key[1], current_questions))

	return compounds


def report_to_dict(report: DocumentReport) -> dict[str, str | int | list[dict[str, str | int | list[dict[str, str | list[dict[str, str]] | None]] | None]]]:
	return {
		"source_file": report.source_file,
		"total_questions": len(report.questions),
		"questions": [question_to_dict(question) for question in report.questions],
		"compounds": group_compound_questions(report.questions),
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
