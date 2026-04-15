# personal_site_dos

Static personal site hosted on GitHub Pages.

## Structure

- `index.html` is the homepage
- `posts/*.md` are the blog source files
- `templates/` contains the blog HTML templates
- `build_posts.py` generates `blog/index.html` and `blog/<post>/index.html`

## Local preview

From the repo root:

```bash
python3 -m http.server 4000
```

Then open:

- `http://127.0.0.1:4000/`
- `http://127.0.0.1:4000/blog/`

## Adding a post

1. Create a new file in `posts/` named like `YYYY-MM-DD-my-post.md`
2. Add front matter:

```md
---
title: My Post Title
date: 2026-04-14
description: One-sentence summary for the blog index and meta description.
---
```

3. Write the body in Markdown
4. Run:

```bash
python3 build_posts.py
```

5. Push the generated `blog/` pages along with the Markdown source

## Notes

- The slug comes from the filename after the date
- The site uses the installed Python `markdown` package, which is already available in this environment
- `index.html.save` is just a local backup file and can be ignored or deleted
