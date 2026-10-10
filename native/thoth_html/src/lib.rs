use std::collections::{HashMap, HashSet};

use ego_tree::NodeId;
use pyo3::prelude::*;
use regex::Regex;
use scraper::{ElementRef, Html, Selector};
use url::Url;

mod institutional_search;

const CONTENT_SELECTOR: &str = "h1,h2,h3,p,li,blockquote,figcaption";
const CONTAINER_SELECTOR: &str = "article,main,section,div";

fn selector(value: &str) -> Selector {
    Selector::parse(value).expect("static selector must parse")
}

fn host_for_url(value: Option<&str>) -> String {
    value
        .and_then(|raw| Url::parse(raw).ok())
        .and_then(|url| url.host_str().map(str::to_lowercase))
        .unwrap_or_default()
}

fn clean_whitespace(value: &str) -> String {
    let mut lines: Vec<String> = value
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .split('\n')
        .map(|line| line.split_whitespace().collect::<Vec<_>>().join(" "))
        .collect();
    while lines.first().is_some_and(|line| line.is_empty()) {
        lines.remove(0);
    }
    while lines.last().is_some_and(|line| line.is_empty()) {
        lines.pop();
    }
    let mut out = Vec::with_capacity(lines.len());
    let mut blanks = 0;
    for line in lines {
        if line.is_empty() {
            blanks += 1;
            if blanks <= 1 {
                out.push(line);
            }
        } else {
            blanks = 0;
            out.push(line);
        }
    }
    out.join("\n")
}

fn text_of(node: ElementRef<'_>, separator: &str) -> String {
    node.text()
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(separator)
}

fn profile_roots(host: &str) -> &'static [&'static str] {
    if host == "x.com" || host.ends_with(".x.com") || host.contains("twitter.com") {
        &[
            "[data-testid='twitterArticleReadView']",
            "[data-testid='twitterArticleRichTextView']",
            "article[data-testid='tweet']",
            "main",
        ]
    } else if host.contains("fool.com") {
        &[
            "#article-body-transcript",
            ".transcript-content",
            ".article-body",
            "#content-page",
            "main",
        ]
    } else if host.contains("semiwiki.com") {
        &[
            ".message-body .bbWrapper",
            ".message-body",
            ".message-content",
            ".message-userContent",
            ".bbWrapper",
            "article",
            "main",
        ]
    } else if host.contains("nvidianews.nvidia.com") {
        &[".article-body", "article", ".main", "main"]
    } else if host.contains("semiengineering.com") {
        &[
            "article",
            ".entry-content",
            ".post-content",
            ".post-entry",
            ".td-post-content",
            "#content article",
            "#content",
        ]
    } else if host.contains("trendforce.com") {
        &[
            ".presscenter",
            ".press-center",
            ".presscenter-detail",
            ".presscenter-content",
            ".news_content",
            ".content",
            "article",
            "main",
        ]
    } else if host.contains("hankyung.com") {
        &[
            "#articletxt",
            "#article_body",
            "#article-body",
            ".article-body",
            ".article-text",
            ".news-article",
            ".view-content",
            "article",
            "main",
        ]
    } else {
        &[]
    }
}

fn profile_removals(host: &str) -> &'static [&'static str] {
    if host.contains("fool.com") {
        &[
            ".related",
            ".recommend",
            ".promo",
            ".advert",
            ".ads",
            ".sponsor",
            ".social",
            ".share",
            ".newsletter",
            ".subscribe",
            ".sidebar",
        ]
    } else if host.contains("semiwiki.com") {
        &[
            ".message-user",
            ".message-attribution",
            ".message-footer",
            ".message-actionBar",
            ".message-signature",
            ".shareButtons",
            ".p-breadcrumbs",
            ".p-footer",
        ]
    } else if host.contains("nvidianews.nvidia.com") {
        &[
            ".article-bottom",
            ".related",
            ".related-news",
            ".sidebar",
            ".share",
            ".social",
            ".nav",
            ".menu",
            ".footer",
            ".header",
        ]
    } else if host.contains("semiengineering.com") {
        &[
            ".comments",
            "#comments",
            ".related",
            ".related-posts",
            ".popular",
            ".tags",
            ".breadcrumbs",
            ".sidebar",
            ".widget",
            ".newsletter",
            ".subscribe",
            ".sponsor",
            ".ad",
            ".social",
            ".share",
            ".menu",
            ".nav",
            ".mega-menu",
        ]
    } else if host.contains("trendforce.com") {
        &[
            ".leftmenu",
            ".rightmenu",
            ".menu",
            ".nav",
            ".breadcrumbs",
            ".sidebar",
            ".related",
            ".tags",
            ".social",
            ".share",
            ".newsletter",
            ".subscribe",
            ".footer",
        ]
    } else if host.contains("hankyung.com") {
        &[
            ".article-ad",
            ".ad",
            ".advertisement",
            ".banner",
            ".sponsor",
            ".promotion",
            ".share",
            ".sns",
            ".comment",
            ".related",
            ".recommend",
            ".ranking",
            ".newsletter",
            ".login",
            ".toolbar",
            ".breadcrumbs",
            ".nav",
            ".sidebar",
            ".footer",
            ".header",
        ]
    } else {
        &[]
    }
}

