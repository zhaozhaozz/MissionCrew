/* ---- 统一 Markdown 阅读组件 ----
   所有文档型预览共用安全正文渲染和 YAML front matter 属性表。 */
const MISSIONCREW_RESOURCE_TYPES = new Set([
  "documents", "channels", "tasks", "dashboards", "guidelines", "skills",
  "recycle-bin",
]);

function canonicalMissionCrewResourceType(type) {
  return ({ boards: "dashboards", dashboard: "dashboards", panels: "dashboards" })[type]
    || type;
}

function missionCrewResourceUrl(projectId, type, ...segments) {
  const resourceType = canonicalMissionCrewResourceType(String(type || ""));
  if (!projectId || !MISSIONCREW_RESOURCE_TYPES.has(resourceType)) return "";
  const parts = ["resources", projectId, resourceType, ...segments.filter(Boolean)];
  return "/" + parts.map(value => encodeURIComponent(String(value))).join("/");
}

function missionCrewDocumentUrl(projectId, path = "") {
  return missionCrewResourceUrl(
    projectId, "documents", ...String(path).split("/").filter(Boolean));
}

function missionCrewResourceReference(target) {
  const raw = String(target || "").trim();
  let pathname = raw.split(/[?#]/, 1)[0].replace(/\\/g, "/");
  if (/^https?:\/\//i.test(pathname)) {
    try {
      const parsed = new URL(pathname);
      if (parsed.origin !== location.origin) return null;
      pathname = parsed.pathname;
    } catch (_) { return null; }
  }
  const rawParts = pathname.replace(/^\/+/, "").split("/");
  const decode = value => {
    try { return decodeURIComponent(value); } catch (_) { return value; }
  };
  if (rawParts[0] === "resources" && rawParts[1] && rawParts[2]) {
    const projectId = decode(rawParts[1]);
    const type = canonicalMissionCrewResourceType(decode(rawParts[2]));
    const segments = rawParts.slice(3).filter(Boolean).map(decode);
    if (MISSIONCREW_RESOURCE_TYPES.has(type)) {
      return { projectId, type, segments,
               url: missionCrewResourceUrl(projectId, type, ...segments) };
    }
  }
  // 历史消息曾发布平台真实路径。只识别 MissionCrew 自有文档入口，
  // 立即转换成资源 URL；任意其他绝对路径仍不会成为可点击链接。
  let decoded = pathname;
  try { decoded = decodeURIComponent(pathname); } catch (_) { /* 保留原值 */ }
  let match = decoded.match(/(?:^|\/)\.missioncrew\/projects\/([^/]+)\/documents\/(.+)$/);
  if (!match)
    match = decoded.match(/(?:^|\/)\.missioncrew\/agent-workspaces\/([^/]+)\/.*?\/\.missioncrew\/documents\/(.+)$/);
  if (match) {
    const projectId = match[1], path = match[2];
    const segments = path.split("/").filter(Boolean);
    return { projectId, type: "documents", segments,
             url: missionCrewResourceUrl(projectId, "documents", ...segments) };
  }
  return null;
}

function missionCrewDocumentReference(target) {
  const resource = missionCrewResourceReference(target);
  if (!resource || resource.type !== "documents") return null;
  return { ...resource, path: resource.segments.join("/") };
}

function markdownInline(source) {
  const tokens = [];
  const hold = html => `\uE000${tokens.push(html) - 1}\uE001`;
  let value = String(source ?? "");
  value = value.replace(/`([^`\n]+)`/g, (_, code) => hold(`<code>${esc(code)}</code>`));
  value = value.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,
    (_, label, target) => {
      const safeLabel = markdownInline(label);
      const href = target.trim();
      const resource = missionCrewResourceReference(href);
      if (resource)
        return hold(`<a href="${esc(resource.url)}" data-resource-link="${esc(resource.url)}" ` +
          `onclick="return openMissionCrewResourceLink(event,this.dataset.resourceLink)">${safeLabel}</a>`);
      if (/^(https?:\/\/|mailto:)/i.test(href))
        return hold(`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${safeLabel}</a>`);
      if (href.startsWith("#")) return hold(`<a href="${esc(href)}">${safeLabel}</a>`);
      if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return hold(safeLabel);
      return hold(`<a href="#" data-doc-link="${esc(href)}" ` +
        `onclick="return openMarkdownDocumentLink(event,this.dataset.docLink)">${safeLabel}</a>`);
    });
  let html = esc(value)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
  return html.replace(/\uE000(\d+)\uE001/g, (_, index) => tokens[Number(index)]);
}

function markdownTableCells(line) {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, "|"));
}

function markdownBlockStart(lines, index) {
  const line = lines[index] || "";
  return /^\s*(```|~~~)/.test(line) || /^(#{1,6})\s+/.test(line)
    || /^\s*(?:[-*+] |\d+[.)] )/.test(line) || /^\s*>/.test(line)
    || /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)
    || (line.includes("|") && /^\s*\|?\s*:?-{3,}:?/.test(lines[index + 1] || ""));
}

