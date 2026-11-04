"""Keyword matching, change detection and HTML highlighting for the email."""
import difflib
import html
import re


def build_keyword_regex(keywords):
    parts = []
    for kw in sorted(set(keywords), key=len, reverse=True):
        esc = re.escape(kw).replace(r"\ ", r"\s+")
        left = r"\b" if kw[:1].isalnum() else ""
        right = r"\b" if kw[-1:].isalnum() else ""
        parts.append(f"{left}{esc}{right}")
    if not parts:
        return None
    return re.compile("(" + "|".join(parts) + ")", re.I)


def find_keywords(text, kw_re, keywords):
    """Return the configured keywords (original spelling) that appear in text."""
    if not kw_re or not text:
        return []
    found = {m.group(0).lower() for m in kw_re.finditer(text)}
    found = {" ".join(f.split()) for f in found}
    return [k for k in keywords if " ".join(k.lower().split()) in found]


class Painter:
    def __init__(self, kw_re, change_color, keyword_color):
        self.kw_re = kw_re
        self.change_style = f"background-color:{change_color};color:#000;padding:0 1px;"
        self.kw_style = (f"background-color:{keyword_color};color:#000;font-weight:bold;"
                         f"padding:0 2px;border-radius:2px;")

    def kw(self, text):
        """Escape text and paint keywords blue."""
        if not self.kw_re:
            return html.escape(text)
        out, pos = [], 0
        for m in self.kw_re.finditer(text):
            out.append(html.escape(text[pos:m.start()]))
            out.append(f'<span style="{self.kw_style}">{html.escape(m.group(0))}</span>')
            pos = m.end()
        out.append(html.escape(text[pos:]))
        return "".join(out)

    def changed(self, text):
        """Paint text yellow (changed) with any keywords inside it blue."""
        return f'<span style="{self.change_style}">{self.kw(text)}</span>'

    def snippets(self, text, max_snips=4, radius=160):
        """Short excerpts around keyword hits, keywords painted blue."""
        if not self.kw_re or not text:
            return []
        flat = " ".join(text.split())
        out, last_end = [], -1
        for m in self.kw_re.finditer(flat):
            if m.start() < last_end:
                continue
            a, b = max(0, m.start() - radius), min(len(flat), m.end() + radius)
            out.append(("…" if a else "") + self.kw(flat[a:b]) + ("…" if b < len(flat) else ""))
            last_end = b
            if len(out) >= max_snips:
                break
        return out


def _word_diff(painter, old_line, new_line):
    old_w, new_w = re.findall(r"\S+|\s+", old_line), re.findall(r"\S+|\s+", new_line)
    sm = difflib.SequenceMatcher(None, old_w, new_w, autojunk=False)
    out = []
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        seg = "".join(new_w[b1:b2])
        if op == "equal":
            out.append(painter.kw(seg))
        elif op in ("replace", "insert"):
            out.append(painter.changed(seg))
        # deleted words are shown in the "removed" line instead
    return "".join(out)


def diff_html(painter, old_text, new_text, context=1, max_blocks=40):
    """
    Returns (html, added_text). Only the changed areas are shown, with a line of context.
    Added/changed text is yellow; keywords anywhere are blue; removed lines are struck through.
    """
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    blocks, added = [], []
    removed_style = "color:#888;text-decoration:line-through;"
    for group in sm.get_grouped_opcodes(context):
        rows = []
        for op, a1, a2, b1, b2 in group:
            if op == "equal":
                for line in new_lines[b1:b2]:
                    rows.append(f'<div style="color:#555;">{painter.kw(line)}</div>')
            elif op == "insert":
                for line in new_lines[b1:b2]:
                    added.append(line)
                    rows.append(f"<div>{painter.changed(line)}</div>")
            elif op == "delete":
                for line in old_lines[a1:a2]:
                    rows.append(f'<div style="{removed_style}">{html.escape(line)}</div>')
            elif op == "replace":
                olds, news = old_lines[a1:a2], new_lines[b1:b2]
                for i in range(max(len(olds), len(news))):
                    o = olds[i] if i < len(olds) else None
                    n = news[i] if i < len(news) else None
                    if o is not None and n is not None and difflib.SequenceMatcher(None, o, n).ratio() > 0.5:
                        rows.append(f"<div>{_word_diff(painter, o, n)}</div>")
                        added.append(n)
                    else:
                        if o is not None:
                            rows.append(f'<div style="{removed_style}">{html.escape(o)}</div>')
                        if n is not None:
                            added.append(n)
                            rows.append(f"<div>{painter.changed(n)}</div>")
        blocks.append("".join(rows))
        if len(blocks) >= max_blocks:
            blocks.append('<div style="color:#888;"><i>…more changes not shown; open the page to see '
                          'everything.</i></div>')
            break
    sep = '<div style="border-top:1px dashed #ccc;margin:6px 0;"></div>'
    return sep.join(blocks), "\n".join(added)