fn pick_root<'a>(document: &'a Html, host: &str) -> ElementRef<'a> {
    for raw in profile_roots(host) {
        if let Some(node) = document.select(&selector(raw)).next() {
            return node;
        }
    }
    let mut best: Option<(i64, ElementRef<'a>)> = None;
    let paragraph_selector = selector("p");
    let heading_selector = selector("h1,h2,h3");
    let list_selector = selector("li");
    let link_selector = selector("a");
    for node in document.select(&selector(CONTAINER_SELECTOR)) {
        let paragraphs = node.select(&paragraph_selector).count() as i64;
        if paragraphs < 2 {
            continue;
        }
        let text_len = text_of(node, " ").len() as i64;
        if text_len < 400 {
            continue;
        }
        let headings = node.select(&heading_selector).count() as i64;
        let list_items = node.select(&list_selector).count() as i64;
        let link_len: i64 = node
            .select(&link_selector)
            .map(|link| text_of(link, " ").len() as i64)
            .sum();
        let score = paragraphs * 220 + headings * 80 + text_len - link_len * 2 - list_items * 12;
        if best
            .as_ref()
            .is_none_or(|(best_score, _)| score > *best_score)
        {
            best = Some((score, node));
        }
    }
    best.map(|(_, node)| node)
        .or_else(|| document.select(&selector("article")).next())
        .or_else(|| document.select(&selector("main")).next())
        .or_else(|| document.select(&selector("body")).next())
        .unwrap_or_else(|| document.root_element())
}

fn excluded_nodes(document: &Html, host: &str) -> HashSet<NodeId> {
    let mut excluded = HashSet::new();
    for raw in [
        "script", "style", "noscript", "svg", "iframe", "footer", "nav", "aside", "form",
    ]
    .into_iter()
    .chain(profile_removals(host).iter().copied())
    {
        for node in document.select(&selector(raw)) {
            excluded.insert(node.id());
        }
    }
    let boilerplate = Regex::new(r"(?i)(?:^|[_-])(?:nav|navbar|menu|submenu|header|footer|breadcrumb|cookie|consent|subscribe|newsletter|social|share|login|signup|account|search|related|recommend|promo|advert|ads|sponsor|sidebar|sticky|toolbar|lang|language|translation|switcher|pagination|pager|sitemap)(?:[_-]|$)").unwrap();
    for node in document.select(&selector("[id],[class],[aria-hidden],[role]")) {
        let value = node.value();
        let hidden = value
            .attr("aria-hidden")
            .is_some_and(|v| matches!(v.trim().to_ascii_lowercase().as_str(), "true" | "1"));
        let bad_role = value.attr("role").is_some_and(|v| {
            matches!(
                v.trim().to_ascii_lowercase().as_str(),
                "navigation" | "banner" | "contentinfo" | "search"
            )
        });
        let bad_id = value.attr("id").is_some_and(|v| boilerplate.is_match(v));
        let bad_class = value.classes().any(|v| boilerplate.is_match(v));
        if hidden || bad_role || bad_id || bad_class {
            let text_len = text_of(node, " ").len();
            let p_count = node.select(&selector("p")).count();
            if text_len < 1200 || p_count < 4 {
                excluded.insert(node.id());
            }
        }
    }
    excluded
}

