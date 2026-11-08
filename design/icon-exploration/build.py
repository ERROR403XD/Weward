"""Build the original Weward SVG candidates and their static review gallery."""

import html
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
GREEN = "#16745f"
MINT = "#8bc7ae"

# All marks use a 128-unit canvas and transparent negative space.
CONCEPTS = [
    (
        "forward-w", "向前 W", "字母标识",
        "把 W 的末笔折成向前的箭头，兼顾项目名称与中转方向。",
        '<path d="M13 29h17l14 49 14-35h14l14 35 13-40H87l25-22 6 33-11-6-20 60H76L65 73l-13 30H35Z"/>',
    ),
    (
        "conversation-w", "会话 W", "消息符号",
        "对话框底部化为 W 的两道折角，让字母融入消息轮廓。",
        '<path fill-rule="evenodd" d="M38 20h52a22 22 0 0 1 22 22v27a20 20 0 0 1-20 20h-6l-11 17-13-17h-7l-12 17-12-19A22 22 0 0 1 16 66V42a22 22 0 0 1 22-22Zm0 16a6 6 0 0 0-6 6v24a6 6 0 0 0 6 6h52a6 6 0 0 0 6-6V42a6 6 0 0 0-6-6Z"/>'
        '<rect x="45" y="48" width="38" height="12" rx="6"/>',
    ),
    (
        "relay-link", "接力环", "连接中转",
        "两段开口链环相接，表达消息在两个端点之间可靠流转。",
        '<g fill="none" stroke="currentColor" stroke-width="16" stroke-linecap="round">'
        '<path d="m58 76-7 7a22 22 0 0 1-31-31l23-23a22 22 0 0 1 31 0"/>'
        '<path d="m70 52 7-7a22 22 0 0 1 31 31L85 99a22 22 0 0 1-31 0"/>'
        '<path d="m48 80 32-32"/></g>',
    ),
    (
        "message-gate", "消息出口", "消息符号",
        "对话框向右敞开，箭头从内部延伸，直接表达消息中转。",
        '<path d="M94 19H39a23 23 0 0 0-23 23v35a22 22 0 0 0 22 22h3v15l25-15h28V83H62L51 90v-7H38a6 6 0 0 1-6-6V42a7 7 0 0 1 7-7h55Z"/>'
        '<path d="M58 52h32V39l28 22-28 22V69H58Z"/>',
    ),
    (
        "confluence", "汇流", "连接中转",
        "三路输入收束为一个出口，对应多群采集与统一投递。",
        '<g fill="none" stroke="currentColor" stroke-width="14" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M20 27h14c24 0 18 37 43 37h21M20 64h78M20 101h14c24 0 18-37 43-37"/></g>'
        '<path d="m88 42 29 22-29 22Z"/>',
    ),
    (
        "relay-bridge", "消息桥", "连接中转",
        "桥拱连接两端，稳重、简洁，强调持续运行的中转服务。",
        '<path fill-rule="evenodd" d="M14 58a50 50 0 0 1 100 0v45H90V61a26 26 0 0 0-52 0v42H14Zm36 19h28v26H50Z"/>'
        f'<rect x="50" y="50" width="28" height="16" rx="8" fill="{MINT}"/>',
    ),
    (
        "folded-packet", "折页 W", "字母标识",
        "四块折面组成紧凑的 W，也像一组被整理好的消息页。",
        '<path d="m14 27 25 11 25-11 25 11 25-11v62l-25 12-25-12-25 12-25-12Z"/>'
        f'<path d="m39 38 25-11v62l-25 12Zm50 0 25-11v62l-25 12Z" fill="{MINT}"/>',
    ),
    (
        "message-tray", "收信槽", "消息符号",
        "消息落入开放的收集槽，突出接收与持久保存。",
        '<path fill-rule="evenodd" d="M41 15h46a12 12 0 0 1 12 12v28a12 12 0 0 1-12 12H61L44 79V67h-3a12 12 0 0 1-12-12V27a12 12 0 0 1 12-12Zm4 16v20h38V31Z"/>'
        '<path d="M13 68h16v21a8 8 0 0 0 8 8h54a8 8 0 0 0 8-8V68h16v22a23 23 0 0 1-23 23H36a23 23 0 0 1-23-23Z"/>',
    ),
    (
        "relay-ring", "环形接力", "连接中转",
        "开放圆环围绕消息块，表达消息被接住后继续向前。",
        '<path d="M101 66a37 37 0 1 1-19-34" fill="none" stroke="currentColor" stroke-width="16" stroke-linecap="round"/>'
        '<path d="m82 14 22 30-36 1Z"/>'
        f'<rect x="49" y="49" width="30" height="30" rx="7" fill="{MINT}"/>',
    ),
    (
        "w-cutout", "负形 W", "字母标识",
        "以大块留白刻出 W，轮廓紧凑，适合标签页与应用图标。",
        '<path fill-rule="evenodd" d="M40 12h48a28 28 0 0 1 28 28v48a28 28 0 0 1-28 28H40a28 28 0 0 1-28-28V40a28 28 0 0 1 28-28Zm-13 25 14 54h16l7-22 7 22h16l14-54H84l-7 32-8-25H59l-8 25-7-32Z"/>',
    ),
    (
        "relay-tiles", "递进", "抽象几何",
        "三枚错位方块朝同一方向推进，像依次处理的消息队列。",
        '<path d="M16 70a10 10 0 0 1 10-10h22v22h20v22H26a10 10 0 0 1-10-10Z"/>'
        f'<path d="M46 42a10 10 0 0 1 10-10h22v22h20v22H56a10 10 0 0 1-10-10Z" fill="{MINT}"/>'
        '<rect x="76" y="14" width="36" height="34" rx="10"/>',
    ),
    (
        "chat-bookmark", "会话书签", "消息符号",
        "将聊天气泡与书签合并，强调消息的留存和回看。",
        '<path fill-rule="evenodd" d="M38 17h52a23 23 0 0 1 23 23v38a23 23 0 0 1-23 23H53l-26 15V98a23 23 0 0 1-12-20V40a23 23 0 0 1 23-23Zm13 16v43l15-11 15 11V33Z"/>',
    ),
    (
        "w-route", "W 轨迹", "字母标识",
        "一条连续的粗线走成 W，表达不中断的消息传递。",
        '<path d="m22 31 18 66 24-44 24 44 18-66" fill="none" stroke="currentColor" stroke-width="17" stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="106" cy="31" r="9" fill="{MINT}"/>',
    ),
    (
        "open-parcel", "消息包", "抽象几何",
        "打开的包裹由三块平面构成，对应整理后投递的文件包。",
        '<path d="m16 48 42 22v45L16 92Zm54 22 42-22v44l-42 23Z"/>'
        f'<path d="m20 35 44-22 44 22-44 23Z" fill="{MINT}"/>',
    ),
    (
        "conversation-weave", "交织", "抽象几何",
        "两条宽带交织成方形结，表达多来源消息的连接与组织。",
        '<path d="M44 14h50a20 20 0 0 1 20 20v50H94V38a4 4 0 0 0-4-4H44Z"/>'
        '<path d="M84 114H34a20 20 0 0 1-20-20V44h20v46a4 4 0 0 0 4 4h46Z"/>'
        f'<path d="M44 44h40v20H64v20H44Z" fill="{MINT}"/>',
    ),
    (
        "relay-wing", "信翼", "抽象几何",
        "两片折面组成向前的轻盈轮廓，像被送出的信件。",
        '<path d="M13 36 114 17 73 111 57 76 89 42 44 62Z"/>'
        f'<path d="m13 36 44 40 16 35-4-38 45-56-70 45Z" fill="{MINT}"/>',
    ),
    (
        "connected-rooms", "相连的群", "消息符号",
        "两个圆角空间由短桥连接，让群聊与中转关系一目了然。",
        '<path d="M26 20h24a16 16 0 0 1 16 16v19h12V73H58a10 10 0 0 1-10-10V38H28v31H10V36a16 16 0 0 1 16-16Zm76 88H78a16 16 0 0 1-16-16V73H50V55h20a10 10 0 0 1 10 10v25h20V59h18v33a16 16 0 0 1-16 16Z"/>'
        f'<path d="M10 77h18v13h20v18H26a16 16 0 0 1-16-16Zm108-26h-18V38H80V20h22a16 16 0 0 1 16 16Z" fill="{MINT}"/>',
    ),
    (
        "message-stack", "消息叠栈", "消息符号",
        "层叠卡片中的负形箭头，兼顾保存与向外投递。",
        f'<path d="M32 14h53a14 14 0 0 1 14 14v6H40a10 10 0 0 0-10 10v47H18V28a14 14 0 0 1 14-14Z" fill="{MINT}"/>'
        '<path fill-rule="evenodd" d="M47 42h53a14 14 0 0 1 14 14v44a14 14 0 0 1-14 14H47a14 14 0 0 1-14-14V56a14 14 0 0 1 14-14Zm5 29v14h25v12l22-19-22-19v12Z"/>',
    ),
    (
        "relay-pivot", "中转枢纽", "连接中转",
        "多条路径围绕中心折转，以一个明确出口结束。",
        '<path d="M19 17h19v28a9 9 0 0 0 9 9h39V40l31 24-31 24V74H47a28 28 0 0 1-28-28Z"/>'
        f'<path d="M19 111h19V93a9 9 0 0 1 9-9h25v19H47a9 9 0 0 0-9 9v-1Z" fill="{MINT}"/>'
        '<path d="M19 111V93a28 28 0 0 1 28-28h15v19H47a9 9 0 0 0-9 9v18Z"/>',
    ),
    (
        "window-w", "W 窗口", "字母标识",
        "圆角窗口内嵌 W，让品牌字母与 WebUI 的窗口形态相呼应。",
        '<path fill-rule="evenodd" d="M38 14h52a24 24 0 0 1 24 24v52a24 24 0 0 1-24 24H38a24 24 0 0 1-24-24V38a24 24 0 0 1 24-24Zm0 16a8 8 0 0 0-8 8v52a8 8 0 0 0 8 8h52a8 8 0 0 0 8-8V38a8 8 0 0 0-8-8Z"/>'
        '<path d="M30 51h15l8 30 11-21 11 21 8-30h15l-16 53H69l-5-12-5 12H46Z"/>',
    ),
]


