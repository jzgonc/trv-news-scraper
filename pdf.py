"""Print-PDF rendering (WeasyPrint) for notice-board printouts.

Generic on purpose: takes a label, title, meta line, body HTML and image paths, so it
can be reused for other content (e.g. newsletters) without changes.
"""

import html
import logging
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from string import Template
from urllib.parse import urlparse

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
MAX_IMAGE_EDGE = 1500
JPEG_QUALITY = 80
MAX_GRID_IMAGES = 3
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?")


# --------------------------------------------------------------------------- cleaning

class _Cleaner(HTMLParser):
    """Reduce CMS HTML to p/br/strong/em/ul/ol/li/h2-h4/a without any attributes but href.

    The CMS mostly emits one <div> per line and empty <div>&nbsp;</div> (or <br><br>) as
    paragraph separators. Lines become <br>, empty lines start a new <p>. Breaks are only
    written when more text follows, so trailing/duplicate blank lines vanish.
    """

    PARA = {"p", "blockquote", "section", "article", "header", "footer", "table", "tr",
            "pre", "figure", "address", "center"}
    LINE = {"div", "td", "th", "figcaption", "dd", "dt"}
    HEADINGS = {"h1": "h2", "h2": "h2", "h3": "h3", "h4": "h4", "h5": "h4", "h6": "h4"}
    INLINE = {"strong": "strong", "b": "strong", "em": "em", "i": "em", "a": "a"}
    SKIP = {"script", "style", "head", "title", "noscript", "iframe", "svg", "object"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.block = None          # currently open block tag
        self.next_block = "p"      # block to open when the next text arrives
        self.pending = None        # None | "soft" | "hard"
        self.inline: list[tuple[str, str]] = []  # (tag, opening markup)
        self.lists = 0
        self.skip = 0
        self.text_seen = 0
        self.line_marks: list[int] = []
        self.br_run = 0

    # helpers
    def _open(self, tag):
        self.out.append(f"<{tag}>")
        self.block = tag
        self.out.extend(markup for _, markup in self.inline)

    def _close(self):
        if self.block:
            self.out.extend(f"</{t}>" for t, _ in reversed(self.inline))
            self.out.append(f"</{self.block}>")
            self.block = None
        self.pending = None

    def _boundary(self, next_block="p"):
        self._close()
        self.next_block = next_block

    def _request(self, kind):
        if not self.block:
            return
        if kind == "hard" and self.block == "li":
            kind = "soft"
        if kind == "hard" or self.pending is None:
            self.pending = kind

    def _text(self, text):
        if not text.strip():
            if self.block and self.pending is None and text:
                self.out.append(" ")
            return
        if self.block and self.pending == "hard":
            self._close()
        if not self.block:
            tag = self.next_block
            if tag == "p" and self.lists:
                tag = "li"
            self._open(tag)
            text = text.lstrip()
        elif self.pending == "soft":
            self.out.append("<br>")
            text = text.lstrip()
        self.pending = None
        self.br_run = 0
        self.text_seen += 1
        self.out.append(html.escape(text, quote=False))

    # parser callbacks
    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in self.PARA:
            self._boundary()
        elif tag in self.LINE:
            self._request("soft")
            self.line_marks.append(self.text_seen)
        elif tag in self.HEADINGS:
            self._boundary(self.HEADINGS[tag])
        elif tag in ("ul", "ol"):
            self._boundary()
            self.out.append(f"<{tag}>")
            self.lists += 1
        elif tag == "li":
            self._boundary("li" if self.lists else "p")
        elif tag == "br":
            self.br_run += 1
            self._request("hard" if self.br_run >= 2 else "soft")
        elif tag in self.INLINE:
            name = self.INLINE[tag]
            markup = f"<{name}>"
            if name == "a":
                href = dict(attrs).get("href") or ""
                if urlparse(href).scheme not in ("http", "https", "mailto"):
                    href = ""
                markup = f'<a href="{html.escape(href)}">' if href else "<a>"
            self.inline.append((name, markup))
            if self.block:
                self.out.append(markup)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in ("br",):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in self.PARA or tag in self.HEADINGS or tag == "li":
            self._boundary()
        elif tag in self.LINE:
            start = self.line_marks.pop() if self.line_marks else self.text_seen
            self._request("soft" if self.text_seen > start else "hard")
        elif tag in ("ul", "ol") and self.lists:
            self._boundary()
            self.out.append(f"</{tag}>")
            self.lists -= 1
        elif tag in self.INLINE:
            name = self.INLINE[tag]
            names = [t for t, _ in self.inline]
            if name not in names:
                return
            idx = len(names) - 1 - names[::-1].index(name)
            above = self.inline[idx + 1:]
            if self.block:
                self.out.extend(f"</{t}>" for t, _ in reversed(above))
                self.out.append(f"</{name}>")
                self.out.extend(m for _, m in above)
            del self.inline[idx]

    def handle_data(self, data):
        if not self.skip:
            self._text(data)

    def result(self):
        self._close()
        for tag in ("ul", "ol"):
            while self.lists:
                self.out.append(f"</{tag}>")
                self.lists -= 1
        text = "".join(self.out)
        for _ in range(3):  # remove empty inline/list elements left behind
            text = re.sub(r"<(strong|em|a)\b[^>]*>\s*</\1>", "", text)
            text = re.sub(r"<(ul|ol)>\s*</\1>", "", text)
        return text


def clean_html(raw: str) -> str:
    """Return a sanitized, attribute-free version of CMS body HTML."""
    parser = _Cleaner()
    parser.feed(raw or "")
    parser.close()
    return parser.result()


# --------------------------------------------------------------------------- images

def prepare_images(paths, out_dir: Path, max_edge: int = MAX_IMAGE_EDGE) -> list[Path]:
    """Downscale images to JPEGs (EXIF-rotated, RGB) so the PDF stays small."""
    from PIL import Image, ImageOps

    result = []
    for i, src in enumerate(paths):
        try:
            with Image.open(src) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGBA")
                    bg = Image.new("RGB", img.size, "white")
                    bg.paste(img, mask=img.getchannel("A"))
                    img = bg
                else:
                    img = img.convert("RGB")
                img.thumbnail((max_edge, max_edge))
                dest = out_dir / f"img_{i}.jpg"
                img.save(dest, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
                result.append(dest)
        except Exception as exc:  # broken/unsupported image: just leave it out
            log.warning("skipping image %s: %s", src, exc)
    return result


# --------------------------------------------------------------------------- rendering

def _color_style(accent: str | None, accent_contrast: str | None) -> str:
    """Inline CSS overriding the default colors from print.css (only valid hex colors)."""
    decls = [f"{var}: {value}" for var, value in (("--accent", accent),
                                                   ("--accent-contrast", accent_contrast))
             if value and _HEX_COLOR.fullmatch(value)]
    return f' style="{"; ".join(decls)}"' if decls else ""


def _build_html(title, label, meta, body_html, lead, grid, lead_small=False,
                color_style="") -> str:
    lead_cls = "lead small" if lead_small else "lead"
    lead_html = f'<figure class="{lead_cls}"><img src="{lead.as_uri()}"></figure>' if lead else ""
    grid_html = ""
    if grid:
        cells = "".join(f'<div class="cell"><img src="{p.as_uri()}"></div>' for p in grid)
        grid_html = f'<div class="grid grid-{len(grid)}">{cells}</div>'
    tpl = Template((TEMPLATE_DIR / "print.html").read_text(encoding="utf-8"))
    return tpl.substitute(
        title=html.escape(title),
        color_style=color_style,
        label=html.escape(label),
        label_class="" if label else "hidden",
        meta=html.escape(meta),
        lead=lead_html,
        body=body_html,
        grid=grid_html,
    )


def render_pdf(out_path: Path, *, title: str, label: str = "", meta: str = "",
               body_html: str = "", images=(), max_pages: int = 2,
               accent: str | None = None, accent_contrast: str | None = None) -> int:
    """Render a print PDF and return its page count.

    Tries to fit on one page, then on `max_pages`, dropping grid images first and the
    lead image last. The text is never shortened: if even the text alone exceeds
    `max_pages`, the text-only version is written anyway.
    `accent` / `accent_contrast` (hex colors) override the label, title and heading
    color and the label text color; invalid values are ignored.
    """
    from weasyprint import CSS, HTML
    from weasyprint.text.fonts import FontConfiguration

    font_config = FontConfiguration()
    css = CSS(filename=str(TEMPLATE_DIR / "print.css"), font_config=font_config)
    body = clean_html(body_html)
    color_style = _color_style(accent, accent_contrast)

    with tempfile.TemporaryDirectory() as tmp:
        imgs = prepare_images(list(images), Path(tmp))
        # Variants in order of preference: (lead, grid, small lead).
        # Grid images go first, the lead image only if needed to stay within max_pages.
        image_cfgs = []
        if imgs:
            for n_grid in range(min(MAX_GRID_IMAGES, len(imgs) - 1), -1, -1):
                image_cfgs.append((imgs[0], tuple(imgs[1:1 + n_grid]), False))
            image_cfgs.append((imgs[0], (), True))
        no_images = (None, (), False)
        one_page = image_cfgs or [no_images]
        two_pages = image_cfgs + [no_images]

        cache = {}

        def render(variant):
            if variant not in cache:
                lead, grid, lead_small = variant
                doc_html = _build_html(title, label, meta, body, lead, list(grid), lead_small,
                                       color_style)
                cache[variant] = HTML(string=doc_html, base_url=str(TEMPLATE_DIR)).render(
                    stylesheets=[css], font_config=font_config)
            return cache[variant]

        attempts = [(1, one_page)] + ([(max_pages, two_pages)] if max_pages > 1 else [])
        chosen = next((render(v) for limit, variants in attempts for v in variants
                       if len(render(v).pages) <= limit), None)
        if chosen is None:
            chosen = render(no_images)
            log.warning("text of %r needs %d pages (> %d)", title, len(chosen.pages), max_pages)

        out_path = Path(out_path)
        tmp_out = out_path.with_suffix(".pdf.tmp")
        chosen.write_pdf(tmp_out)
        tmp_out.replace(out_path)
        return len(chosen.pages)