fn has_excluded_ancestor(node: ElementRef<'_>, excluded: &HashSet<NodeId>) -> bool {
    node.ancestors()
        .any(|ancestor| excluded.contains(&ancestor.id()))
}

fn filtered_root_text(root: ElementRef<'_>, excluded: &HashSet<NodeId>) -> String {
    root.descendants()
        .filter(|node| {
            !node
                .ancestors()
                .any(|ancestor| excluded.contains(&ancestor.id()))
        })
        .filter_map(|node| node.value().as_text())
        .map(|text| text.trim())
        .filter(|text| !text.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn clean_site_lines(text: String, host: &str) -> String {
    if !host.contains("hankyung.com") && !host.contains("udn.com") {
        return text;
    }
    let mut out = Vec::new();
    for line in text.lines() {
        let raw = line.trim();
        let low = raw.to_ascii_lowercase();
        if host.contains("udn.com")
            && ([
                "※",
                "延伸閱讀",
                "延伸阅读",
                "相關新聞",
                "相關報導",
                "推薦閱讀",
                "更多新聞",
                "更多內容",
            ]
            .iter()
            .any(|v| raw.contains(v))
                || [
                    "歡迎轉載",
                    "欢迎转载",
                    "請勿全文轉載",
                    "请勿全文转载",
                    "未經授權",
                    "未经授权",
                    "do not copy",
                    "do not repost",
                    "reposting",
                ]
                .iter()
                .any(|v| low.contains(v)))
        {
            break;
        }
        if host.contains("hankyung.com")
            && ([
                "advertisement",
                "article scrap",
                "share",
                "comments",
                "comment",
                "clean view",
                "print",
            ]
            .contains(&low.as_str())
                || low.contains("unauthorized reproduction")
                || low.contains("hankyung.com")
                || raw.contains('ⓒ'))
        {
            continue;
        }
        if !raw.is_empty() {
            out.push(raw);
        }
    }
    clean_whitespace(&out.join("\n"))
}

fn article_title(document: &Html, host: &str) -> Option<String> {
    if (host == "x.com" || host.ends_with(".x.com") || host.contains("twitter.com"))
        && let Some(title) = document
            .select(&selector("[data-testid='twitter-article-title']"))
            .next()
            .map(|node| clean_whitespace(&text_of(node, " ")))
            .filter(|value| !value.is_empty())
    {
        return Some(title);
    }
    for raw in [
        "meta[property='og:title']",
        "meta[name='twitter:title']",
        "meta[name='title']",
        "meta[itemprop='name']",
    ] {
        if let Some(content) = document
            .select(&selector(raw))
            .next()
            .and_then(|node| node.value().attr("content"))
        {
            let title = clean_whitespace(content);
            if !title.is_empty() {
                return Some(title);
            }
        }
    }
    let root = pick_root(document, host);
    let excluded = excluded_nodes(document, host);
    for raw in ["h1", "h2", "h3"] {
        if let Some(title) = root
            .select(&selector(raw))
            .find(|node| !has_excluded_ancestor(*node, &excluded))
            .map(|node| clean_whitespace(&text_of(node, " ")))
            .filter(|value| !value.is_empty())
        {
            return Some(title);
        }
    }
    document
        .select(&selector("title"))
        .next()
        .map(|node| clean_whitespace(&text_of(node, " ")))
        .filter(|value| !value.is_empty())
}

fn resolved_image_url(candidate: &str, base: Option<&str>) -> Option<String> {
    let raw = candidate.trim();
    if raw.is_empty() || raw.starts_with("data:") {
        return None;
    }
    let mut resolved = match base.and_then(|value| Url::parse(value).ok()) {
        Some(base_url) => base_url
            .join(raw)
            .map(|url| url.to_string())
            .unwrap_or_else(|_| raw.to_string()),
        None => raw.to_string(),
    };
    if let Ok(mut url) = Url::parse(&resolved)
        && url
            .host_str()
            .is_some_and(|host| host.eq_ignore_ascii_case("pbs.twimg.com"))
        && url.path().starts_with("/media/")
    {
        let mut query: Vec<(String, String)> = url
            .query_pairs()
            .filter(|(key, _)| key != "name")
            .map(|(key, value)| (key.into_owned(), value.into_owned()))
            .collect();
        query.push(("name".to_string(), "orig".to_string()));
        url.query_pairs_mut().clear().extend_pairs(query);
        resolved = url.to_string();
    }
    let path = Url::parse(&resolved)
        .ok()
        .map(|url| url.path().to_ascii_lowercase())
        .unwrap_or_else(|| resolved.to_ascii_lowercase());
    (!path.ends_with(".svg")).then_some(resolved)
}

fn add_image(out: &mut Vec<String>, candidate: &str, base: Option<&str>, limit: usize) {
    if out.len() < limit
        && let Some(url) = resolved_image_url(candidate, base)
        && !out.contains(&url)
    {
        out.push(url);
    }
}

fn article_images(document: &Html, host: &str, base: Option<&str>, limit: usize) -> Vec<String> {
    let mut out = Vec::new();
    let is_x = host == "x.com" || host.ends_with(".x.com") || host.contains("twitter.com");
    if !is_x {
        for raw in [
            "meta[property='og:image']",
            "meta[property='og:image:url']",
            "meta[name='twitter:image']",
            "meta[name='twitter:image:src']",
            "meta[itemprop='image']",
        ] {
            if let Some(content) = document
                .select(&selector(raw))
                .next()
                .and_then(|node| node.value().attr("content"))
            {
                add_image(&mut out, content, base, limit);
            }
        }
    }
    if out.len() < limit {
        let root = pick_root(document, host);
        let excluded = excluded_nodes(document, host);
        for image in root.select(&selector("img")) {
            if has_excluded_ancestor(image, &excluded) {
                continue;
            }
            let value = image.value();
            if let Some(src) = value.attr("src").or_else(|| value.attr("data-src")) {
                if is_x && !src.contains("pbs.twimg.com/media") {
                    continue;
                }
                add_image(&mut out, src, base, limit);
            } else if let Some(srcset) = value.attr("srcset")
                && let Some(src) = srcset
                    .split(',')
                    .next_back()
                    .and_then(|part| part.split_whitespace().next())
            {
                if is_x && !src.contains("pbs.twimg.com/media") {
                    continue;
                }
                add_image(&mut out, src, base, limit);
            }
            if out.len() >= limit {
                break;
            }
        }
    }
    out.truncate(limit);
    out
}

fn article_text(document: &Html, host: &str) -> String {
    let root = pick_root(document, host);
    let excluded = excluded_nodes(document, host);
    let li_selector = selector("li");
    let mut lines = Vec::new();
    for node in root.select(&selector(CONTENT_SELECTOR)) {
        if has_excluded_ancestor(node, &excluded) {
            continue;
        }
        if node.value().name() == "li" {
            let oversized = node
                .ancestors()
                .filter_map(ElementRef::wrap)
                .find(|parent| matches!(parent.value().name(), "ul" | "ol"))
                .is_some_and(|parent| parent.select(&li_selector).count() > 20);
            if oversized {
                continue;
            }
        }
        let text = clean_whitespace(&text_of(node, " "));
        if !text.is_empty() {
            lines.push(text);
        }
    }
    let mut joined = clean_whitespace(&lines.join("\n"));
    let root_text = clean_whitespace(&filtered_root_text(root, &excluded));
    if joined.is_empty()
        || ((!root_text.is_empty() && joined.len() < 180 && root_text.len() > 500)
            || joined.len() * 3 < root_text.len())
    {
        joined = root_text;
    }
    clean_site_lines(joined, host)
}

#[pyfunction]
#[pyo3(signature = (html, url=None))]
fn extract_text_from_html(html: &str, url: Option<&str>) -> String {
    let document = Html::parse_document(html);
    article_text(&document, &host_for_url(url))
}

#[pyfunction]
#[pyo3(signature = (html, url=None))]
fn extract_title_from_html(html: &str, url: Option<&str>) -> Option<String> {
    let document = Html::parse_document(html);
    article_title(&document, &host_for_url(url))
}

#[pyfunction]
#[pyo3(signature = (html, url=None, limit=3))]
fn extract_image_urls_from_html(html: &str, url: Option<&str>, limit: usize) -> Vec<String> {
    let document = Html::parse_document(html);
    article_images(&document, &host_for_url(url), url, limit)
}

#[pyfunction]
#[pyo3(signature = (html, url=None, image_limit=3))]
fn extract_document_from_html(
    html: &str,
    url: Option<&str>,
    image_limit: usize,
) -> (String, Option<String>, Vec<String>) {
    let document = Html::parse_document(html);
    let host = host_for_url(url);
    (
        article_text(&document, &host),
        article_title(&document, &host),
        article_images(&document, &host, url, image_limit),
    )
}

fn search_tokens(value: &str) -> HashSet<String> {
    value
        .split(|character: char| !character.is_ascii_lowercase() && !character.is_ascii_digit())
        .filter(|token| token.len() >= 2)
        .map(str::to_string)
        .collect()
}

fn sequence_ratio(left: &str, right: &str) -> f64 {
    let a: Vec<char> = left.chars().collect();
    let b: Vec<char> = right.chars().collect();
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    let mut positions: HashMap<char, Vec<usize>> = HashMap::new();
    for (index, character) in b.iter().copied().enumerate() {
        positions.entry(character).or_default().push(index);
    }
    let mut pending = vec![(0usize, a.len(), 0usize, b.len())];
    let mut matched = 0usize;
    while let Some((a_start, a_end, b_start, b_end)) = pending.pop() {
        let mut previous: HashMap<usize, usize> = HashMap::new();
        let mut best_a = a_start;
        let mut best_b = b_start;
        let mut best_size = 0usize;
        for (a_index, character) in a.iter().enumerate().take(a_end).skip(a_start) {
            let mut current = HashMap::new();
            if let Some(indices) = positions.get(character) {
                for &b_index in indices
                    .iter()
                    .filter(|&&index| index >= b_start && index < b_end)
                {
                    let size = previous.get(&b_index.wrapping_sub(1)).copied().unwrap_or(0) + 1;
                    current.insert(b_index, size);
                    if size > best_size {
                        best_a = a_index + 1 - size;
                        best_b = b_index + 1 - size;
                        best_size = size;
                    }
                }
            }
            previous = current;
        }
        if best_size == 0 {
            continue;
        }
        matched += best_size;
        if a_start < best_a && b_start < best_b {
            pending.push((a_start, best_a, b_start, best_b));
        }
        let next_a = best_a + best_size;
        let next_b = best_b + best_size;
        if next_a < a_end && next_b < b_end {
            pending.push((next_a, a_end, next_b, b_end));
        }
    }
    (2 * matched) as f64 / (a.len() + b.len()) as f64
}

#[pyfunction]
fn rank_search_values(query: &str, values: Vec<String>) -> i32 {
    let query_text = query.trim().to_lowercase();
    if query_text.is_empty() {
        return 999;
    }
    let query_tokens = search_tokens(&query_text);
    let mut best = 999;
    for value in values {
        let text = value.trim().to_lowercase();
        if text.is_empty() {
            continue;
        }
        if text == query_text {
            return 0;
        }
        if text.starts_with(&query_text) {
            best = best.min(1);
        } else if text.contains(&query_text) {
            best = best.min(2);
        }
        let overlap = query_tokens.intersection(&search_tokens(&text)).count() as i32;
        if overlap > 0 {
            best = best.min(3.max(12 - overlap));
        }
        let comparison: String = text
            .chars()
            .take(12.max(query_text.chars().count() + 20))
            .collect();
        if sequence_ratio(&query_text, &comparison) >= 0.70 {
            best = best.min(8);
        }
    }
    best
}

#[pymodule]
fn _thoth_html(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        institutional_search::institutional_search_index_statements,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        institutional_search::plan_institutional_identities,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        institutional_search::plan_institutional_search,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(extract_text_from_html, module)?)?;
    module.add_function(wrap_pyfunction!(extract_title_from_html, module)?)?;
    module.add_function(wrap_pyfunction!(extract_image_urls_from_html, module)?)?;
    module.add_function(wrap_pyfunction!(extract_document_from_html, module)?)?;
    module.add_function(wrap_pyfunction!(rank_search_values, module)?)?;
    module.add("IMPLEMENTATION", "rust-scraper")?;
    Ok(())
}