function miniMarkdown(text) {
  // 项目内容来自用户和 Agent，先按块解析并逐段转义，避免 Markdown 预览注入 HTML。
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const output = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }

    const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)\s*$/);
    if (fence) {
      const body = [];
      index += 1;
      while (index < lines.length && !new RegExp(`^\\s*${fence[1]}`).test(lines[index]))
        body.push(lines[index++]);
      if (index < lines.length) index += 1;
      const language = fence[2] ? ` class="language-${esc(fence[2])}"` : "";
      output.push(`<pre><code${language}>${esc(body.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+?)\s*#*$/);
    if (heading) {
      const level = heading[1].length;
      const slug = heading[2].replace(/[^\p{L}\p{N}\s-]/gu, "").trim()
        .replace(/\s+/g, "-").toLowerCase();
      output.push(`<h${level}${slug ? ` id="${esc(slug)}"` : ""}>${markdownInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      output.push("<hr>");
      index += 1;
      continue;
    }

    if (/^\s*>/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>/.test(lines[index]))
        quote.push(lines[index++].replace(/^\s*>\s?/, ""));
      output.push(`<blockquote>${miniMarkdown(quote.join("\n"))}</blockquote>`);
      continue;
    }

    const list = line.match(/^\s*([-*+]|\d+[.)])\s+(.+)$/);
    if (list) {
      const ordered = /^\d/.test(list[1]);
      const tag = ordered ? "ol" : "ul";
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*([-*+]|\d+[.)])\s+(.+)$/);
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        items.push(`<li>${markdownInline(item[2])}</li>`);
        index += 1;
      }
      output.push(`<${tag}>${items.join("")}</${tag}>`);
      continue;
    }

    if (line.includes("|") && /^\s*\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?\s*$/.test(lines[index + 1] || "")) {
      const headers = markdownTableCells(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim())
        rows.push(markdownTableCells(lines[index++]));
      output.push(`<div class="markdown-table-wrap"><table><thead><tr>${headers.map(cell =>
        `<th>${markdownInline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map(row =>
        `<tr>${headers.map((_, column) => `<td>${markdownInline(row[column] || "")}</td>`).join("")}</tr>`
      ).join("")}</tbody></table></div>`);
      continue;
    }

    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !markdownBlockStart(lines, index))
      paragraph.push(lines[index++]);
    output.push(`<p>${paragraph.map(markdownInline).join("<br>")}</p>`);
  }
  return output.join("\n");
}

function splitMarkdownFrontmatter(markdown) {
  const normalized = String(markdown ?? "").replace(/\r\n?/g, "\n");
  if (!normalized.startsWith("---\n"))
    return { frontmatter: null, content: normalized };
  const remainder = normalized.slice(4);
  const closing = remainder.match(/^---[ \t]*$/m);
  if (!closing) return { frontmatter: null, content: normalized };
  return {
    frontmatter: remainder.slice(0, closing.index).replace(/\n$/, ""),
    content: remainder.slice(closing.index + closing[0].length).replace(/^\n/, ""),
  };
}

function markdownFrontmatterTableHtml(source) {
  const rows = [];
  let current = null;
  for (const line of String(source ?? "").replace(/\r\n?/g, "\n").split("\n")) {
    const property = line.match(/^([A-Za-z_][\w.-]*):(?:[ \t]*(.*))?$/);
    if (property) {
      current = { key: property[1], value: property[2] || "" };
      rows.push(current);
    } else if (current) {
      const continuation = line.replace(/^(?: {2}|\t)/, "");
      current.value += `${current.value ? "\n" : ""}${continuation}`;
    } else if (line.trim()) {
      current = { key: "YAML", value: line };
      rows.push(current);
    }
  }
  return `<div class="markdown-table-wrap"><table class="markdown-frontmatter-table">
    <thead><tr><th>属性</th><th>值</th></tr></thead>
    <tbody>${rows.map(row => `<tr><th scope="row">${esc(row.key)}</th>
      <td><code class="markdown-frontmatter-value">${esc(row.value)}</code></td></tr>`).join("")}</tbody>
  </table></div>`;
}

function markdownPreviewHtml(markdown, { showFrontmatter = true } = {}) {
  const parsed = splitMarkdownFrontmatter(markdown);
  const properties = showFrontmatter && parsed.frontmatter !== null
    ? markdownFrontmatterTableHtml(parsed.frontmatter) : "";
  return `${properties}${miniMarkdown(parsed.content)}`;
}