def svg(body, title, **attrs):
    attributes = " ".join(f'{key}="{value}"' for key, value in attrs.items())
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" '
        f'fill="currentColor" color="{GREEN}" role="img" {attributes}>'
        f"<title>{html.escape(title)}</title>{body}</svg>"
    )


def build():
    (ROOT / "icons").mkdir(parents=True, exist_ok=True)
    cards, manifest = [], []
    sheet = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1570" viewBox="0 0 1600 1570">',
        "<title>Weward — 20 个图标设计方案</title>",
        '<rect width="1600" height="1570" fill="#f4f3ed"/>',
        '<g font-family="Noto Sans CJK SC, sans-serif" fill="#233c34">',
        '<text x="60" y="70" font-size="36" font-family="Georgia, serif" font-weight="bold">Weward / Icon studies</text>',
        '<text x="60" y="112" font-size="18" fill="#6c7c72">20 个不同的图形构思 · 点击选型页可查看使用效果</text>',
        '<text x="1540" y="70" text-anchor="end" font-size="15" letter-spacing="2">ROUND 01 / 2026.11</text>',
    ]
    for number, (slug, name, category, description, body) in enumerate(CONCEPTS, 1):
        key = f"{number:02d}"
        filename = f"icons/{key}-{slug}.svg"
        (ROOT / filename).write_text(svg(body, f"Weward {key} · {name}") + "\n", encoding="utf-8")
        manifest.append(dict(id=key, slug=slug, name=name, category=category, description=description, file=filename))
        smalls = "".join(
            f'<span class="size-sample"><img src="{filename}" width="{size}" height="{size}" alt=""><span>{size}</span></span>'
            for size in (16, 24, 32)
        )
        cards.append(f"""
        <button class="candidate" data-id="{key}" data-category="{category}"
                aria-label="{key} {name}，查看预览">
          <span class="card-top"><span class="number">{key}</span><span class="category">{category}</span></span>
          <span class="mark-stage"><img class="main-mark" src="{filename}" width="128" height="128" alt=""></span>
          <span class="card-heading"><strong>{name}</strong><span class="open-hint" aria-hidden="true">↗</span></span>
          <span class="card-description">{description}</span>
          <span class="card-bottom"><span class="samples">{smalls}</span><span class="saved-label">已加入候选</span></span>
        </button>""")
        col, row = (number - 1) % 5, (number - 1) // 5
        x, y = 60 + col * 300, 152 + row * 340
        sheet.extend([
            f'<rect x="{x}" y="{y}" width="280" height="320" rx="14" fill="#fff" stroke="#e0e5dc"/>',
            f'<text x="{x + 22}" y="{y + 34}" font-size="16" font-family="monospace" fill="#758477">{key}</text>',
            f'<text x="{x + 258}" y="{y + 34}" text-anchor="end" font-size="12" fill="#758477">{category}</text>',
            svg(body, name, x=x + 76, y=y + 56, width=128, height=128),
            f'<text x="{x + 22}" y="{y + 227}" font-size="21" font-weight="500">{name}</text>',
            f'<line x1="{x + 22}" x2="{x + 258}" y1="{y + 249}" y2="{y + 249}" stroke="#edf0eb"/>',
            f'<text x="{x + 258}" y="{y + 289}" text-anchor="end" font-size="11" fill="#829085">16 / 24 / 32 px</text>',
        ])
        for size, dx in ((16, 22), (24, 56), (32, 98)):
            sheet.append(svg(body, name, x=x + dx, y=y + 292 - size, width=size, height=size))
    sheet.append("</g></svg>")
    (ROOT / "overview.svg").write_text("\n".join(sheet) + "\n", encoding="utf-8")
    (ROOT / "concepts.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    template = (ROOT / "index.template.html").read_text(encoding="utf-8")
    page = template.replace("<!-- CANDIDATES -->", "\n".join(cards))
    page = page.replace("/* CONCEPT_DATA */", json.dumps(manifest, ensure_ascii=False))
    page = "\n".join(line.rstrip() for line in page.splitlines()) + "\n"
    (ROOT / "index.html").write_text(page, encoding="utf-8")
    with ZipFile(ROOT / "weward-20-icons.zip", "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "icons").glob("*.svg")):
            archive.write(path, path.relative_to(ROOT))
        for filename in ("index.html", "gallery.css", "gallery.js", "concepts.json", "overview.svg"):
            archive.write(ROOT / filename, filename)
    print(f"Built {len(manifest)} SVG candidates, gallery, contact sheet, and ZIP in {ROOT}")


if __name__ == "__main__":
    build()
