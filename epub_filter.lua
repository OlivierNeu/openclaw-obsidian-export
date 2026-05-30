-- Pandoc Lua filter: drop EPUB front/back-matter navigation that carries no
-- prose value for a RAG (cover, title page, TOC, landmarks, page-list, etc.).
--
-- Why a filter and not -t gfm-raw_html: pandoc renders `<nav>` / `<section
-- epub:type="...">` as a Div in the AST, so `gfm-raw_html` only strips the
-- wrapper tag and KEEPS the rendered content (a "## Guide" heading + a
-- [Cover]/[Table of Contents]/[Begin Reading] link list, plus a long
-- page-list of [N](#page_N) links). This filter removes those whole Divs at
-- the AST level — including their content — while leaving `bodymatter`
-- (the actual chapters) untouched.

local DROP = {
  cover = true,
  titlepage = true,
  frontmatter = true,      -- only the nav-ish frontmatter; chapters are bodymatter
  ["toc"] = true,
  landmarks = true,
  ["page-list"] = true,
  ["copyright-page"] = true,
  colophon = true,
  ["loi"] = true,          -- list of illustrations
  ["lot"] = true,          -- list of tables
}

-- epub:type may hold several space-separated tokens (e.g. "frontmatter toc").
local function should_drop(epub_type)
  if not epub_type then return false end
  for token in epub_type:gmatch("%S+") do
    if DROP[token] then return true end
  end
  return false
end

function Div(el)
  if should_drop(el.attributes["epub:type"]) then
    return {}  -- remove the whole block, content included
  end
  return nil
end
