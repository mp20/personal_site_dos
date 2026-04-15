from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import html
import re
import shutil

import markdown


ROOT = Path(__file__).parent
POSTS_DIR = ROOT / "posts"
BLOG_DIR = ROOT / "blog"
TEMPLATES_DIR = ROOT / "templates"
POST_TEMPLATE = TEMPLATES_DIR / "post.html"
INDEX_TEMPLATE = TEMPLATES_DIR / "blog_index.html"


@dataclass
class Post:
    title: str
    date: str
    description: str
    slug: str
    content_html: str
    output_dir: Path

    @property
    def pretty_date(self) -> str:
        return datetime.strptime(self.date, "%Y-%m-%d").strftime("%Y-%m-%d")


def parse_post(path: Path) -> Post:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError(f"{path.name}: missing front matter")

    raw_meta, body = match.groups()
    meta: dict[str, str] = {}
    for line in raw_meta.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"{path.name}: invalid front matter line: {line}")
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()

    required = ["title", "date", "description"]
    missing = [key for key in required if key not in meta or not meta[key]]
    if missing:
        raise ValueError(f"{path.name}: missing {', '.join(missing)}")

    filename = path.stem
    slug_match = re.match(r"^\d{4}-\d{2}-\d{2}-(.+)$", filename)
    if not slug_match:
        raise ValueError(f"{path.name}: expected filename format YYYY-MM-DD-slug.md")
    slug = slug_match.group(1)

    body_html = markdown.markdown(
        body.strip(),
        extensions=["fenced_code", "tables", "sane_lists"],
    )
    indented_body = "\n".join(
        f"        {line}" if line else "" for line in body_html.splitlines()
    )

    return Post(
        title=meta["title"],
        date=meta["date"],
        description=meta["description"],
        slug=slug,
        content_html=indented_body,
        output_dir=BLOG_DIR / slug,
    )


def render_post(post: Post, template: str) -> str:
    return (
        template.replace("{{TITLE}}", html.escape(post.title))
        .replace("{{DATE}}", html.escape(post.pretty_date))
        .replace("{{DESCRIPTION}}", html.escape(post.description, quote=True))
        .replace("{{CONTENT}}", post.content_html)
    )


def render_index(posts: list[Post], template: str) -> str:
    items = []
    for post in posts:
        items.append(
            "\n".join(
                [
                    '        <article class="post">',
                    f'          <h2 class="post-title"><a href="/blog/{post.slug}/">{html.escape(post.title)}</a></h2>',
                    f'          <div class="meta">{html.escape(post.pretty_date)}</div>',
                    f'          <p class="excerpt">{html.escape(post.description)}</p>',
                    "        </article>",
                ]
            )
        )
    return template.replace("{{POST_LIST}}", "\n".join(items))


def main() -> None:
    BLOG_DIR.mkdir(exist_ok=True)

    posts = sorted(
        (parse_post(path) for path in POSTS_DIR.glob("*.md")),
        key=lambda post: post.date,
        reverse=True,
    )

    existing_dirs = [path for path in BLOG_DIR.iterdir() if path.is_dir()]
    valid_dirs = {post.output_dir for post in posts}
    for path in existing_dirs:
        if path not in valid_dirs:
            shutil.rmtree(path)

    post_template = POST_TEMPLATE.read_text(encoding="utf-8")
    for post in posts:
        post.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = post.output_dir / "index.html"
        output_path.write_text(render_post(post, post_template), encoding="utf-8")

    index_template = INDEX_TEMPLATE.read_text(encoding="utf-8")
    (BLOG_DIR / "index.html").write_text(
        render_index(posts, index_template),
        encoding="utf-8",
    )

    print(f"Built {len(posts)} post(s).")


if __name__ == "__main__":
    main()
