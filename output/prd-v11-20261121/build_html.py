import re, html, os, json

SRC = r"C:\Users\可怜虫\WorkBuddy\2026-11-21-16-17-02\output\prd-v11-20261121\stage1\organized_draft.md"
OUT = r"C:\Users\可怜虫\WorkBuddy\2026-11-21-16-17-02\output\prd-v11-20261121\stage2\formatted-知鉴Search产品需求文档.html"

lines = open(SRC, encoding="utf-8").read().split("\n")

def inline(t):
    t = html.escape(t, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    return t

blocks = []   # (kind, data)
cur_table = None
cur_list = None

def flush_table():
    global cur_table
    if cur_table:
        blocks.append(("table", cur_table))
        cur_table = None

def flush_list():
    global cur_list
    if cur_list:
        blocks.append(("list", cur_list))
        cur_list = None

for raw in lines:
    line = raw.rstrip()
    s = line.strip()
    if not s:
        flush_table(); flush_list()
        continue
    if s.startswith("|"):
        flush_list()
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue
        if cur_table is None:
            cur_table = []
        cur_table.append(cells)
        continue
    flush_table()
    m = re.match(r"^(#{1,4})\s+(.*)$", s)
    if m:
        flush_list()
        blocks.append(("h%d" % len(m.group(1)), m.group(2)))
        continue
    m = re.match(r"^(\d+)\.\s+(.*)$", s)
    if m:
        if cur_list is None or cur_list[0] != "ol":
            flush_list(); cur_list = ("ol", [])
        cur_list[1].append(m.group(2)); continue
    m = re.match(r"^-\s+(.*)$", s)
    if m:
        if cur_list is None or cur_list[0] != "ul":
            flush_list(); cur_list = ("ul", [])
        cur_list[1].append(m.group(1)); continue
    flush_list()
    blocks.append(("p", s))
flush_table(); flush_list()

# assign ids to h2 (chapters)
chap = 0
toc = []
out = []
for kind, data in blocks:
    if kind == "h1":            # markdown ## -> doc chapter (h1)
        continue                # document title, carried by cover
    if kind == "h2":
        chap += 1
        cid = "section-%d" % chap
        toc.append((cid, data, []))
        out.append('<h1 id="%s">%s</h1>' % (cid, inline(data)))
    elif kind == "h3":
        sid = ""
        if toc:
            sid = "%s-%d" % (toc[-1][0], len(toc[-1][2]) + 1)
            toc[-1][2].append((sid, data))
        out.append('<h2 id="%s">%s</h2>' % (sid, inline(data)))
    elif kind == "h4":
        out.append("<h3>%s</h3>" % inline(data))
    elif kind == "p":
        out.append("<p>%s</p>" % inline(data))
    elif kind == "list":
        tag, items = data
        lis = "".join("<li>%s</li>" % inline(x) for x in items)
        out.append("<%s>%s</%s>" % (tag, lis, tag))
    elif kind == "table":
        rows = data
        head = "".join("<th>%s</th>" % inline(c) for c in rows[0])
        body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c) for c in r) for r in rows[1:])
        out.append(
            '<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (head, body)
        )

toc_html = []
for cid, title, subs in toc:
    li = ['<li><a href="#%s">%s</a>' % (cid, html.escape(title, quote=False))]
    if subs:
        li.append("<ol>")
        for sid, s in subs:
            li.append('<li><a href="#%s">%s</a></li>' % (sid, html.escape(s, quote=False)))
        li.append("</ol>")
    li.append("</li>")
    toc_html.append("".join(li))

CSS = """
:root {
  --ff-heading: "SimHei", "黑体", "Microsoft YaHei", sans-serif;
  --ff-body: "SimSun", "宋体", serif;
  --ff-mono: "Consolas", "Courier New", monospace;
  --fs-cover-title: 30pt;
  --fs-cover-sub: 13pt;
  --fs-h1: 17pt;
  --fs-h2: 13.5pt;
  --fs-h3: 12pt;
  --fs-body: 12pt;
  --fs-table: 10.5pt;
  --fs-small: 10.5pt;
  --lh-body: 1.72;
  --lh-heading: 1.4;
  --lh-toc: 1.55;
  --fw-bold: 700;
  --fw-normal: 400;
  --color-primary: #1B4B7A;
  --color-text: #23262B;
  --color-muted: #6A7280;
  --color-border: #C9D2DC;
  --color-line: #E3E8EE;
  --color-th-bg: #EDF2F7;
  --color-alt-bg: #F7F9FB;
  --spacing-para: 0.62em;
  --spacing-section: 1.5em;
  --spacing-block: 0.9em;
  --margin-page: 2.5cm;
  --page-content-width: 16cm;
}
@page {
  margin: var(--margin-page);
  @bottom-center { content: "— " counter(page) " —"; }
}
@page cover {
  @bottom-center { content: none; }
}
section[role="cover"] { page: cover; }
body {
  max-width: var(--page-content-width);
  margin-left: auto;
  margin-right: auto;
  font-family: var(--ff-body);
  color: var(--color-text);
}
p {
  font-family: var(--ff-body);
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  color: var(--color-text);
  text-align: justify;
  text-indent: 2em;
  margin: var(--spacing-para) 0;
}
h1 {
  font-family: var(--ff-heading);
  font-size: var(--fs-h1);
  font-weight: var(--fw-bold);
  color: var(--color-primary);
  line-height: var(--lh-heading);
  text-align: left;
  text-indent: 0;
  margin: var(--spacing-section) 0 var(--spacing-block) 0;
  padding-bottom: 0.28em;
  border-bottom: 0.9pt solid var(--color-primary);
}
h2 {
  font-family: var(--ff-heading);
  font-size: var(--fs-h2);
  font-weight: var(--fw-bold);
  color: var(--color-text);
  line-height: var(--lh-heading);
  text-align: left;
  text-indent: 0;
  margin: var(--spacing-section) 0 var(--spacing-para) 0;
}
h3 {
  font-family: var(--ff-heading);
  font-size: var(--fs-h3);
  font-weight: var(--fw-bold);
  color: var(--color-text);
  line-height: var(--lh-heading);
  text-align: left;
  text-indent: 0;
  margin: var(--spacing-block) 0 var(--spacing-para) 0;
}
strong { font-weight: var(--fw-bold); color: var(--color-primary); }
code {
  font-family: var(--ff-mono);
  font-size: var(--fs-table);
  color: var(--color-primary);
}
ul, ol { margin: var(--spacing-para) 0; padding-left: 2.2em; }
li {
  font-family: var(--ff-body);
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  color: var(--color-text);
  text-align: justify;
  text-indent: 0;
  margin: 0.2em 0;
}
table {
  width: 100%;
  border-collapse: collapse;
  margin: var(--spacing-block) 0;
  font-family: var(--ff-body);
  font-size: var(--fs-table);
}
th {
  font-family: var(--ff-heading);
  font-size: var(--fs-table);
  font-weight: var(--fw-bold);
  color: var(--color-text);
  text-align: left;
  text-indent: 0;
  background: var(--color-th-bg);
  border: 0.5pt solid var(--color-border);
  padding: 0.32em 0.5em;
}
td {
  font-family: var(--ff-body);
  font-size: var(--fs-table);
  color: var(--color-text);
  text-align: left;
  text-indent: 0;
  border: 0.5pt solid var(--color-border);
  padding: 0.32em 0.5em;
  vertical-align: top;
}
.cover-title {
  font-family: var(--ff-heading);
  font-size: var(--fs-cover-title);
  font-weight: var(--fw-bold);
  color: var(--color-primary);
  text-align: center;
  text-indent: 0;
  line-height: var(--lh-heading);
  margin: 2.6em 0 0.5em 0;
}
.cover-subtitle {
  font-family: var(--ff-heading);
  font-size: var(--fs-cover-sub);
  font-weight: var(--fw-normal);
  color: var(--color-muted);
  text-align: center;
  text-indent: 0;
  line-height: var(--lh-heading);
  margin: 0 0 3.4em 0;
}
.cover-meta {
  font-family: var(--ff-body);
  font-size: var(--fs-body);
  color: var(--color-text);
  text-align: center;
  text-indent: 0;
  line-height: var(--lh-toc);
  margin: 0.18em 0;
}
.cover-label {
  font-family: var(--ff-heading);
  font-size: var(--fs-body);
  font-weight: var(--fw-bold);
  color: var(--color-primary);
  text-align: center;
  text-indent: 0;
  margin: 3.6em 0 0.2em 0;
}
.toc-title {
  font-family: var(--ff-heading);
  font-size: var(--fs-h1);
  font-weight: var(--fw-bold);
  color: var(--color-primary);
  text-align: center;
  text-indent: 0;
  margin: 0 0 1.1em 0;
}
.toc-list, .toc-list ol, .toc-list ul { list-style: none; }
.toc-list { padding-left: 0; margin: 0; }
.toc-list li {
  font-family: var(--ff-body);
  font-size: var(--fs-body);
  color: var(--color-text);
  line-height: var(--lh-toc);
  text-indent: 0;
  text-align: left;
  margin: 0.1em 0;
}
.toc-list ol { padding-left: 1.8em; margin: 0.05em 0 0.3em 0; }
.toc-list ol li { font-size: var(--fs-small); color: var(--color-muted); }
"""

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="docx-page-size" content="A4">
<title>知鉴Search 产品需求文档 v1.1</title>
<style>%s</style>
</head>
<body>

<section role="cover">
<p class="cover-label">产品需求文档</p>
<p class="cover-title">知鉴Search</p>
<p class="cover-subtitle">基于知乎开放内容的检索增强与信息审校层</p>
<p class="cover-meta">知乎黑客松 S3 · 湾区共创季</p>
<p class="cover-meta">赛道 01「AI × 知识与探索」（社区 × 知识发现）</p>
<p class="cover-meta">版本 v1.1</p>
<p class="cover-meta">2026 年 11 月</p>
</section>

<section role="cover">
<nav class="doc-toc" aria-label="文档目录">
<p class="toc-title">目录</p>
<ol class="toc-list">%s</ol>
</nav>
</section>

<section role="body" data-page-restart="1">
%s
</section>

</body>
</html>
""" % (CSS, "\n".join(toc_html), "\n".join(out))

os.makedirs(os.path.dirname(OUT), exist_ok=True)
open(OUT, "w", encoding="utf-8").write(HTML)
print("written", OUT)
print("chapters", chap, "toc entries", len(toc), "blocks", len(out))
